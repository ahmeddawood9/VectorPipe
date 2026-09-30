"""Handling of a single queue message: claim -> process -> complete -> acknowledge.

Delivery is at-least-once, so every step is written to be safe to repeat:

* The claim is one conditional UPDATE, so concurrent workers cannot both own a job.
* The result object has a deterministic key and deterministic bytes; re-writing it is harmless.
* The message is acknowledged only after the database says COMPLETED. A crash at any earlier point
  leaves the message to reappear, and a duplicate delivery of a finished job is simply acknowledged.
"""

from __future__ import annotations

import enum
import logging
import random
import time
from collections.abc import Callable

from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.metrics import WorkerMetrics
from app.schemas import JobMessage
from app.services.documents import (
    ClaimResult,
    claim_document,
    mark_attempt_failed,
    mark_completed,
)
from app.services.object_storage import ObjectStorage
from app.services.queue import Queue, QueueMessage
from app.worker.embedding import generate_embeddings, serialize

logger = logging.getLogger(__name__)


class Outcome(str, enum.Enum):
    COMPLETED = "completed"  # processed and acknowledged
    DUPLICATE = "duplicate"  # already COMPLETED; acknowledged without reprocessing
    BUSY = "busy"  # another worker is processing it; left for redelivery
    RETRY = "retry"  # attempt failed, will be redelivered after backoff
    FAILED = "failed"  # final attempt failed; document is FAILED, message will be dead-lettered
    INVALID = "invalid"  # malformed message / unknown document; left for dead-lettering


class JobProcessor:
    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: sessionmaker[Session],
        storage: ObjectStorage,
        queue: Queue,
        metrics: WorkerMetrics | None = None,
        sleep: Callable[[float], None] = time.sleep,
        random_fn: Callable[[], float] = random.random,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._storage = storage
        self._queue = queue
        self._metrics = metrics or WorkerMetrics()
        self._sleep = sleep
        self._random = random_fn

    # ------------------------------------------------------------------ public
    def handle(self, message: QueueMessage) -> Outcome:
        started = time.monotonic()
        outcome = self._handle(message)
        self._metrics.jobs.labels(outcome.value).inc()
        self._metrics.duration.observe(time.monotonic() - started)
        return outcome

    # ------------------------------------------------------------------ internals
    def _backoff(self, receive_count: int) -> float:
        base = self._settings.retry_backoff_seconds
        return min(base * (2 ** max(0, receive_count - 1)), self._settings.queue_visibility_timeout_seconds)

    def _handle(self, message: QueueMessage) -> Outcome:
        ctx = {"message_id": message.message_id, "receive_count": message.receive_count}

        try:
            job = JobMessage.model_validate_json(message.body)
        except ValidationError as exc:
            logger.error("invalid job message; leaving it for dead-lettering", extra={**ctx, "error": str(exc)})
            self._queue.change_visibility(message.receipt_handle, self._backoff(message.receive_count))
            return Outcome.INVALID
        ctx["document_id"] = str(job.document_id)

        with self._session_factory() as session:
            claim = claim_document(
                session, job.document_id, stale_after_seconds=self._settings.processing_stale_after_seconds
            )

        if claim.result is ClaimResult.NOT_FOUND:
            logger.error("job refers to an unknown document; leaving it for dead-lettering", extra=ctx)
            self._queue.change_visibility(message.receipt_handle, self._backoff(message.receive_count))
            return Outcome.INVALID
        if claim.result is ClaimResult.ALREADY_COMPLETED:
            logger.info("duplicate delivery of a completed document; acknowledging", extra=ctx)
            self._acknowledge(message, ctx)
            return Outcome.DUPLICATE
        if claim.result is ClaimResult.IN_PROGRESS:
            logger.info("document is being processed by another worker; skipping", extra=ctx)
            return Outcome.BUSY

        doc = claim.document
        assert doc is not None
        logger.info(
            "processing started", extra={**ctx, "doc_filename": doc.original_filename, "attempt": doc.attempts}
        )

        try:
            raw = self._storage.get_object(doc.raw_object_key)
            if self._settings.simulate_failure_rate and self._random() < self._settings.simulate_failure_rate:
                raise RuntimeError("simulated transient failure (SIMULATE_FAILURE_RATE)")
            self._sleep(self._settings.processing_delay_seconds)  # stands in for the expensive AI work
            result = generate_embeddings(document_id=str(doc.id), filename=doc.original_filename, data=raw)
            processed_key = f"processed/{doc.id}.json"
            self._storage.put_object(processed_key, serialize(result))
            with self._session_factory() as session:
                if not mark_completed(session, doc.id, processed_key):
                    raise RuntimeError("document row vanished before it could be marked COMPLETED")
        except Exception as exc:  # noqa: BLE001 - any failure is recorded and retried
            return self._record_failure(message, doc.id, exc, ctx)

        self._acknowledge(message, ctx)
        logger.info("processing completed", extra={**ctx, "chunks": result["chunk_count"]})
        return Outcome.COMPLETED

    def _acknowledge(self, message: QueueMessage, ctx: dict) -> None:
        if not self._queue.delete(message.receipt_handle):
            # The message was redelivered to someone else meanwhile; they will acknowledge it.
            logger.warning("receipt handle no longer valid; message was already redelivered", extra=ctx)

    def _record_failure(self, message: QueueMessage, document_id, exc: Exception, ctx: dict) -> Outcome:
        final = message.receive_count >= self._queue.max_receive_count
        error = f"{type(exc).__name__}: {exc}"
        logger.exception("processing failed", extra={**ctx, "final": final, "error": error})
        self._metrics.failures.inc()
        try:
            with self._session_factory() as session:
                mark_attempt_failed(session, document_id, error, final=final)
        except Exception:  # noqa: BLE001 - the stale-PROCESSING reclaim path still recovers this job
            logger.exception("could not record the failure in the database", extra=ctx)
        # Deliberately NOT acknowledged. A non-final failure reappears after an exponential backoff; after
        # the final attempt the message becomes visible immediately so the queue dead-letters it.
        delay = 0.0 if final else self._backoff(message.receive_count)
        self._queue.change_visibility(message.receipt_handle, delay)
        return Outcome.FAILED if final else Outcome.RETRY
