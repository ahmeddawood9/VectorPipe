"""Run a worker:  python -m app.worker"""

from __future__ import annotations

import logging
import signal
import sys

from prometheus_client import start_http_server
from pydantic import ValidationError

from app.config import Settings, configure_logging
from app.db import check_database, create_db_engine, create_session_factory
from app.metrics import WorkerMetrics
from app.services import build_queue, build_storage
from app.worker import JobProcessor, Worker

logger = logging.getLogger("app.worker")


def main() -> int:
    try:
        settings = Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        print(f"Configuration error:\n{exc}", file=sys.stderr)
        return 2

    configure_logging(settings.numeric_log_level, service="worker")

    engine = create_db_engine(settings.database_url)
    try:
        check_database(engine)
    except RuntimeError as exc:
        logger.error(str(exc))
        return 1

    if settings.processing_delay_seconds >= settings.queue_visibility_timeout_seconds:
        logger.warning(
            "PROCESSING_DELAY_SECONDS >= QUEUE_VISIBILITY_TIMEOUT_SECONDS: jobs will be redelivered while "
            "still running (results stay correct, but work is duplicated)"
        )

    try:
        storage = build_storage(settings)
        queue = build_queue(settings)
    except Exception as exc:  # noqa: BLE001 - misconfigured AWS resources should stop the worker cleanly
        logger.error(f"could not initialise storage/queue backends: {exc}")
        return 1
    metrics = WorkerMetrics()
    if settings.worker_metrics_port:
        try:
            start_http_server(settings.worker_metrics_port, registry=metrics.registry)
            logger.info("worker metrics listening", extra={"port": settings.worker_metrics_port})
        except OSError as exc:
            logger.warning(
                "worker metrics disabled (port unavailable); set WORKER_METRICS_PORT per worker",
                extra={"port": settings.worker_metrics_port, "error": str(exc)},
            )

    processor = JobProcessor(
        settings=settings, session_factory=create_session_factory(engine), storage=storage, queue=queue, metrics=metrics
    )
    worker = Worker(queue, processor)

    def _on_signal(signum: int, _frame) -> None:
        name = signal.Signals(signum).name
        if worker.stop_event.is_set():
            logger.warning("second %s received; exiting immediately", name)
            raise SystemExit(1)
        logger.info("%s received; finishing the current job then shutting down", name)
        worker.request_stop()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    logger.info(
        "worker configured",
        extra={
            "storage_backend": settings.storage_backend,
            "queue_backend": settings.queue_backend,
            "storage_root": str(settings.local_storage_root),
            "processing_delay_seconds": settings.processing_delay_seconds,
            "visibility_timeout_seconds": settings.queue_visibility_timeout_seconds,
            "max_receive_count": settings.queue_max_receive_count,
        },
    )
    try:
        worker.run_forever()
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
