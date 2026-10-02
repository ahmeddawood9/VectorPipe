"""Queue abstraction plus a SQLite-backed local implementation and an SQS implementation.

The semantics deliberately mirror the small subset of SQS the application needs:

* ``enqueue`` appends a message.
* ``receive`` hands out a visible message and hides it for ``visibility_timeout`` seconds (in flight).
* ``delete`` acknowledges a message using the receipt handle from its *latest* delivery.
* A message that is never deleted becomes visible again after the timeout (retry). Every delivery
  increments ``receive_count``; once a message has been delivered ``max_receive_count`` times and is
  still not acknowledged it is moved to a dead-letter state instead of being delivered again.
* ``change_visibility`` lets a consumer schedule the next retry (backoff).

``LocalQueue`` uses one SQLite file in WAL mode with ``BEGIN IMMEDIATE`` transactions, so any number
of API/worker processes can share it safely. It is a development/testing stand-in, not a broker.

``SqsQueue`` maps the same six operations onto Amazon SQS. Dead-lettering is done by SQS itself through the
queue's redrive policy, so the application only has to agree with that policy on ``max_receive_count``.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from botocore.exceptions import BotoCoreError, ClientError

from app.services.aws import create_client

logger = logging.getLogger(__name__)


class QueueError(Exception):
    """Base class for queue failures."""


@dataclass(frozen=True, slots=True)
class QueueMessage:
    message_id: str
    receipt_handle: str
    body: str
    receive_count: int
    enqueued_at: float


@dataclass(frozen=True, slots=True)
class QueueStats:
    visible: int
    in_flight: int
    dead: int


class Queue(ABC):
    @property
    @abstractmethod
    def max_receive_count(self) -> int:
        """Deliveries after which an unacknowledged message is dead-lettered."""

    @abstractmethod
    def enqueue(self, body: str) -> str:
        """Append a message; returns its message id."""

    @abstractmethod
    def receive(
        self,
        *,
        max_messages: int = 1,
        wait_seconds: float = 0.0,
        visibility_timeout: float | None = None,
        stop_event: threading.Event | None = None,
    ) -> list[QueueMessage]:
        """Long-poll for up to ``wait_seconds``; returns as soon as a message is available."""

    @abstractmethod
    def delete(self, receipt_handle: str) -> bool:
        """Acknowledge a message. Returns False if the handle is unknown/stale."""

    @abstractmethod
    def change_visibility(self, receipt_handle: str, timeout_seconds: float) -> bool:
        """Re-time an in-flight message. Returns False if the handle is unknown/stale."""

    @abstractmethod
    def stats(self) -> QueueStats:
        """Approximate message counts."""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id     TEXT    NOT NULL UNIQUE,
    body           TEXT    NOT NULL,
    enqueued_at    REAL    NOT NULL,
    visible_at     REAL    NOT NULL,
    receive_count  INTEGER NOT NULL DEFAULT 0,
    receipt_handle TEXT,
    state          TEXT    NOT NULL DEFAULT 'active'
);
CREATE INDEX IF NOT EXISTS ix_messages_ready ON messages (state, visible_at, seq);
CREATE INDEX IF NOT EXISTS ix_messages_receipt ON messages (receipt_handle);
"""


