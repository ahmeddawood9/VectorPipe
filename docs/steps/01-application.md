# Step 1 — Application: API, worker, local storage and queue

**Date:** 2026-10-01 · **Commit:** `e62c40a` Add application code

## Goal

A document-ingestion pipeline where the API accepts uploads instantly and the expensive work
(embedding generation) happens in separate, independently scalable worker processes. It had to run
entirely on one machine with no cloud account.

## What was built

| Area | Files | Role |
|---|---|---|
| API | `app/api/` | FastAPI: `POST /documents` (202), `GET /documents/{id}`, `/result`, `/stats`, `/health`, `/metrics`, dashboard at `/` |
| Worker | `app/worker/` | `runner.py` polls the queue; `processor.py` claims → processes → completes → acknowledges; `embedding.py` produces deterministic fake embeddings |
| Interfaces | `app/services/object_storage.py`, `app/services/queue.py` | `ObjectStorage` (put/get/delete) and `Queue` (enqueue/receive/delete/change_visibility/stats) |
| Local implementations | same files | `LocalObjectStorage` (files under `storage/`) and `LocalQueue` (SQLite file with SQS-like semantics) |
| Database | `app/db/`, `app/models/`, `migrations/` | PostgreSQL via SQLAlchemy 2 + Alembic; one `documents` table holding status |
| Config | `app/config/` | Pydantic settings from env / `.env`; JSON structured logs |
| Metrics | `app/metrics/` | Prometheus for the API and for each worker |
| Tests | `tests/` | 122 tests on temporary SQLite + temp dirs, no external services |

## How a document flows

```
POST /documents ─▶ storage.put_object(raw/<id>)
                ─▶ INSERT documents (status PENDING)
                ─▶ queue.enqueue({document_id})
                ─▶ 202 {id, status}

worker: receive ─▶ claim (PENDING → PROCESSING, one conditional UPDATE)
               ─▶ read raw/<id> ─▶ embed ─▶ put processed/<id>.json
               ─▶ status COMPLETED ─▶ queue.delete (acknowledge, only now)
```

If the insert or the enqueue fails after the upload, the API removes what it already wrote and
returns 503, so no half-submitted documents remain.

## Key design decisions

* **The app depends only on the two interfaces.** That decision is what made Step 4 a plug-in job
  instead of a rewrite.
* **At-least-once delivery, idempotent processing.** A message is deleted only after the row is
  `COMPLETED`. A duplicate delivery of a finished document is acknowledged without reprocessing.
  Results have a deterministic key and bytes, so re-running is harmless.
* **The atomic claim prevents double work.** Of N workers racing for one document, exactly one
  wins the conditional `UPDATE`.
* **Failures use the queue's retry mechanics.** The message is not acknowledged; its visibility is
  set to an exponential backoff; after `QUEUE_MAX_RECEIVE_COUNT` (3) attempts the row becomes
  `FAILED` and the message is dead-lettered.
* **Crashed workers are recovered.** A `PROCESSING` row untouched for 0.8× the visibility timeout
  counts as abandoned and is reclaimed when the message is redelivered.
* **Graceful shutdown.** SIGTERM finishes the current job; a second signal exits immediately.

## How to verify

```bash
pip install -r requirements.txt
pytest -q                    # all tests, no services needed
alembic upgrade head && python -m app.api     # terminal 1
python -m app.worker                          # terminal 2
curl -F "file=@report.pdf" http://127.0.0.1:8000/documents
```

## Left open

No cloud implementations of the interfaces (added in [Step 4](04-aws-backends.md)).
