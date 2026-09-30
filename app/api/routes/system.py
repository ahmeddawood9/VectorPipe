from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import FileResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy.orm import Session

from app.api.deps import get_queue, get_session
from app.schemas import Health, Stats
from app.services import Queue
from app.services.stats import collect_stats

router = APIRouter()
_DASHBOARD = Path(__file__).resolve().parent.parent / "static" / "dashboard.html"


@router.get("/health", response_model=Health, tags=["system"])
def health() -> Health:
    return Health()


@router.get("/metrics", tags=["system"], response_class=Response)
def metrics(request: Request) -> Response:
    return Response(generate_latest(request.app.state.metrics.registry), media_type=CONTENT_TYPE_LATEST)


@router.get("/stats", response_model=Stats, tags=["system"])
def stats(session: Annotated[Session, Depends(get_session)], queue: Annotated[Queue, Depends(get_queue)]) -> Stats:
    return Stats.model_validate(collect_stats(session, queue))


@router.get("/", include_in_schema=False)
def dashboard() -> FileResponse:
    return FileResponse(_DASHBOARD, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-cache"})