class LocalQueue(Queue):
    def __init__(
        self,
        path: str | Path,
        *,
        visibility_timeout: float = 30.0,
        max_receive_count: int = 3,
        poll_interval: float = 0.5,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if visibility_timeout <= 0 or max_receive_count < 1 or poll_interval <= 0:
            raise ValueError("visibility_timeout, max_receive_count and poll_interval must be positive")
        self._path = Path(path).expanduser()
        self._visibility_timeout = visibility_timeout
        self._max_receive_count = max_receive_count
        self._poll_interval = poll_interval
        self._clock = clock
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @property
    def max_receive_count(self) -> int:
        return self._max_receive_count

    # ------------------------------------------------------------------ connection helpers
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=30.0, isolation_level=None)
        return conn

    def _init_schema(self) -> None:
        try:
            conn = self._connect()
            try:
                try:
                    conn.execute("PRAGMA journal_mode=WAL")
                except sqlite3.DatabaseError:
                    pass  # some filesystems do not support WAL; the queue still works
                conn.executescript(_SCHEMA)
            finally:
                conn.close()
        except sqlite3.Error as exc:
            raise QueueError(f"cannot initialise queue at {self._path}: {exc}") from exc

    @contextmanager
    def _write_txn(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")
        except sqlite3.Error as exc:
            raise QueueError(str(exc)) from exc
        finally:
            conn.close()

    # ------------------------------------------------------------------ API
    def enqueue(self, body: str) -> str:
        if not isinstance(body, str):
            raise TypeError("body must be a str (use JSON)")
        message_id = uuid.uuid4().hex
        now = self._clock()
        with self._write_txn() as conn:
            conn.execute(
                "INSERT INTO messages (message_id, body, enqueued_at, visible_at) VALUES (?, ?, ?, ?)",
                (message_id, body, now, now),
            )
        return message_id

    def receive(
        self,
        *,
        max_messages: int = 1,
        wait_seconds: float = 0.0,
        visibility_timeout: float | None = None,
        stop_event: threading.Event | None = None,
    ) -> list[QueueMessage]:
        if max_messages < 1:
            raise ValueError("max_messages must be >= 1")
        timeout = self._visibility_timeout if visibility_timeout is None else visibility_timeout
        deadline = self._clock() + wait_seconds
        while True:
            messages = self._receive_once(max_messages, timeout)
            if messages:
                return messages
            if stop_event is not None and stop_event.is_set():
                return []
            remaining = deadline - self._clock()
            if remaining <= 0:
                return []
            pause = min(self._poll_interval, remaining)
            if stop_event is not None:
                stop_event.wait(pause)
            else:
                time.sleep(pause)

    def _receive_once(self, max_messages: int, visibility_timeout: float) -> list[QueueMessage]:
        now = self._clock()
        try:
            # Cheap read-only probe first so idle pollers do not contend for the write lock.
            conn = self._connect()
            try:
                has_work = conn.execute(
                    "SELECT 1 FROM messages WHERE state = 'active' AND visible_at <= ? LIMIT 1", (now,)
                ).fetchone()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            raise QueueError(str(exc)) from exc
        if not has_work:
            return []

        delivered: list[QueueMessage] = []
        with self._write_txn() as conn:
            # Redrive: visible again but already delivered the maximum number of times -> dead letter.
            conn.execute(
                "UPDATE messages SET state = 'dead' "
                "WHERE state = 'active' AND visible_at <= ? AND receive_count >= ?",
                (now, self._max_receive_count),
            )
            rows = conn.execute(
                "SELECT seq, message_id, body, receive_count, enqueued_at FROM messages "
                "WHERE state = 'active' AND visible_at <= ? ORDER BY seq LIMIT ?",
                (now, max_messages),
            ).fetchall()
            for seq, message_id, body, receive_count, enqueued_at in rows:
                handle = uuid.uuid4().hex
                conn.execute(
                    "UPDATE messages SET visible_at = ?, receive_count = receive_count + 1, "
                    "receipt_handle = ? WHERE seq = ?",
                    (now + visibility_timeout, handle, seq),
                )
                delivered.append(QueueMessage(message_id, handle, body, receive_count + 1, enqueued_at))
        return delivered

    def delete(self, receipt_handle: str) -> bool:
        with self._write_txn() as conn:
            cur = conn.execute(
                "DELETE FROM messages WHERE receipt_handle = ? AND state = 'active'", (receipt_handle,)
            )
            return cur.rowcount == 1

    def change_visibility(self, receipt_handle: str, timeout_seconds: float) -> bool:
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be >= 0")
        now = self._clock()
        with self._write_txn() as conn:
            cur = conn.execute(
                "UPDATE messages SET visible_at = ? WHERE receipt_handle = ? AND state = 'active'",
                (now + timeout_seconds, receipt_handle),
            )
            return cur.rowcount == 1

    def stats(self) -> QueueStats:
        now = self._clock()
        try:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT "
                    " COALESCE(SUM(state = 'active' AND visible_at <= :now AND receive_count < :max), 0),"
                    " COALESCE(SUM(state = 'active' AND visible_at > :now), 0),"
                    " COALESCE(SUM(state = 'dead' OR (state = 'active' AND visible_at <= :now"
                    "                                 AND receive_count >= :max)), 0) "
                    "FROM messages",
                    {"now": now, "max": self._max_receive_count},
                ).fetchone()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            raise QueueError(str(exc)) from exc
        return QueueStats(visible=int(row[0]), in_flight=int(row[1]), dead=int(row[2]))


