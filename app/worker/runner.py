"""The worker loop: long-poll the queue and hand messages to the processor."""

from __future__ import annotations

import logging
import threading

from app.services.queue import Queue
from app.worker.processor import JobProcessor

logger = logging.getLogger(__name__)

LONG_POLL_SECONDS = 20.0
ERROR_PAUSE_SECONDS = 2.0


class Worker:
    def __init__(self, queue: Queue, processor: JobProcessor, stop_event: threading.Event | None = None) -> None:
        self._queue = queue
        self._processor = processor
        self.stop_event = stop_event or threading.Event()

    def request_stop(self) -> None:
        self.stop_event.set()

    def run_forever(self) -> None:
        """Process messages until :meth:`request_stop` is called; the current job always finishes."""
        logger.info("worker started")
        while not self.stop_event.is_set():
            try:
                messages = self._queue.receive(
                    max_messages=1, wait_seconds=LONG_POLL_SECONDS, stop_event=self.stop_event
                )
            except Exception:  # noqa: BLE001 - keep the worker alive through transient queue errors
                logger.exception("receiving from the queue failed; will retry")
                self.stop_event.wait(ERROR_PAUSE_SECONDS)
                continue
            for message in messages:
                self._safe_handle(message)
        logger.info("worker stopped")

    def run_once(self) -> int:
        """Handle every currently visible message without waiting (used by tests and scripts)."""
        handled = 0
        while True:
            messages = self._queue.receive(max_messages=1, wait_seconds=0)
            if not messages:
                return handled
            for message in messages:
                self._safe_handle(message)
                handled += 1

    def _safe_handle(self, message) -> None:
        try:
            self._processor.handle(message)
        except Exception:  # noqa: BLE001 - one bad message must not kill the worker
            logger.exception(
                "unexpected error while handling message; it will be redelivered",
                extra={"message_id": message.message_id},
            )
