from __future__ import annotations

import logging
import re
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile, status
from sqlalchemy.orm import Session

from app.api.deps import get_metrics, get_queue, get_session, get_settings_dep, get_storage
from app.config import Settings
from app.db.types import utcnow
from app.metrics import ApiMetrics
from app.models import DocumentStatus
from app.schemas import DocumentAccepted, DocumentList, DocumentOut, JobMessage
from app.services import ObjectNotFoundError, ObjectStorage, Queue
from app.services import documents as repo

logger = logging.getLogger(__name__)
router = APIRouter(tags=["documents"])

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_READ_CHUNK = 1024 * 1024


def sanitize_filename(raw: str | None) -> str:
    """Keep only the final path component; strip control characters; cap the length."""
    name = re.split(r"[\\/]", raw or "")[-1]
    name = _CONTROL_CHARS.sub("", name).strip()
    return name[:255]


def read_limited(upload: UploadFile, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while chunk := upload.file.read(_READ_CHUNK):
        total += len(chunk)
        if total > limit:
            raise HTTPException(
                status.HTTP_413_CONTENT_TOO_LARGE, f"file exceeds the {limit}-byte upload limit"
            )
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/documents", status_code=status.HTTP_202_ACCEPTED, response_model=DocumentAccepted)
def submit_document(
    request: Request,
    response: Response,
    file: Annotated[UploadFile, File(description="The document to ingest.")],
    session: Annotated[Session, Depends(get_session)],
    storage: Annotated[ObjectStorage, Depends(get_storage)],
    queue: Annotated[Queue, Depends(get_queue)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
    metrics: Annotated[ApiMetrics, Depends(get_metrics)],
) -> DocumentAccepted:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > settings.max_upload_bytes + 64 * 1024:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "request body too large")

    filename = sanitize_filename(file.filename)
    if not filename:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "the uploaded file must have a filename")
    data = read_limited(file, settings.max_upload_bytes)
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "the uploaded file is empty")

    document_id = uuid.uuid4()
    raw_key = f"raw/{document_id}"

    # 1. raw document -> object storage
    storage.put_object(raw_key, data)

    # 2. PENDING row in PostgreSQL (compensate by removing the object if this fails)
    try:
        repo.create_document(
            session,
            document_id=document_id,
            filename=filename,
            content_type=(file.content_type or "application/octet-stream")[:255],
            size_bytes=len(data),
            raw_object_key=raw_key,
        )
    except Exception:
        session.rollback()
        _best_effort_cleanup(storage, raw_key)
        raise

    # 3. job -> queue (compensate by removing both the row and the object if this fails)
    job = JobMessage(document_id=document_id, raw_object_key=raw_key, submitted_at=utcnow())
    try:
        queue.enqueue(job.model_dump_json())
    except Exception:
        logger.exception("enqueue failed; rolling back submission", extra={"document_id": str(document_id)})
        try:
            repo.delete_document(session, document_id)
        except Exception:  # noqa: BLE001
            session.rollback()
            logger.exception("could not remove document row after enqueue failure")
        _best_effort_cleanup(storage, raw_key)
        raise

    metrics.documents_submitted.inc()
    logger.info(
        "document accepted",
        extra={"document_id": str(document_id), "doc_filename": filename, "size_bytes": len(data)},
    )
    status_url = f"/documents/{document_id}"
    response.headers["Location"] = status_url
    return DocumentAccepted(id=document_id, status=DocumentStatus.PENDING, filename=filename, status_url=status_url)


def _best_effort_cleanup(storage: ObjectStorage, key: str) -> None:
    try:
        storage.delete_object(key)
    except Exception:  # noqa: BLE001
        logger.exception("could not remove orphaned object", extra={"key": key})


@router.get("/documents", response_model=DocumentList)
def list_documents(
    session: Annotated[Session, Depends(get_session)],
    status_filter: Annotated[DocumentStatus | None, Query(alias="status")] = None,
    q: Annotated[str | None, Query(max_length=100, description="Filename contains")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> DocumentList:
    items, total = repo.list_documents(session, status=status_filter, query=q, limit=limit, offset=offset)
    return DocumentList(
        items=[DocumentOut.model_validate(d) for d in items], total=total, limit=limit, offset=offset
    )


@router.get("/documents/{document_id}", response_model=DocumentOut)
def get_document(document_id: uuid.UUID, session: Annotated[Session, Depends(get_session)]) -> DocumentOut:
    doc = repo.get_document(session, document_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    return DocumentOut.model_validate(doc)


@router.get("/documents/{document_id}/result", responses={409: {"description": "Not completed yet"}})
def get_result(
    document_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
    storage: Annotated[ObjectStorage, Depends(get_storage)],
) -> Response:
    doc = repo.get_document(session, document_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    if doc.status != DocumentStatus.COMPLETED or not doc.processed_object_key:
        raise HTTPException(status.HTTP_409_CONFLICT, f"document is {doc.status.value}, result not available")
    try:
        payload = storage.get_object(doc.processed_object_key)
    except ObjectNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "processed result is missing from object storage") from None
    return Response(content=payload, media_type="application/json")
