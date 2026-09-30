import io
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from app.api.main import create_app
from app.models import Document, DocumentStatus
from app.services import QueueError
from app.services import documents as repo
from tests.helpers import step, upload


def rows(session_factory):
    with session_factory() as s:
        return s.scalars(select(Document)).all()


# ---------------------------------------------------------------- POST /documents
def test_upload_returns_202_and_does_everything_but_process(client, session_factory, real_storage, real_queue, settings):
    data = b"some document text " * 100
    r = upload(client, "report.txt", data)

    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "PENDING" and body["filename"] == "report.txt"
    doc_id = uuid.UUID(body["id"])
    assert r.headers["location"] == f"/documents/{doc_id}"
    assert body["status_url"] == f"/documents/{doc_id}"

    # 1. raw object stored
    assert real_storage.get_object(f"raw/{doc_id}") == data
    # 2. PENDING row in the database
    [doc] = rows(session_factory)
    assert doc.id == doc_id and doc.status == DocumentStatus.PENDING and doc.attempts == 0
    assert doc.raw_object_key == f"raw/{doc_id}" and doc.size_bytes == len(data) and doc.content_type == "text/plain"
    # 3. job enqueued as JSON
    [msg] = real_queue.receive()
    job = json.loads(msg.body)
    assert job["document_id"] == str(doc_id) and job["raw_object_key"] == f"raw/{doc_id}" and job["version"] == 1
    # ...and nothing was processed synchronously
    assert doc.processed_object_key is None and doc.completed_at is None
    assert not (settings.local_storage_root / "processed").exists()


def test_each_upload_gets_its_own_id_and_job(client, real_queue):
    ids = {upload(client).json()["id"] for _ in range(3)}
    assert len(ids) == 3
    assert real_queue.stats().visible == 3


@pytest.mark.parametrize(
    "sent,stored",
    [("../../etc/passwd", "passwd"), ("C:\\Users\\me\\cv.docx", "cv.docx"), ("a/b/c.txt", "c.txt"), ("  spaced.txt ", "spaced.txt")],
)
def test_filename_is_sanitised(client, session_factory, sent, stored):
    r = upload(client, sent)
    assert r.status_code == 202
    assert r.json()["filename"] == stored
    assert rows(session_factory)[0].original_filename == stored


def test_sanitize_filename_unit():
    from app.api.routes.documents import sanitize_filename

    assert sanitize_filename("ctrl\x07char\n.txt") == "ctrlchar.txt"
    assert sanitize_filename("dir/") == "" and sanitize_filename(None) == "" and sanitize_filename("..") == ".."
    assert sanitize_filename("é文档.txt") == "é文档.txt"


def test_overlong_filename_is_truncated(client):
    r = upload(client, "a" * 400 + ".txt")
    assert r.status_code == 202 and len(r.json()["filename"]) == 255


def test_missing_file_field_is_422(client):
    assert client.post("/documents").status_code == 422
    assert client.post("/documents", files={"other": ("x.txt", io.BytesIO(b"x"))}).status_code == 422


def test_non_multipart_body_is_422(client):
    assert client.post("/documents", json={"file": "nope"}).status_code == 422


def test_plain_form_field_instead_of_file_is_422(client):
    assert client.post("/documents", data={"file": "just text"}).status_code == 422


def test_empty_file_is_400(client, session_factory):
    r = upload(client, "empty.txt", b"")
    assert r.status_code == 400 and "empty" in r.json()["detail"]
    assert rows(session_factory) == []


def test_file_over_limit_is_413_and_not_stored(settings, session_factory, storage, queue):
    small = settings.model_copy(update={"max_upload_bytes": 100})
    c = TestClient(create_app(small, session_factory=session_factory, storage=storage, queue=queue))
    r = upload(c, "big.bin", b"x" * 101)
    assert r.status_code == 413
    assert upload(c, "ok.bin", b"x" * 100).status_code == 202
    assert len(rows(session_factory)) == 1


def test_enqueue_failure_rolls_back_row_and_object(client, queue, session_factory, settings):
    queue.fail_enqueue = QueueError("queue down")
    r = upload(client)
    assert r.status_code == 503
    assert rows(session_factory) == []
    raw_dir = settings.local_storage_root / "raw"
    assert not raw_dir.exists() or list(raw_dir.iterdir()) == []


