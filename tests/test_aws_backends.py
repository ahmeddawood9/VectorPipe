"""S3ObjectStorage / SqsQueue against moto, plus the settings that pick the backends.

Moto does not enforce IAM, so permission gaps (e.g. GetQueueAttributes on the DLQ) only show up in a real
AWS smoke test. Redelivery is driven with change_visibility(0) rather than sleeping, to keep the suite fast.
"""

import json
import threading
import time
import uuid

import boto3
import pytest
from moto import mock_aws
from pydantic import ValidationError
from sqlalchemy import select

from app.config import Settings
from app.models import Document, DocumentStatus
from app.services import (
    InvalidObjectKeyError,
    LocalObjectStorage,
    LocalQueue,
    ObjectNotFoundError,
    ObjectStorage,
    ObjectStorageError,
    Queue,
    QueueError,
    S3ObjectStorage,
    SqsQueue,
    build_queue,
    build_storage,
)
from app.worker import Outcome
from app.worker.embedding import generate_embeddings, serialize
from tests.helpers import step, upload

REGION = "us-east-1"
BUCKET = "vectorpipe-test"


@pytest.fixture(autouse=True)
def aws_env(monkeypatch):
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(name, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        yield


@pytest.fixture
def s3_store() -> S3ObjectStorage:
    boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
    return S3ObjectStorage(BUCKET, region=REGION)


@pytest.fixture
def sqs_urls() -> tuple[str, str]:
    sqs = boto3.client("sqs", region_name=REGION)
    dlq = sqs.create_queue(QueueName="vectorpipe-dlq")["QueueUrl"]
    dlq_arn = sqs.get_queue_attributes(QueueUrl=dlq, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    main = sqs.create_queue(
        QueueName="vectorpipe",
        Attributes={
            "VisibilityTimeout": "30",
            "RedrivePolicy": json.dumps({"deadLetterTargetArn": dlq_arn, "maxReceiveCount": "3"}),
        },
    )["QueueUrl"]
    return main, dlq


@pytest.fixture
def sqs_queue(sqs_urls) -> SqsQueue:
    main, dlq = sqs_urls
    return SqsQueue(main, dlq, region=REGION, visibility_timeout=30, max_receive_count=3)


@pytest.fixture(params=["local", "s3"])
def store(request, tmp_path) -> ObjectStorage:
    if request.param == "local":
        return LocalObjectStorage(tmp_path / "objects")
    return request.getfixturevalue("s3_store")


@pytest.fixture(params=["local", "sqs"])
def q(request, tmp_path) -> Queue:
    if request.param == "local":
        return LocalQueue(tmp_path / "q" / "queue.sqlite3", visibility_timeout=30, max_receive_count=3)
    return request.getfixturevalue("sqs_queue")


# ------------------------------------------------------------------ object storage: same contract on both
def test_storage_roundtrip_overwrite_and_empty(store):
    assert isinstance(store, ObjectStorage)
    store.put_object("raw/abc", b"\x00binary\xffdata")
    assert store.get_object("raw/abc") == b"\x00binary\xffdata"
    store.put_object("raw/abc", b"two")
    assert store.get_object("raw/abc") == b"two"
    store.put_object("raw/empty", b"")
    assert store.get_object("raw/empty") == b""


def test_storage_missing_key_is_not_found(store):
    with pytest.raises(ObjectNotFoundError):
        store.get_object("raw/missing")


def test_storage_delete_is_idempotent(store):
    store.put_object("raw/k", b"x")
    store.delete_object("raw/k")
    store.delete_object("raw/k")
    with pytest.raises(ObjectNotFoundError):
        store.get_object("raw/k")


def test_storage_rejects_non_bytes(store):
    with pytest.raises(TypeError):
        store.put_object("raw/k", "text")  # type: ignore[arg-type]


@pytest.mark.parametrize("key", ["", "/etc/passwd", "../outside", "a//b", "raw/.hidden", "a b", "x" * 600])
def test_storage_rejects_unsafe_keys(store, key):
    for call in (lambda: store.put_object(key, b"x"), lambda: store.get_object(key), lambda: store.delete_object(key)):
        with pytest.raises(InvalidObjectKeyError):
            call()


# ------------------------------------------------------------------ S3 specifics
def test_s3_objects_are_encrypted_at_rest(s3_store):
    s3_store.put_object("raw/k", b"x")
    head = boto3.client("s3", region_name=REGION).head_object(Bucket=BUCKET, Key="raw/k")
    assert head["ServerSideEncryption"] == "AES256"


def test_s3_failures_surface_as_object_storage_error():
    store = S3ObjectStorage("bucket-that-does-not-exist", region=REGION)
    with pytest.raises(ObjectStorageError) as exc:
        store.put_object("raw/k", b"x")
    assert not isinstance(exc.value, ObjectNotFoundError)
    with pytest.raises(ObjectStorageError):
        store.get_object("raw/k")  # NoSuchBucket is an error, not "not found"


def test_s3_requires_a_bucket():
    with pytest.raises(ValueError):
        S3ObjectStorage("")


# ------------------------------------------------------------------ queue: same contract on both
def test_queue_enqueue_receive_delete(q):
    assert isinstance(q, Queue) and q.max_receive_count == 3
    assert q.receive() == []
    mid = q.enqueue(json.dumps({"a": 1}))
    [msg] = q.receive()
    assert msg.message_id == mid
    assert json.loads(msg.body) == {"a": 1}
    assert msg.receive_count == 1 and msg.receipt_handle and msg.enqueued_at > 0
    assert q.receive() == []  # in flight: hidden
    assert q.delete(msg.receipt_handle) is True
    stats = q.stats()
    assert (stats.visible, stats.in_flight, stats.dead) == (0, 0, 0)


def test_queue_stats_count_visible_and_in_flight(q):
    q.enqueue("a")
    q.enqueue("b")
    q.receive()
    stats = q.stats()
    assert (stats.visible, stats.in_flight) == (1, 1)


def test_queue_change_visibility_zero_redelivers_with_higher_count(q):
    q.enqueue("m")
    [first] = q.receive()
    assert q.change_visibility(first.receipt_handle, 0) is True
    [second] = q.receive()
    assert second.message_id == first.message_id
    assert second.receive_count == 2
    assert second.receipt_handle != first.receipt_handle


def test_queue_dead_letters_after_max_receive_count(q):
    q.enqueue("poison")
    for attempt in (1, 2, 3):
        [msg] = q.receive()
        assert msg.receive_count == attempt
        q.change_visibility(msg.receipt_handle, 0)
    assert q.receive() == []  # a 4th delivery never happens
    assert q.stats().dead == 1


def test_queue_stop_event_ends_long_poll_promptly(q):
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    started = time.monotonic()
    assert q.receive(wait_seconds=15, stop_event=stop) == []
    assert time.monotonic() - started < 5


# ------------------------------------------------------------------ SQS specifics
def test_sqs_receive_batch_is_capped_at_ten(sqs_queue):
    for i in range(12):
        sqs_queue.enqueue(str(i))
    assert len(sqs_queue.receive(max_messages=50)) == 10


def test_sqs_long_poll_returns_as_soon_as_a_message_arrives(sqs_queue):
    threading.Timer(0.3, lambda: sqs_queue.enqueue("late")).start()
    started = time.monotonic()
    [msg] = sqs_queue.receive(wait_seconds=10)
    assert msg.body == "late"
    assert time.monotonic() - started < 5


def test_sqs_delete_with_a_garbage_handle_is_false_not_an_error(sqs_queue):
    assert sqs_queue.delete("not-a-real-handle") is False
    assert sqs_queue.change_visibility("not-a-real-handle", 5) is False


def test_sqs_errors_surface_as_queue_error(sqs_urls):
    main, dlq = sqs_urls
    q = SqsQueue(main + "-gone", dlq, region=REGION, verify_redrive=False)
    with pytest.raises(QueueError):
        q.enqueue("x")
    with pytest.raises(QueueError):
        q.receive()
    with pytest.raises(QueueError):
        q.stats()


def test_sqs_refuses_a_queue_without_a_redrive_policy():
    sqs = boto3.client("sqs", region_name=REGION)
    plain = sqs.create_queue(QueueName="plain")["QueueUrl"]
    dlq = sqs.create_queue(QueueName="some-dlq")["QueueUrl"]
    with pytest.raises(QueueError, match="redrive"):
        SqsQueue(plain, dlq, region=REGION)


def test_sqs_warns_when_max_receive_count_disagrees_with_the_redrive_policy(sqs_urls, caplog):
    main, dlq = sqs_urls
    SqsQueue(main, dlq, region=REGION, max_receive_count=5)
    assert "does not match" in caplog.text


def test_sqs_stats_can_be_cached(sqs_urls):
    main, dlq = sqs_urls
    q = SqsQueue(main, dlq, region=REGION, stats_cache_seconds=60)
    assert q.stats().visible == 0
    q.enqueue("m")
    assert q.stats().visible == 0  # served from the cache
    assert SqsQueue(main, dlq, region=REGION).stats().visible == 1


# ------------------------------------------------------------------ settings and factory
def _settings(**kw) -> Settings:
    return Settings(_env_file=None, database_url="sqlite://", **kw)


def test_defaults_stay_local(tmp_path):
    s = _settings(local_storage_root=tmp_path)
    assert (s.storage_backend, s.queue_backend) == ("local", "local")
    assert isinstance(build_storage(s), LocalObjectStorage)
    assert isinstance(build_queue(s), LocalQueue)


def test_aws_backends_are_built_from_settings(sqs_urls, s3_store):
    main, dlq = sqs_urls
    s = _settings(
        storage_backend="s3", queue_backend="sqs", aws_region=REGION, s3_bucket=BUCKET, sqs_queue_url=main, sqs_dlq_url=dlq
    )
    assert isinstance(build_storage(s), S3ObjectStorage)
    assert isinstance(build_queue(s), SqsQueue)


@pytest.mark.parametrize(
    "kw, missing",
    [
        ({"storage_backend": "s3", "aws_region": REGION}, "S3_BUCKET"),
        ({"storage_backend": "s3", "s3_bucket": "b"}, "AWS_REGION"),
        ({"queue_backend": "sqs", "aws_region": REGION, "sqs_dlq_url": "u"}, "SQS_QUEUE_URL"),
        ({"queue_backend": "sqs", "aws_region": REGION, "sqs_queue_url": "u"}, "SQS_DLQ_URL"),
    ],
)
def test_incomplete_aws_settings_are_rejected(kw, missing):
    with pytest.raises(ValidationError, match=missing):
        _settings(**kw)


def test_unknown_backend_is_rejected():
    with pytest.raises(ValidationError):
        _settings(queue_backend="rabbitmq")


# ------------------------------------------------------------------ whole pipeline on S3 + SQS
@pytest.fixture
def aws_pipeline(settings, session_factory, sqs_queue, s3_store, make_processor):
    from fastapi.testclient import TestClient

    from app.api.main import create_app

    app = create_app(settings, session_factory=session_factory, storage=s3_store, queue=sqs_queue)
    return TestClient(app), make_processor(storage=s3_store, queue=sqs_queue), sqs_queue, s3_store


def _doc(session_factory, doc_id) -> Document:
    with session_factory() as s:
        return s.scalar(select(Document).where(Document.id == uuid.UUID(doc_id)))


def test_full_flow_on_s3_and_sqs(aws_pipeline, session_factory):
    client, processor, queue, store = aws_pipeline
    data = b"the quick brown fox " * 400
    doc_id = upload(client, "doc.txt", data).json()["id"]
    assert queue.stats().visible == 1

    assert step(queue, processor) is Outcome.COMPLETED

    doc = _doc(session_factory, doc_id)
    assert doc.status == DocumentStatus.COMPLETED
    assert store.get_object(doc.raw_object_key) == data
    assert store.get_object(doc.processed_object_key) == serialize(
        generate_embeddings(document_id=doc_id, filename="doc.txt", data=data)
    )
    stats = queue.stats()
    assert (stats.visible, stats.in_flight, stats.dead) == (0, 0, 0)
    assert client.get(f"/documents/{doc_id}/result").status_code == 200


def test_stale_handle_redelivery_is_harmless_because_the_claim_is_atomic(aws_pipeline, session_factory):
    """SQS can accept a delete with an old receipt handle without removing the message, so the same job can
    be handled twice. The conditional-UPDATE claim must make that a no-op the second time."""
    client, processor, queue, store = aws_pipeline
    doc_id = upload(client, "doc.txt", b"payload " * 100).json()["id"]

    [first] = queue.receive()
    queue.change_visibility(first.receipt_handle, 0)  # lets the message be delivered again
    [second] = queue.receive()
    assert second.message_id == first.message_id and second.receipt_handle != first.receipt_handle

    assert processor.handle(first) is Outcome.COMPLETED  # the stale delivery does the work...
    assert processor.handle(second) is Outcome.DUPLICATE  # ...the current one finds it done

    doc = _doc(session_factory, doc_id)
    assert doc.status == DocumentStatus.COMPLETED
    assert doc.attempts == 1 and doc.error_message is None
    assert store.get_object(doc.processed_object_key) == serialize(
        generate_embeddings(document_id=doc_id, filename="doc.txt", data=b"payload " * 100)
    )


# ------------------------------------------------------------------ AWS profile (assume-role) plumbing
def test_profile_is_passed_through_to_boto3(monkeypatch, sqs_urls, s3_store):
    seen: list[str | None] = []
    real_session = boto3.Session

    def spy(*args, **kwargs):
        seen.append(kwargs.get("profile_name"))
        kwargs["profile_name"] = None  # the profile does not exist here; keep moto's fake credentials
        return real_session(*args, **kwargs)

    monkeypatch.setattr(boto3, "Session", spy)
    main, dlq = sqs_urls
    s = _settings(
        storage_backend="s3", queue_backend="sqs", aws_region=REGION, aws_profile="vectorpipe-dev",
        s3_bucket=BUCKET, sqs_queue_url=main, sqs_dlq_url=dlq,
    )
    build_storage(s)
    build_queue(s)
    assert seen == ["vectorpipe-dev", "vectorpipe-dev"]


def test_no_profile_means_boto3_default_chain(monkeypatch, sqs_urls):
    seen: list[str | None] = []
    real_session = boto3.Session

    def spy(*args, **kwargs):
        seen.append(kwargs.get("profile_name"))
        return real_session(*args, **kwargs)

    monkeypatch.setattr(boto3, "Session", spy)
    main, dlq = sqs_urls
    build_queue(_settings(queue_backend="sqs", aws_region=REGION, sqs_queue_url=main, sqs_dlq_url=dlq))
    assert seen == [None]
