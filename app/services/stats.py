"""Aggregations for the dashboard (`GET /stats`)."""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.types import utcnow
from app.models import Document, DocumentStatus
from app.services.documents import failed_attempts, status_counts
from app.services.queue import Queue

TIMELINE_MINUTES = 30
_SAMPLE_LIMIT = 20_000
_LATENCY_SAMPLE = 200


def _minute(ts: datetime) -> datetime:
    return ts.replace(second=0, microsecond=0)


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def collect_stats(session: Session, queue: Queue, *, now: datetime | None = None) -> dict:
    now = now or utcnow()
    counts = status_counts(session)
    total = sum(counts.values())

    first_bucket = _minute(now) - timedelta(minutes=TIMELINE_MINUTES - 1)
    submitted = dict.fromkeys((first_bucket + timedelta(minutes=i) for i in range(TIMELINE_MINUTES)), 0)
    completed = dict(submitted)

    for (created_at,) in session.execute(
        select(Document.created_at).where(Document.created_at >= first_bucket).limit(_SAMPLE_LIMIT)
    ):
        bucket = _minute(created_at)
        if bucket in submitted:
            submitted[bucket] += 1
    for (completed_at,) in session.execute(
        select(Document.completed_at).where(Document.completed_at >= first_bucket).limit(_SAMPLE_LIMIT)
    ):
        bucket = _minute(completed_at)
        if bucket in completed:
            completed[bucket] += 1

    latencies = [
        (done - created).total_seconds()
        for created, done in session.execute(
            select(Document.created_at, Document.completed_at)
            .where(Document.status == DocumentStatus.COMPLETED, Document.completed_at.is_not(None))
            .order_by(Document.completed_at.desc())
            .limit(_LATENCY_SAMPLE)
        )
    ]

    # Completions in the current minute plus the previous four, averaged per minute.
    window_start = _minute(now) - timedelta(minutes=4)
    recent_completions = sum(n for bucket, n in completed.items() if bucket >= window_start)

    finished = counts[DocumentStatus.COMPLETED] + counts[DocumentStatus.FAILED]
    q = queue.stats()
    return {
        "generated_at": now,
        "total": total,
        "counts": {status.value: n for status, n in counts.items()},
        "success_rate": (counts[DocumentStatus.COMPLETED] / finished) if finished else None,
        "processing_failures": failed_attempts(session, counts),
        "latency": {
            "avg_seconds": (sum(latencies) / len(latencies)) if latencies else None,
            "p95_seconds": _p95(latencies),
            "sample_size": len(latencies),
        },
        "throughput_per_minute": recent_completions / 5,
        "queue": {"visible": q.visible, "in_flight": q.in_flight, "dead": q.dead},
        "timeline": [
            {"minute": bucket, "submitted": submitted[bucket], "completed": completed[bucket]}
            for bucket in sorted(submitted)
        ],
    }
