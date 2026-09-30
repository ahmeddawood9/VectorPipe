"""The whole flow, exactly as documented:  POST -> storage -> PENDING -> queue -> worker -> COMPLETED."""

import json
import threading
import time

from app.models import DocumentStatus
from app.worker import Worker
from tests.helpers import upload


def test_full_flow_through_the_public_api(client, real_storage, real_queue, processor, session_factory):
    files = {"a.txt": b"alpha " * 900, "b.csv": b"x,y\n1,2\n" * 50, "c.md": b"# title\n" + b"body " * 3000}
    ids = {}
    for name, data in files.items():
        r = upload(client, name, data)
        assert r.status_code == 202 and r.json()["status"] == "PENDING"
        ids[name] = r.json()["id"]

    # Nothing has been processed by the API itself.
    assert {client.get(f"/documents/{i}").json()["status"] for i in ids.values()} == {"PENDING"}
    assert real_queue.stats().visible == 3

    worker = Worker(real_queue, processor)
    thread = threading.Thread(target=worker.run_forever)
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            states = {n: client.get(f"/documents/{i}").json()["status"] for n, i in ids.items()}
            if set(states.values()) == {"COMPLETED"}:
                break
            time.sleep(0.05)
        assert set(states.values()) == {"COMPLETED"}, states
    finally:
        worker.request_stop()
        thread.join(timeout=10)

    for name, doc_id in ids.items():
        body = client.get(f"/documents/{doc_id}").json()
        assert body["status"] == DocumentStatus.COMPLETED.value
        assert body["processed_object_key"] == f"processed/{doc_id}.json"
        assert body["completed_at"] is not None and body["error_message"] is None
        # the raw upload and the processed result are both in object storage
        assert real_storage.get_object(body["raw_object_key"]) == files[name]
        result = json.loads(real_storage.get_object(body["processed_object_key"]))
        assert result["document_id"] == doc_id and result["filename"] == name
        assert client.get(f"/documents/{doc_id}/result").json() == result

    stats = real_queue.stats()
    assert (stats.visible, stats.in_flight, stats.dead) == (0, 0, 0)
