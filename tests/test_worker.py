import json
import threading
import time
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from app.db.types import utcnow
from app.models import Document, DocumentStatus
from app.services import ObjectStorageError
from app.services import documents as repo
from app.worker import Outcome, Worker
from app.worker import runner as runner_module
from app.worker.embedding import generate_embeddings, serialize
from tests.helpers import step, upload

DATA = b"the quick brown fox " * 400


def get_doc(session_factory, doc_id) -> Document:
    with session_factory() as s:
        return s.scalar(select(Document).where(Document.id == uuid.UUID(str(doc_id))))


def submit(client, name="doc.txt", data=DATA) -> str:
    r = upload(client, name, data)
    assert r.status_code == 202
    return r.json()["id"]


# ------------------------------------------------------------------ success path
def test_success_path_end_to_end(client, queue, processor, session_factory, real_storage):
    doc_id = submit(client)

    assert step(queue, processor) is Outcome.COMPLETED

    doc = get_doc(session_factory, doc_id)
    assert doc.status == DocumentStatus.COMPLETED
    assert doc.attempts == 1 and doc.error_message is None
    assert doc.completed_at is not None and doc.completed_at >= doc.created_at
    assert doc.processed_object_key == f"processed/{doc_id}.json"

    expected = serialize(generate_embeddings(document_id=doc_id, filename="doc.txt", data=DATA))
    assert real_storage.get_object(doc.processed_object_key) == expected
    result = json.loads(expected)
    assert result["chunk_count"] == -(-len(DATA) // 2048) and result["dimensions"] == 16

    stats = queue.stats()
    assert (stats.visible, stats.in_flight, stats.dead) == (0, 0, 0)  # acknowledged


def test_status_is_processing_while_the_work_runs(client, queue, make_processor, session_factory):
    doc_id = submit(client)
    seen = []
    proc = make_processor(sleep=lambda _s: seen.append(get_doc(session_factory, doc_id).status))
    step(queue, proc)
    assert seen == [DocumentStatus.PROCESSING]


def test_configured_delay_is_used(client, queue, make_processor, settings):
    submit(client)
    delays = []
    proc = make_processor(settings=settings.model_copy(update={"processing_delay_seconds": 2.5}), sleep=delays.append)
    step(queue, proc)
    assert delays == [2.5]


def test_reprocessing_writes_byte_identical_output(client, queue, processor, session_factory, real_storage):
    doc_id = submit(client)
    step(queue, processor)
    first = real_storage.get_object(f"processed/{doc_id}.json")
    with session_factory() as s:
        s.execute(update(Document).where(Document.id == uuid.UUID(doc_id)).values(status=DocumentStatus.FAILED))
        s.commit()
    queue.enqueue(json.dumps({"document_id": doc_id, "raw_object_key": f"raw/{doc_id}", "submitted_at": utcnow().isoformat()}))
    assert step(queue, processor) is Outcome.COMPLETED
    assert real_storage.get_object(f"processed/{doc_id}.json") == first


def test_run_once_drains_the_queue(client, worker, session_factory):
    ids = [submit(client, f"f{i}.txt") for i in range(4)]
    assert worker.run_once() == 4
    assert all(get_doc(session_factory, i).status == DocumentStatus.COMPLETED for i in ids)


# ------------------------------------------------------------------ failure path
def test_failure_records_error_keeps_message_and_retries(client, queue, processor, session_factory, storage):
    doc_id = submit(client)
    storage.fail_get = ObjectStorageError("disk on fire")

    assert step(queue, processor) is Outcome.RETRY

    doc = get_doc(session_factory, doc_id)
    assert doc.status == DocumentStatus.PENDING  # not terminal: a retry is coming
    assert "ObjectStorageError: disk on fire" in doc.error_message
    assert doc.attempts == 1 and doc.processed_object_key is None and doc.completed_at is None
    # the message was NOT acknowledged
    assert queue.stats().visible + queue.stats().in_flight == 1

    storage.fail_get = None  # the fault clears
    assert step(queue, processor) is Outcome.COMPLETED
    doc = get_doc(session_factory, doc_id)
    assert doc.status == DocumentStatus.COMPLETED and doc.attempts == 2 and doc.error_message is None
    assert queue.stats().visible + queue.stats().in_flight + queue.stats().dead == 0


def test_retry_uses_exponential_backoff_via_visibility(client, queue, make_processor, settings, storage):
    submit(client)
    proc = make_processor(settings=settings.model_copy(update={"retry_backoff_seconds": 10}))
    storage.fail_get = RuntimeError("x")
    assert step(queue, proc) is Outcome.RETRY
    assert queue.receive() == []  # hidden for the backoff period
    assert queue.stats().in_flight == 1


def test_final_failure_marks_failed_and_dead_letters_without_ever_acknowledging(client, queue, processor, session_factory, storage):
    doc_id = submit(client)
    storage.fail_get = RuntimeError("permanent")

    outcomes = [step(queue, processor) for _ in range(3)]
    assert outcomes == [Outcome.RETRY, Outcome.RETRY, Outcome.FAILED]

    doc = get_doc(session_factory, doc_id)
    assert doc.status == DocumentStatus.FAILED and doc.attempts == 3
    assert "RuntimeError: permanent" in doc.error_message
    assert doc.completed_at is None

    assert step(queue, processor) is None  # nothing left to deliver
    assert queue.stats().dead == 1  # the message was dead-lettered, never deleted


def test_failure_while_writing_the_result_leaves_no_processed_key(client, queue, processor, session_factory, storage):
    doc_id = submit(client)
    storage.fail_put = ObjectStorageError("cannot write")
    assert step(queue, processor) is Outcome.RETRY
    doc = get_doc(session_factory, doc_id)
    assert doc.status == DocumentStatus.PENDING and doc.processed_object_key is None and "cannot write" in doc.error_message


def test_missing_raw_object_fails_after_retries(client, queue, processor, session_factory, real_storage):
    doc_id = submit(client)
    real_storage.delete_object(f"raw/{doc_id}")
    outcomes = [step(queue, processor) for _ in range(3)]
    assert outcomes[-1] is Outcome.FAILED
    assert "ObjectNotFoundError" in get_doc(session_factory, doc_id).error_message


def test_failure_metric_is_incremented(client, queue, make_processor, storage):
    from app.metrics import WorkerMetrics

    metrics = WorkerMetrics()
    proc = make_processor(metrics=metrics)
    submit(client)
    storage.fail_get = RuntimeError("x")
    step(queue, proc)
    assert metrics.failures._value.get() == 1
    assert metrics.jobs.labels("retry")._value.get() == 1


def test_simulated_failure_rate_triggers_a_retry(client, queue, make_processor, settings, session_factory):
    doc_id = submit(client)
    proc = make_processor(settings=settings.model_copy(update={"simulate_failure_rate": 1.0}))
    assert step(queue, proc) is Outcome.RETRY
    assert "simulated" in get_doc(session_factory, doc_id).error_message

    proc = make_processor(settings=settings.model_copy(update={"simulate_failure_rate": 0.5}), random_fn=lambda: 0.9)
    assert step(queue, proc) is Outcome.COMPLETED


# ------------------------------------------------------------------ duplicates / idempotency
def job_body(doc_id) -> str:
    return json.dumps({"document_id": str(doc_id), "raw_object_key": f"raw/{doc_id}", "submitted_at": utcnow().isoformat()})


def test_duplicate_message_for_a_completed_document_is_acknowledged_not_reprocessed(client, queue, processor, session_factory, real_storage):
    doc_id = submit(client)
    step(queue, processor)
    before = get_doc(session_factory, doc_id)
    output = real_storage.get_object(before.processed_object_key)

    queue.enqueue(job_body(doc_id))
    assert step(queue, processor) is Outcome.DUPLICATE

    after = get_doc(session_factory, doc_id)
    assert after.attempts == 1 and after.completed_at == before.completed_at and after.status == DocumentStatus.COMPLETED
    assert real_storage.get_object(after.processed_object_key) == output
    assert queue.stats().visible + queue.stats().in_flight + queue.stats().dead == 0


def test_two_messages_queued_for_the_same_document_process_it_once(client, queue, processor, session_factory):
    doc_id = submit(client)
    queue.enqueue(job_body(doc_id))
    assert [step(queue, processor), step(queue, processor)] == [Outcome.COMPLETED, Outcome.DUPLICATE]
    assert get_doc(session_factory, doc_id).attempts == 1
    assert step(queue, processor) is None


def test_message_for_a_document_being_processed_elsewhere_is_left_alone(client, queue, processor, session_factory):
    doc_id = submit(client)
    with session_factory() as s:
        assert repo.claim_document(s, uuid.UUID(doc_id), stale_after_seconds=30).result is repo.ClaimResult.CLAIMED
    before = get_doc(session_factory, doc_id)

    assert step(queue, processor) is Outcome.BUSY

    after = get_doc(session_factory, doc_id)
    assert after.status == DocumentStatus.PROCESSING and after.attempts == before.attempts == 1
    assert queue.stats().in_flight == 1  # not acknowledged: redelivered later


def test_document_stuck_in_processing_by_a_dead_worker_is_reclaimed(client, queue, processor, session_factory):
    doc_id = submit(client)
    with session_factory() as s:
        repo.claim_document(s, uuid.UUID(doc_id), stale_after_seconds=30)
        s.execute(update(Document).where(Document.id == uuid.UUID(doc_id)).values(updated_at=utcnow() - timedelta(hours=1)))
        s.commit()
    assert step(queue, processor) is Outcome.COMPLETED
    doc = get_doc(session_factory, doc_id)
    assert doc.status == DocumentStatus.COMPLETED and doc.attempts == 2


def test_crash_after_result_written_but_before_completion_is_recovered(client, queue, processor, session_factory, real_storage, storage):
    """Simulates a worker dying between writing the result and updating the database."""
    doc_id = submit(client)
    result = serialize(generate_embeddings(document_id=doc_id, filename="doc.txt", data=DATA))
    real_storage.put_object(f"processed/{doc_id}.json", result)  # orphaned result from the crashed attempt
    assert step(queue, processor) is Outcome.COMPLETED
    assert real_storage.get_object(f"processed/{doc_id}.json") == result
    assert get_doc(session_factory, doc_id).status == DocumentStatus.COMPLETED


def test_late_success_after_another_worker_failed_the_job_still_completes(session_factory, client):
    doc_id = uuid.UUID(submit(client))
    with session_factory() as s:
        repo.claim_document(s, doc_id, stale_after_seconds=30)
        assert repo.mark_attempt_failed(s, doc_id, "other worker failed", final=False)
        assert repo.mark_completed(s, doc_id, f"processed/{doc_id}.json")
        assert repo.mark_completed(s, doc_id, f"processed/{doc_id}.json")  # repeatable
        # a late failure report must not undo the success
        assert repo.mark_attempt_failed(s, doc_id, "too late", final=True) is False
    doc = get_doc(session_factory, doc_id)
    assert doc.status == DocumentStatus.COMPLETED and doc.error_message is None


def test_invalid_and_unknown_messages_are_never_acknowledged_and_end_up_dead_lettered(queue, processor):
    queue.enqueue("this is not json")
    queue.enqueue(json.dumps({"nope": 1}))
    queue.enqueue(job_body(uuid.uuid4()))  # well-formed, but no such document
    outcomes = [step(queue, processor) for _ in range(9)]
    assert outcomes.count(Outcome.INVALID) == 9
    assert step(queue, processor) is None
    assert queue.stats().dead == 3


# ------------------------------------------------------------------ concurrency
def test_claim_is_atomic_across_threads(client, session_factory):
    doc_id = uuid.UUID(submit(client))
    results = []
    barrier = threading.Barrier(8)

    def contend():
        with session_factory() as s:
            barrier.wait()
            results.append(repo.claim_document(s, doc_id, stale_after_seconds=30).result)

    threads = [threading.Thread(target=contend) for _ in range(8)]
    [t.start() for t in threads]
    [t.join(timeout=30) for t in threads]
    assert results.count(repo.ClaimResult.CLAIMED) == 1
    assert results.count(repo.ClaimResult.IN_PROGRESS) == 7
    assert get_doc(session_factory, doc_id).attempts == 1


def test_many_workers_process_every_document_exactly_once(client, make_processor, queue, session_factory):
    ids = [submit(client, f"f{i}.txt") for i in range(16)]
    counts: list[int] = []

    def run():
        proc = make_processor(sleep=lambda _s: time.sleep(0.01))
        counts.append(Worker(queue, proc).run_once())

    threads = [threading.Thread(target=run) for _ in range(4)]
    [t.start() for t in threads]
    [t.join(timeout=60) for t in threads]

    assert sum(counts) == 16
    docs = [get_doc(session_factory, i) for i in ids]
    assert all(d.status == DocumentStatus.COMPLETED and d.attempts == 1 for d in docs)
    assert queue.stats().visible + queue.stats().in_flight + queue.stats().dead == 0


def test_racing_duplicate_messages_process_the_document_once(client, tmp_path, settings, make_processor, session_factory):
    from app.services import LocalQueue

    q = LocalQueue(tmp_path / "dup.sqlite3", visibility_timeout=0.4, max_receive_count=5, poll_interval=0.02)
    doc_id = submit(client)
    for _ in range(3):
        q.enqueue(job_body(doc_id))
    outcomes: list[Outcome] = []
    lock = threading.Lock()

    def run():
        proc = make_processor(queue=q, sleep=lambda _s: time.sleep(0.15))
        while True:
            msgs = q.receive()
            if not msgs:
                return
            out = proc.handle(msgs[0])
            with lock:
                outcomes.append(out)

    threads = [threading.Thread(target=run) for _ in range(3)]
    [t.start() for t in threads]
    [t.join(timeout=30) for t in threads]
    time.sleep(0.5)  # BUSY leftovers become visible again
    run()  # a later poll acknowledges the rest as duplicates

    assert outcomes.count(Outcome.COMPLETED) == 1
    assert get_doc(session_factory, doc_id).attempts == 1
    assert q.stats().visible + q.stats().in_flight + q.stats().dead == 0


# ------------------------------------------------------------------ runner / graceful shutdown
def test_worker_finishes_the_current_job_before_shutting_down(client, queue, make_processor, session_factory):
    doc_id = submit(client)
    started = threading.Event()

    def slow(_s):
        started.set()
        time.sleep(0.4)

    worker = Worker(queue, make_processor(sleep=slow))
    t = threading.Thread(target=worker.run_forever)
    t.start()
    assert started.wait(5)
    worker.request_stop()  # signal arrives mid-job
    t.join(timeout=10)

    assert not t.is_alive()
    doc = get_doc(session_factory, doc_id)
    assert doc.status == DocumentStatus.COMPLETED  # the in-flight job was not abandoned
    assert queue.stats().visible + queue.stats().in_flight == 0


def test_idle_worker_stops_promptly(queue, processor):
    worker = Worker(queue, processor)
    t = threading.Thread(target=worker.run_forever)
    t.start()
    time.sleep(0.15)
    started = time.monotonic()
    worker.request_stop()
    t.join(timeout=5)
    assert not t.is_alive() and time.monotonic() - started < 3


def test_worker_processes_jobs_that_arrive_while_running(client, queue, processor, session_factory):
    worker = Worker(queue, processor)
    t = threading.Thread(target=worker.run_forever)
    t.start()
    try:
        doc_id = submit(client)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and get_doc(session_factory, doc_id).status != DocumentStatus.COMPLETED:
            time.sleep(0.05)
        assert get_doc(session_factory, doc_id).status == DocumentStatus.COMPLETED
    finally:
        worker.request_stop()
        t.join(timeout=10)


def test_worker_survives_queue_errors(monkeypatch, client, queue, processor, session_factory):
    monkeypatch.setattr(runner_module, "ERROR_PAUSE_SECONDS", 0.05)
    queue.fail_receive_times = 2
    doc_id = submit(client)
    worker = Worker(queue, processor)
    t = threading.Thread(target=worker.run_forever)
    t.start()
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and get_doc(session_factory, doc_id).status != DocumentStatus.COMPLETED:
            time.sleep(0.05)
        assert get_doc(session_factory, doc_id).status == DocumentStatus.COMPLETED
    finally:
        worker.request_stop()
        t.join(timeout=10)


def test_unexpected_processor_error_does_not_kill_the_worker_and_message_is_redelivered(client, queue, processor, session_factory, monkeypatch, worker):
    doc_id = submit(client)
    import app.worker.processor as proc_module

    def boom(*a, **k):
        raise RuntimeError("database exploded")

    with monkeypatch.context() as m:
        m.setattr(proc_module, "claim_document", boom)
        assert worker.run_once() == 1  # handled (and swallowed) without raising
    assert get_doc(session_factory, doc_id).status == DocumentStatus.PENDING
    assert queue.stats().in_flight == 1  # unacknowledged -> comes back after the visibility timeout


def test_stale_threshold_sits_below_the_visibility_timeout(settings):
    """A crashed worker's message is redelivered right after the visibility timeout; the row must be
    reclaimable by then, so the threshold has to be strictly smaller."""
    assert settings.processing_stale_after_seconds < settings.queue_visibility_timeout_seconds


def test_row_processing_for_less_than_the_threshold_is_not_reclaimed_but_older_is(client, queue, processor, session_factory, settings):
    doc_id = uuid.UUID(submit(client))

    def age_processing_row(seconds: float) -> None:
        with session_factory() as s:
            s.execute(
                update(Document)
                .where(Document.id == doc_id)
                .values(status=DocumentStatus.PROCESSING, updated_at=utcnow() - timedelta(seconds=seconds))
            )
            s.commit()

    age_processing_row(settings.processing_stale_after_seconds * 0.5)
    assert step(queue, processor) is Outcome.BUSY  # a live worker still owns it

    age_processing_row(settings.processing_stale_after_seconds * 1.1)
    queue.enqueue(job_body(doc_id))  # the redelivery a crashed worker's message would eventually produce
    assert step(queue, processor) is Outcome.COMPLETED
    assert get_doc(session_factory, doc_id).attempts == 1
