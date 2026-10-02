"""Build the storage and queue implementations selected by the settings."""

from __future__ import annotations

from app.config import Settings
from app.services.object_storage import LocalObjectStorage, ObjectStorage, S3ObjectStorage
from app.services.queue import LocalQueue, Queue, SqsQueue

# /stats and /metrics both call queue.stats(); two GetQueueAttributes calls per scrape add up, so cache briefly.
_SQS_STATS_CACHE_SECONDS = 5.0


def build_storage(settings: Settings) -> ObjectStorage:
    if settings.storage_backend == "s3":
        assert settings.s3_bucket  # guaranteed by Settings validation
        return S3ObjectStorage(settings.s3_bucket, region=settings.aws_region)
    return LocalObjectStorage(settings.local_storage_root)


def build_queue(settings: Settings) -> Queue:
    if settings.queue_backend == "sqs":
        assert settings.sqs_queue_url and settings.sqs_dlq_url  # guaranteed by Settings validation
        return SqsQueue(
            settings.sqs_queue_url,
            settings.sqs_dlq_url,
            region=settings.aws_region,
            visibility_timeout=settings.queue_visibility_timeout_seconds,
            max_receive_count=settings.queue_max_receive_count,
            stats_cache_seconds=_SQS_STATS_CACHE_SECONDS,
        )
    return LocalQueue(
        settings.queue_path,
        visibility_timeout=settings.queue_visibility_timeout_seconds,
        max_receive_count=settings.queue_max_receive_count,
        poll_interval=settings.queue_poll_interval_seconds,
    )
