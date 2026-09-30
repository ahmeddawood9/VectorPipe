"""Database operations for documents, shared by the API and the worker.

Every state transition is a single conditional ``UPDATE`` so it is atomic under concurrency: when two
workers race for the same document exactly one UPDATE matches and wins.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.orm import Session

from app.db.types import utcnow
from app.models import Document, DocumentStatus

MAX_ERROR_LENGTH = 2000


class ClaimResult(str, enum.Enum):
    CLAIMED = "CLAIMED"  # this worker now owns the job (status -> PROCESSING)
    ALREADY_COMPLETED = "ALREADY_COMPLETED"  # duplicate delivery of a finished job
    IN_PROGRESS = "IN_PROGRESS"  # another worker is actively processing it
    NOT_FOUND = "NOT_FOUND"


@dataclass(frozen=True, slots=True)
class Claim:
    result: ClaimResult
    document: Document | None = None


def _reload(session: Session, document_id: uuid.UUID) -> Document | None:
    return session.scalar(
        select(Document).where(Document.id == document_id).execution_options(populate_existing=True)
    )


def create_document(
    session: Session,
    *,
    document_id: uuid.UUID,
    filename: str,
    content_type: str,
    size_bytes: int,
    raw_object_key: str,
) -> Document:
    now = utcnow()
    doc = Document(
        id=document_id,
        original_filename=filename,
        content_type=content_type,
        size_bytes=size_bytes,
        raw_object_key=raw_object_key,
        status=DocumentStatus.PENDING,
        attempts=0,
        created_at=now,
        updated_at=now,
    )
    session.add(doc)
    session.commit()
    return doc


def get_document(session: Session, document_id: uuid.UUID) -> Document | None:
    return _reload(session, document_id)


def delete_document(session: Session, document_id: uuid.UUID) -> None:
    """Compensation helper used when a submission fails after the row was created."""
    session.execute(delete(Document).where(Document.id == document_id))
    session.commit()


def list_documents(
    session: Session,
    *,
    status: DocumentStatus | None = None,
    query: str | None = None,
    limit: int = 25,
    offset: int = 0,
) -> tuple[list[Document], int]:
    conditions = []
    if status is not None:
        conditions.append(Document.status == status)
    if query:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conditions.append(Document.original_filename.ilike(f"%{escaped}%", escape="\\"))
    total = session.scalar(select(func.count()).select_from(Document).where(*conditions)) or 0
    rows = session.scalars(
        select(Document)
        .where(*conditions)
        .order_by(Document.created_at.desc(), Document.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return list(rows), int(total)


def claim_document(session: Session, document_id: uuid.UUID, *, stale_after_seconds: float) -> Claim:
    """Atomically move a job to PROCESSING.

    Claimable: PENDING (first delivery or waiting for retry), FAILED (manual redrive), and PROCESSING
    rows that have not been touched for ``stale_after_seconds`` (the worker that owned it died).
    """
    now = utcnow()
    stale_before = now - timedelta(seconds=stale_after_seconds)
    result = session.execute(
        update(Document)
        .where(
            Document.id == document_id,
            or_(
                Document.status.in_([DocumentStatus.PENDING, DocumentStatus.FAILED]),
                and_(Document.status == DocumentStatus.PROCESSING, Document.updated_at < stale_before),
            ),
        )
        .values(status=DocumentStatus.PROCESSING, attempts=Document.attempts + 1, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    session.commit()
    doc = _reload(session, document_id)
    if result.rowcount == 1:
        return Claim(ClaimResult.CLAIMED, doc)
    if doc is None:
        return Claim(ClaimResult.NOT_FOUND)
    if doc.status == DocumentStatus.COMPLETED:
        return Claim(ClaimResult.ALREADY_COMPLETED, doc)
    return Claim(ClaimResult.IN_PROGRESS, doc)


def mark_completed(session: Session, document_id: uuid.UUID, processed_object_key: str) -> bool:
    """Record success. Safe to repeat; returns True if the document is COMPLETED afterwards.

    The result object is deterministic, so completing from any non-COMPLETED state is correct even if
    another worker meanwhile reclaimed or failed the job.
    """
    now = utcnow()
    result = session.execute(
        update(Document)
        .where(Document.id == document_id, Document.status != DocumentStatus.COMPLETED)
        .values(
            status=DocumentStatus.COMPLETED,
            processed_object_key=processed_object_key,
            error_message=None,
            completed_at=now,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    session.commit()
    if result.rowcount == 1:
        return True
    doc = _reload(session, document_id)
    return doc is not None and doc.status == DocumentStatus.COMPLETED


def mark_attempt_failed(session: Session, document_id: uuid.UUID, error: str, *, final: bool) -> bool:
    """Record a failed attempt.

    Final failures become FAILED (terminal). Otherwise the job goes back to PENDING (awaiting redelivery)
    with the last error attached. Only the current owner (status PROCESSING) may do this, so a slow
    worker can never overwrite a result another worker has already produced.
    """
    result = session.execute(
        update(Document)
        .where(Document.id == document_id, Document.status == DocumentStatus.PROCESSING)
        .values(
            status=DocumentStatus.FAILED if final else DocumentStatus.PENDING,
            error_message=error[:MAX_ERROR_LENGTH],
            updated_at=utcnow(),
        )
        .execution_options(synchronize_session=False)
    )
    session.commit()
    return result.rowcount == 1


def status_counts(session: Session) -> dict[DocumentStatus, int]:
    counts = {status: 0 for status in DocumentStatus}
    for status, n in session.execute(select(Document.status, func.count()).group_by(Document.status)):
        counts[status] = int(n)
    return counts


def failed_attempts(session: Session, counts: dict[DocumentStatus, int] | None = None) -> int:
    """Attempts that did not end in success, derived durably from the database.

    Every claim increments ``attempts``; a document that is COMPLETED or currently PROCESSING accounts
    for exactly one non-failed attempt, every other attempt failed.
    """
    counts = counts or status_counts(session)
    total_attempts = int(session.scalar(select(func.coalesce(func.sum(Document.attempts), 0))) or 0)
    return max(0, total_attempts - counts[DocumentStatus.COMPLETED] - counts[DocumentStatus.PROCESSING])
