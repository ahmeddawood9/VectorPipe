"""Application configuration, loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_LOG_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}


def normalize_database_url(url: str) -> str:
    """Accept plain ``postgresql://`` URLs and route them to the psycopg 3 driver."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Required / documented configuration -------------------------------------------------
    database_url: str = Field(description="PostgreSQL SQLAlchemy URL.")
    local_storage_root: Path = Field(default=Path("./storage"))
    processing_delay_seconds: float = Field(default=3.0, ge=0)
    log_level: str = Field(default="INFO")
    queue_poll_interval_seconds: float = Field(default=0.5, gt=0)

    # --- Optional tuning (all have safe defaults) --------------------------------------------
    queue_visibility_timeout_seconds: float = Field(default=60.0, gt=0)
    queue_max_receive_count: int = Field(default=3, ge=1)
    retry_backoff_seconds: float = Field(default=2.0, ge=0)
    max_upload_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    api_host: str = Field(default="127.0.0.1")
    api_port: int = Field(default=8000, ge=1, le=65535)
    worker_metrics_port: int = Field(default=9101, ge=0, le=65535)
    simulate_failure_rate: float = Field(default=0.0, ge=0.0, le=1.0)

    # --- Backends: local (default, no AWS needed) or AWS ---------------------------------------
    storage_backend: Literal["local", "s3"] = "local"
    queue_backend: Literal["local", "sqs"] = "local"
    aws_region: str | None = None
    s3_bucket: str | None = None
    sqs_queue_url: str | None = None
    sqs_dlq_url: str | None = None

    @field_validator("database_url")
    @classmethod
    def _normalize_url(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("DATABASE_URL must not be empty")
        return normalize_database_url(value)

    @field_validator("log_level")
    @classmethod
    def _validate_level(cls, value: str) -> str:
        level = value.strip().upper()
        if level not in _LOG_LEVELS:
            raise ValueError(f"LOG_LEVEL must be one of {sorted(_LOG_LEVELS)}")
        return level

    @model_validator(mode="after")
    def _require_backend_settings(self) -> "Settings":
        missing: list[str] = []
        if self.storage_backend == "s3" and not self.s3_bucket:
            missing.append("S3_BUCKET (required when STORAGE_BACKEND=s3)")
        if self.queue_backend == "sqs":
            for name, value in (("SQS_QUEUE_URL", self.sqs_queue_url), ("SQS_DLQ_URL", self.sqs_dlq_url)):
                if not value:
                    missing.append(f"{name} (required when QUEUE_BACKEND=sqs)")
        if (self.storage_backend == "s3" or self.queue_backend == "sqs") and not self.aws_region:
            missing.append("AWS_REGION (required when using S3 or SQS)")
        if missing:
            raise ValueError("; ".join(missing))
        return self

    @property
    def numeric_log_level(self) -> int:
        return getattr(logging, self.log_level)

    @property
    def processing_stale_after_seconds(self) -> float:
        """A PROCESSING row untouched for this long is treated as abandoned by a dead worker.

        Deliberately below the visibility timeout: the message of a crashed worker is redelivered right
        after the timeout, and by then the row must already count as stale (otherwise the redelivery would
        be wasted and burn one of the message's limited deliveries).
        """
        return self.queue_visibility_timeout_seconds * 0.8

    @property
    def queue_path(self) -> Path:
        return self.local_storage_root / "queue" / "queue.sqlite3"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # values come from the environment
