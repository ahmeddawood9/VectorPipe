"""FastAPI application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from prometheus_client import CollectorRegistry
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app import __version__
from app.api.middleware import ObservabilityMiddleware
from app.api.routes import documents, system
from app.config import Settings, get_settings
from app.db import create_db_engine, create_session_factory
from app.metrics import ApiMetrics, StateCollector
from app.schemas import Stats  # noqa: F401  (re-exported for OpenAPI)
from app.services import LocalObjectStorage, LocalQueue, ObjectStorage, ObjectStorageError, Queue, QueueError
from app.services import documents as repo

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    session_factory: sessionmaker[Session] | None = None,
    storage: ObjectStorage | None = None,
    queue: Queue | None = None,
) -> FastAPI:
    """Build the app. Collaborators default to the real local implementations; tests inject fakes."""
    settings = settings or get_settings()
    engine = None
    if session_factory is None:
        engine = create_db_engine(settings.database_url)
        session_factory = create_session_factory(engine)
    storage = storage or LocalObjectStorage(settings.local_storage_root)
    queue = queue or LocalQueue(
        settings.queue_path,
        visibility_timeout=settings.queue_visibility_timeout_seconds,
        max_receive_count=settings.queue_max_receive_count,
        poll_interval=settings.queue_poll_interval_seconds,
    )

    registry = CollectorRegistry()
    metrics = ApiMetrics(registry)

    def snapshot() -> dict:
        with session_factory() as session:
            counts = repo.status_counts(session)
            failures = repo.failed_attempts(session, counts)
        q = queue.stats()
        return {
            "counts": counts,
            "processing_failures": failures,
            "queue": {"visible": q.visible, "in_flight": q.in_flight, "dead": q.dead},
        }

    registry.register(StateCollector(snapshot))

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        if engine is not None:
            engine.dispose()

    app = FastAPI(
        title="VectorPipe",
        version=__version__,
        description="Distributed document-ingestion prototype: API + background worker.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.storage = storage
    app.state.queue = queue
    app.state.metrics = metrics

    app.add_middleware(ObservabilityMiddleware, metrics=metrics)
    app.include_router(system.router)
    app.include_router(documents.router)

    def _unavailable(message: str):
        async def handler(_: Request, exc: Exception) -> JSONResponse:
            logger.error("dependency failure: %s", message, exc_info=exc)
            return JSONResponse({"detail": message}, status_code=503)

        return handler

    app.add_exception_handler(SQLAlchemyError, _unavailable("database unavailable"))
    app.add_exception_handler(ObjectStorageError, _unavailable("object storage unavailable"))
    app.add_exception_handler(QueueError, _unavailable("queue unavailable"))

    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        logger.error("unhandled exception", exc_info=exc)
        return JSONResponse({"detail": "internal server error"}, status_code=500)

    app.add_exception_handler(Exception, _unhandled)
    return app
