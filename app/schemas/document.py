from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models import DocumentStatus


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: uuid.UUID
    filename: str = Field(validation_alias="original_filename")
    status: DocumentStatus
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    raw_object_key: str
    processed_object_key: str | None = None
    error_message: str | None = None
    # Additive metadata
    content_type: str
    size_bytes: int
    attempts: int


class DocumentAccepted(BaseModel):
    id: uuid.UUID
    status: DocumentStatus
    filename: str
    status_url: str


class DocumentList(BaseModel):
    items: list[DocumentOut]
    total: int
    limit: int
    offset: int


class JobMessage(BaseModel):
    """The JSON payload placed on the queue by the API and consumed by the worker."""

    version: int = 1
    document_id: uuid.UUID
    raw_object_key: str
    submitted_at: datetime


class Health(BaseModel):
    status: str = "ok"


class QueueDepth(BaseModel):
    visible: int
    in_flight: int
    dead: int


class LatencyStats(BaseModel):
    avg_seconds: float | None
    p95_seconds: float | None
    sample_size: int


class TimelinePoint(BaseModel):
    minute: datetime
    submitted: int
    completed: int


class Stats(BaseModel):
    generated_at: datetime
    total: int
    counts: dict[str, int]
    success_rate: float | None
    processing_failures: int
    latency: LatencyStats
    throughput_per_minute: float
    queue: QueueDepth
    timeline: list[TimelinePoint]
