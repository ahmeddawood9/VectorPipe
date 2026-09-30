from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, Enum, Index, Integer, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import UTCDateTime, utcnow


class DocumentStatus(str, enum.Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class Document(Base):
    """One ingested document == one processing job."""

    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    raw_object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    processed_object_key: Mapped[str | None] = mapped_column(String(512))
    status: Mapped[DocumentStatus] = mapped_column(
        Enum(DocumentStatus, native_enum=False, length=16, create_constraint=False, validate_strings=True),
        nullable=False,
        default=DocumentStatus.PENDING,
        server_default=DocumentStatus.PENDING.value,
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utcnow, onupdate=utcnow, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    # Additive metadata (not in the minimal spec, but useful for the API/dashboard and retries).
    content_type: Mapped[str] = mapped_column(
        String(255), nullable=False, default="application/octet-stream", server_default="application/octet-stream"
    )
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    __table_args__ = (
        CheckConstraint("status IN ('PENDING','PROCESSING','COMPLETED','FAILED')", name="status_valid"),
        # Listing/filtering by status, newest first; also serves the worker's status lookups.
        Index("ix_documents_status_created_at", "status", "created_at"),
        Index("ix_documents_created_at", "created_at"),
        # Throughput / latency queries over recently completed documents.
        Index("ix_documents_completed_at", "completed_at"),
    )