def test_storage_failure_returns_503_and_creates_no_row(client, storage, session_factory):
    from app.services import ObjectStorageError

    storage.fail_put = ObjectStorageError("disk full")
    assert upload(client).status_code == 503
    assert rows(session_factory) == []


def test_database_failure_returns_503_and_removes_the_orphaned_object(client, monkeypatch, settings):
    def boom(*a, **k):
        raise OperationalError("insert", {}, Exception("db down"))

    monkeypatch.setattr(repo, "create_document", boom)
    assert upload(client).status_code == 503
    raw_dir = settings.local_storage_root / "raw"
    assert not raw_dir.exists() or list(raw_dir.iterdir()) == []


def test_unexpected_error_is_a_clean_500(app, monkeypatch):
    monkeypatch.setattr(repo, "get_document", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bug")))
    c = TestClient(app, raise_server_exceptions=False)
    r = c.get(f"/documents/{uuid.uuid4()}")
    assert r.status_code == 500 and r.json() == {"detail": "internal server error"}


# ---------------------------------------------------------------- GET /documents/{id}
def test_status_endpoint_returns_all_fields(client):
    doc_id = upload(client, "a.csv", b"1,2,3", "text/csv").json()["id"]
    body = client.get(f"/documents/{doc_id}").json()
    assert set(body) >= {
        "id", "filename", "status", "created_at", "updated_at", "completed_at",
        "raw_object_key", "processed_object_key", "error_message",
    }
    assert body["id"] == doc_id and body["filename"] == "a.csv" and body["status"] == "PENDING"
    assert body["completed_at"] is None and body["processed_object_key"] is None and body["error_message"] is None
    assert body["raw_object_key"] == f"raw/{doc_id}"
    assert body["created_at"].endswith("Z") and body["content_type"] == "text/csv" and body["size_bytes"] == 5


def test_status_unknown_id_is_404(client):
    r = client.get(f"/documents/{uuid.uuid4()}")
    assert r.status_code == 404


def test_status_malformed_id_is_422(client):
    assert client.get("/documents/not-a-uuid").status_code == 422


# ---------------------------------------------------------------- GET /documents (list)
def test_list_pagination_and_ordering(client):
    ids = [upload(client, f"f{i}.txt").json()["id"] for i in range(5)]
    page = client.get("/documents", params={"limit": 2, "offset": 0}).json()
    assert page["total"] == 5 and len(page["items"]) == 2
    assert [i["id"] for i in page["items"]] == [ids[4], ids[3]]  # newest first
    last = client.get("/documents", params={"limit": 2, "offset": 4}).json()
    assert [i["id"] for i in last["items"]] == [ids[0]]


def test_list_filters_by_status_and_filename(client, session_factory):
    a = upload(client, "alpha_report.txt").json()["id"]
    upload(client, "beta.txt")
    upload(client, "100%_done.txt")
    with session_factory() as s:
        repo.claim_document(s, uuid.UUID(a), stale_after_seconds=30)
    assert [i["id"] for i in client.get("/documents", params={"status": "PROCESSING"}).json()["items"]] == [a]
    assert client.get("/documents", params={"q": "ALPHA"}).json()["total"] == 1
    assert client.get("/documents", params={"q": "%"}).json()["total"] == 1  # wildcard is escaped
    assert client.get("/documents", params={"q": "_"}).json()["total"] == 2  # underscore is literal
    assert client.get("/documents", params={"status": "NOPE"}).status_code == 422
    assert client.get("/documents", params={"limit": 0}).status_code == 422
    assert client.get("/documents", params={"limit": 101}).status_code == 422
    assert client.get("/documents", params={"offset": -1}).status_code == 422


# ---------------------------------------------------------------- result
def test_result_is_409_until_completed_then_returns_embeddings(client, worker):
    doc_id = upload(client, "r.txt", b"embed me " * 300).json()["id"]
    r = client.get(f"/documents/{doc_id}/result")
    assert r.status_code == 409
    worker.run_once()
    r = client.get(f"/documents/{doc_id}/result")
    assert r.status_code == 200 and r.headers["content-type"] == "application/json"
    assert r.json()["document_id"] == doc_id and r.json()["chunk_count"] >= 1
    assert client.get(f"/documents/{uuid.uuid4()}/result").status_code == 404


# ---------------------------------------------------------------- system endpoints
def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_request_id_is_generated_and_echoed(client):
    assert client.get("/health").headers["x-request-id"]
    assert client.get("/health", headers={"X-Request-ID": "trace-1"}).headers["x-request-id"] == "trace-1"


def test_metrics_expose_required_series(client, worker):
    upload(client)
    client.get(f"/documents/{uuid.uuid4()}")  # 404
    client.get("/documents/not-a-uuid")  # 422
    text = client.get("/metrics")
    assert text.status_code == 200 and text.headers["content-type"].startswith("text/plain")
    m = text.text
    assert 'vectorpipe_http_requests_total{method="POST",path="/documents",status="202"} 1.0' in m
    assert 'vectorpipe_http_requests_total{method="GET",path="/documents/{document_id}",status="404"} 1.0' in m
    assert 'vectorpipe_http_errors_total{method="GET",path="/documents/{document_id}",status="404"} 1.0' in m
    assert 'vectorpipe_http_errors_total{method="GET",path="/documents/{document_id}",status="422"} 1.0' in m
    assert 'vectorpipe_http_request_duration_seconds_bucket{le="0.005",method="POST",path="/documents"}' in m
    assert "vectorpipe_documents_submitted_total 1.0" in m
    assert "vectorpipe_processing_failures_total 0.0" in m
    assert 'vectorpipe_documents{status="PENDING"} 1.0' in m
    assert 'vectorpipe_queue_messages{state="visible"} 1.0' in m
    assert "vectorpipe_database_up 1.0" in m
    assert "path=\"/metrics\"" not in m  # the scrape itself is not measured


def test_processing_failures_metric_counts_failed_attempts_across_workers(client, queue, processor, storage):
    upload(client)
    storage.fail_get = RuntimeError("nope")
    step(queue, processor)
    assert "vectorpipe_processing_failures_total 1.0" in client.get("/metrics").text
    step(queue, processor)
    step(queue, processor)
    body = client.get("/metrics").text
    assert "vectorpipe_processing_failures_total 3.0" in body and 'vectorpipe_documents{status="FAILED"} 1.0' in body
    assert 'vectorpipe_queue_messages{state="dead"} 1.0' in body


def test_metrics_survive_a_database_outage(client, session_factory, monkeypatch):
    def down():
        raise OperationalError("select", {}, Exception("db down"))

    monkeypatch.setattr(repo, "status_counts", lambda s: down())
    r = client.get("/metrics")
    assert r.status_code == 200 and "vectorpipe_database_up 0.0" in r.text


def test_stats_endpoint(client, worker):
    upload(client, "a.txt")
    upload(client, "b.txt")
    worker.run_once()
    upload(client, "c.txt")
    s = client.get("/stats").json()
    assert s["total"] == 3 and s["counts"] == {"PENDING": 1, "PROCESSING": 0, "COMPLETED": 2, "FAILED": 0}
    assert s["success_rate"] == 1.0 and s["processing_failures"] == 0
    assert s["queue"]["visible"] == 1 and s["queue"]["dead"] == 0
    assert len(s["timeline"]) == 30
    assert sum(p["submitted"] for p in s["timeline"]) == 3 and sum(p["completed"] for p in s["timeline"]) == 2
    assert s["latency"]["avg_seconds"] is not None and s["latency"]["sample_size"] == 2


def test_stats_on_empty_system(client):
    s = client.get("/stats").json()
    assert s["total"] == 0 and s["success_rate"] is None and s["latency"]["avg_seconds"] is None


def test_dashboard_is_served_at_root(client):
    r = client.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert "VectorPipe" in r.text


def test_unknown_route_is_404_and_metric_label_is_bounded(client):
    assert client.get("/no/such/path/123").status_code == 404
    assert 'path="unmatched"' in client.get("/metrics").text
