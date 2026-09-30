from __future__ import annotations

from collections.abc import Iterator

from fastapi import Request
from sqlalchemy.orm import Session

from app.config import Settings
from app.metrics import ApiMetrics
from app.services import ObjectStorage, Queue


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_session(request: Request) -> Iterator[Session]:
    session: Session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()


def get_storage(request: Request) -> ObjectStorage:
    return request.app.state.storage


def get_queue(request: Request) -> Queue:
    return request.app.state.queue


def get_metrics(request: Request) -> ApiMetrics:
    return request.app.state.metrics