# SQS limits: 10 messages per receive, 20 s per long poll, 12 h of visibility.
_SQS_MAX_BATCH = 10
_SQS_MAX_WAIT = 20
_SQS_MAX_VISIBILITY = 12 * 3600
# Error codes SQS uses when a receipt handle no longer refers to an in-flight message.
_STALE_HANDLE_CODES = {"ReceiptHandleIsInvalid", "MessageNotInflight", "InvalidParameterValue"}


class SqsQueue(Queue):
    """Amazon SQS standard queue with a dead-letter queue.

    Credentials come from the ambient AWS identity (IAM role, SSO, env). The queue's redrive policy
    (``maxReceiveCount``) is what actually dead-letters messages; ``max_receive_count`` here must match it
    so the worker knows which delivery is the final attempt. Both are checked on construction.

    Caveat: SQS cannot tell that a receipt handle is stale. Deleting a message with the handle of an older
    delivery can report success without removing it, so ``delete() is True`` is not proof the message is
    gone. Idempotent, atomically claimed processing (not this class) is what prevents duplicate work.
    """

    def __init__(
        self,
        queue_url: str,
        dlq_url: str,
        *,
        region: str | None = None,
        profile: str | None = None,
        visibility_timeout: float = 30.0,
        max_receive_count: int = 3,
        poll_chunk_seconds: float = 2.0,
        stats_cache_seconds: float = 0.0,
        verify_redrive: bool = True,
        client=None,
    ) -> None:
        if not queue_url or not dlq_url:
            raise ValueError("queue_url and dlq_url are required")
        if visibility_timeout <= 0 or max_receive_count < 1 or poll_chunk_seconds <= 0:
            raise ValueError("visibility_timeout, max_receive_count and poll_chunk_seconds must be positive")
        self._queue_url = queue_url
        self._dlq_url = dlq_url
        self._visibility_timeout = visibility_timeout
        self._max_receive_count = max_receive_count
        # A single long poll blocks for at most this long, so a stop request is noticed promptly.
        self._poll_chunk = min(poll_chunk_seconds, _SQS_MAX_WAIT)
        self._stats_cache_seconds = stats_cache_seconds
        self._stats_cache: tuple[float, QueueStats] | None = None
        if client is None:
            # read_timeout must exceed the long-poll wait
            client = create_client("sqs", region=region, profile=profile, read_timeout=_SQS_MAX_WAIT + 10)
        self._sqs = client
        if verify_redrive:
            self._check_redrive_policy()

    @property
    def max_receive_count(self) -> int:
        return self._max_receive_count

    # ------------------------------------------------------------------ helpers
    def _attributes(self, queue_url: str, names: list[str]) -> dict[str, str]:
        try:
            return self._sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=names).get("Attributes", {})
        except (BotoCoreError, ClientError) as exc:
            raise QueueError(f"get_queue_attributes failed: {exc}") from exc

    def _check_redrive_policy(self) -> None:
        raw = self._attributes(self._queue_url, ["RedrivePolicy"]).get("RedrivePolicy")
        if not raw:
            raise QueueError(
                "the SQS queue has no redrive policy, so failed messages would never be dead-lettered; "
                "attach the dead-letter queue to it"
            )
        configured = int(json.loads(raw).get("maxReceiveCount", 0))
        if configured != self._max_receive_count:
            logger.warning(
                "QUEUE_MAX_RECEIVE_COUNT does not match the queue's redrive policy; the final attempt "
                "will be misjudged",
                extra={"app_max_receive_count": self._max_receive_count, "queue_max_receive_count": configured},
            )

    # ------------------------------------------------------------------ Queue interface
    def enqueue(self, body: str) -> str:
        try:
            return self._sqs.send_message(QueueUrl=self._queue_url, MessageBody=body)["MessageId"]
        except (BotoCoreError, ClientError) as exc:
            raise QueueError(f"send_message failed: {exc}") from exc

    def receive(
        self,
        *,
        max_messages: int = 1,
        wait_seconds: float = 0.0,
        visibility_timeout: float | None = None,
        stop_event: threading.Event | None = None,
    ) -> list[QueueMessage]:
        if max_messages < 1:
            raise ValueError("max_messages must be >= 1")
        timeout = self._visibility_timeout if visibility_timeout is None else visibility_timeout
        timeout_s = min(max(0, math.ceil(timeout)), _SQS_MAX_VISIBILITY)
        deadline = time.monotonic() + wait_seconds
        while True:
            remaining = deadline - time.monotonic()
            wait = min(math.ceil(max(remaining, 0)), math.ceil(self._poll_chunk))
            try:
                response = self._sqs.receive_message(
                    QueueUrl=self._queue_url,
                    MaxNumberOfMessages=min(max_messages, _SQS_MAX_BATCH),
                    WaitTimeSeconds=wait,
                    VisibilityTimeout=timeout_s,
                    MessageSystemAttributeNames=["ApproximateReceiveCount", "SentTimestamp"],
                )
            except (BotoCoreError, ClientError) as exc:
                raise QueueError(f"receive_message failed: {exc}") from exc
            raw_messages = response.get("Messages", [])
            if raw_messages:
                return [self._to_message(m) for m in raw_messages]
            if stop_event is not None and stop_event.is_set():
                return []
            if deadline - time.monotonic() <= 0:
                return []

    @staticmethod
    def _to_message(raw: dict) -> QueueMessage:
        attrs = raw.get("Attributes", {})
        return QueueMessage(
            message_id=raw["MessageId"],
            receipt_handle=raw["ReceiptHandle"],
            body=raw["Body"],
            receive_count=int(attrs.get("ApproximateReceiveCount", 1)),
            enqueued_at=int(attrs.get("SentTimestamp", 0)) / 1000.0,
        )

    def delete(self, receipt_handle: str) -> bool:
        try:
            self._sqs.delete_message(QueueUrl=self._queue_url, ReceiptHandle=receipt_handle)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in _STALE_HANDLE_CODES:
                return False
            raise QueueError(f"delete_message failed: {exc}") from exc
        except BotoCoreError as exc:
            raise QueueError(f"delete_message failed: {exc}") from exc
        return True

    def change_visibility(self, receipt_handle: str, timeout_seconds: float) -> bool:
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be >= 0")
        try:
            self._sqs.change_message_visibility(
                QueueUrl=self._queue_url,
                ReceiptHandle=receipt_handle,
                VisibilityTimeout=min(math.ceil(timeout_seconds), _SQS_MAX_VISIBILITY),
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in _STALE_HANDLE_CODES:
                return False
            raise QueueError(f"change_message_visibility failed: {exc}") from exc
        except BotoCoreError as exc:
            raise QueueError(f"change_message_visibility failed: {exc}") from exc
        return True

    def stats(self) -> QueueStats:
        now = time.monotonic()
        if self._stats_cache and now - self._stats_cache[0] < self._stats_cache_seconds:
            return self._stats_cache[1]
        main = self._attributes(
            self._queue_url, ["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"]
        )
        dead = self._attributes(
            self._dlq_url, ["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"]
        )
        result = QueueStats(
            visible=int(main["ApproximateNumberOfMessages"]),
            in_flight=int(main["ApproximateNumberOfMessagesNotVisible"]),
            dead=int(dead["ApproximateNumberOfMessages"]) + int(dead["ApproximateNumberOfMessagesNotVisible"]),
        )
        self._stats_cache = (now, result)
        return result
