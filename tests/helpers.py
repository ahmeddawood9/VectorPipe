from __future__ import annotations

import io
import threading

from app.services import ObjectStorage, Queue
from app.services.queue import QueueMessage, QueueStats


class FakeClock:
    """Manually advanced clock for testing visibility timeouts without sleeping."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start
        self._lock = threading.Lock()

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        with self._lock:
            self.now += seconds


class FlakyStorage(ObjectStorage):
    """Delegates to a real storage, but can be told to fail specific operations."""

    def __init__(self, inner: ObjectStorage) -> None:
        self.inner = inner
        self.fail_get: Exception | None = None
        self.fail_put: Exception | None = None
        self.put_calls: list[str] = []

    def put_object(self, key: str, data: bytes) -> None:
        self.put_calls.append(key)
        if self.fail_put:
            raise self.fail_put
        self.inner.put_object(key, data)

    def get_object(self, key: str) -> bytes:
        if self.fail_get:
            raise self.fail_get
        return self.inner.get_object(key)

    def delete_object(self, key: str) -> None:
        self.inner.delete_object(key)


class FlakyQueue(Queue):
    def __init__(self, inner: Queue) -> None:
        self.inner = inner
        self.fail_enqueue: Exception | None = None
        self.fail_receive_times = 0

    @property
    def max_receive_count(self) -> int:
        return self.inner.max_receive_count

    def enqueue(self, body: str) -> str:
        if self.fail_enqueue:
            raise self.fail_enqueue
        return self.inner.enqueue(body)

    def receive(self, **kwargs) -> list[QueueMessage]:
        if self.fail_receive_times > 0:
            self.fail_receive_times -= 1
            raise RuntimeError("queue temporarily unavailable")
        return self.inner.receive(**kwargs)

    def delete(self, receipt_handle: str) -> bool:
        return self.inner.delete(receipt_handle)

    def change_visibility(self, receipt_handle: str, timeout_seconds: float) -> bool:
        return self.inner.change_visibility(receipt_handle, timeout_seconds)

    def stats(self) -> QueueStats:
        return self.inner.stats()


def upload(client, name: str = "notes.txt", data: bytes = b"hello vectorpipe " * 200, ctype: str = "text/plain"):
    return client.post("/documents", files={"file": (name, io.BytesIO(data), ctype)})


def step(queue: Queue, processor):
    """Receive exactly one message and hand it to the processor; returns its Outcome (or None if empty)."""
    messages = queue.receive(max_messages=1, wait_seconds=0)
    return processor.handle(messages[0]) if messages else None
