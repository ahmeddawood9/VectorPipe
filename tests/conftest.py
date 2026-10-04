from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.api.main import create_app
from app.config import Settings
from app.config.settings import normalize_database_url
from app.db import Base, create_db_engine, create_session_factory
from app.metrics import WorkerMetrics
from app.models import Document
from app.services import LocalObjectStorage, LocalQueue
from app.worker import JobProcessor, Worker
from tests.helpers import FlakyQueue, FlakyStorage

# Backend/AWS settings tests must choose explicitly; never inherit them from the shell or CI.
_AMBIENT_SETTINGS = (
    "STORAGE_BACKEND",
    "QUEUE_BACKEND",
    "AWS_REGION",
    "AWS_PROFILE",
    "S3_BUCKET",
    "SQS_QUEUE_URL",
    "SQS_DLQ_URL",
)


@pytest.fixture(autouse=True)
def _isolate_from_ambient_settings(monkeypatch):
    """CI sets AWS_REGION for the whole workflow; a developer may have AWS_PROFILE exported."""
    for name in _AMBIENT_SETTINGS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(scope="session")
def database_url(tmp_path_factory) -> str:
    """SQLite file by default (no external services needed); set TEST_DATABASE_URL to run on PostgreSQL."""
    configured = os.environ.get("TEST_DATABASE_URL")
    if configured:
        return normalize_database_url(configured)
    return f"sqlite:///{tmp_path_factory.mktemp('db') / 'test.sqlite3'}"


@pytest.fixture(scope="session")
def engine(database_url):
    eng = create_db_engine(database_url)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session_factory(engine):
    factory = create_session_factory(engine)
    with factory() as s:
        s.execute(delete(Document))
        s.commit()
    return factory


@pytest.fixture
def settings(tmp_path, database_url) -> Settings:
    return Settings(
        _env_file=None,
        database_url=database_url,
        local_storage_root=tmp_path / "storage",
        processing_delay_seconds=0,
        queue_poll_interval_seconds=0.02,
        queue_visibility_timeout_seconds=30,
        queue_max_receive_count=3,
        retry_backoff_seconds=0,
        worker_metrics_port=0,
        log_level="DEBUG",
    )


@pytest.fixture
def real_storage(settings) -> LocalObjectStorage:
    return LocalObjectStorage(settings.local_storage_root)


@pytest.fixture
def storage(real_storage) -> FlakyStorage:
    return FlakyStorage(real_storage)


@pytest.fixture
def real_queue(settings) -> LocalQueue:
    return LocalQueue(
        settings.queue_path,
        visibility_timeout=settings.queue_visibility_timeout_seconds,
        max_receive_count=settings.queue_max_receive_count,
        poll_interval=settings.queue_poll_interval_seconds,
    )


@pytest.fixture
def queue(real_queue) -> FlakyQueue:
    return FlakyQueue(real_queue)


@pytest.fixture
def app(settings, session_factory, storage, queue):
    return create_app(settings, session_factory=session_factory, storage=storage, queue=queue)


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def make_processor(settings, session_factory, storage, queue):
    def factory(**overrides) -> JobProcessor:
        kwargs = dict(
            settings=settings,
            session_factory=session_factory,
            storage=storage,
            queue=queue,
            metrics=WorkerMetrics(),
        )
        kwargs.update(overrides)
        return JobProcessor(**kwargs)

    return factory


@pytest.fixture
def processor(make_processor) -> JobProcessor:
    return make_processor()


@pytest.fixture
def worker(queue, processor) -> Worker:
    return Worker(queue, processor)
