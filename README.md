# VectorPipe

![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-API-009688)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-15-336791)
![Terraform](https://img.shields.io/badge/Terraform-%E2%89%A51.10-7B42BC)
![Docker](https://img.shields.io/badge/Docker-compose-2496ED)

A distributed document-ingestion prototype that runs **entirely on your machine** — no AWS account,
credentials or network access needed. (AWS is only used by the optional Terraform remote-state
bootstrap, see [Infrastructure](#infrastructure).)

**Contents:** [Install](#1-install) · [Docker](#run-with-docker) · [API](#api) ·
[Reliability](#reliability-design) · [Infrastructure](#infrastructure) · [Layout](#layout)

```
Client ──▶ FastAPI API ──┬─▶ local object storage   storage/raw/<id>
                         ├─▶ PostgreSQL row          status = PENDING
                         ├─▶ local queue             JSON job message
                         └─▶ HTTP 202 {id, status}
                                          │
                    Worker(s) ◀───────────┘ long-poll
                       ├─ status → PROCESSING
                       ├─ read raw document
                       ├─ simulate expensive embedding work (PROCESSING_DELAY_SECONDS)
                       ├─ write storage/processed/<id>.json
                       ├─ status → COMPLETED
                       └─ acknowledge (delete) the queue message — only now
```

The API never does the expensive work. API and worker are separate processes and can be started,
stopped and scaled (run N workers) independently.

Application code depends only on the `ObjectStorage` and `Queue` interfaces
(`app/services/object_storage.py`, `app/services/queue.py`); today's implementations are
`LocalObjectStorage` (files) and `LocalQueue` (a SQLite file with SQS-like semantics). No cloud
implementations are included.

## 1. Install

Requires Python 3.12+.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then edit DATABASE_URL
```

## 2. Configure PostgreSQL

You provide the database; nothing is provisioned. Example:

```bash
sudo -u postgres psql -c "CREATE USER vectorpipe WITH PASSWORD 'vectorpipe';"
sudo -u postgres psql -c "CREATE DATABASE vectorpipe OWNER vectorpipe;"
```

Set `DATABASE_URL=postgresql://vectorpipe:vectorpipe@localhost:5432/vectorpipe` in `.env`
(`postgresql://` is mapped to the psycopg 3 driver automatically).

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | *(required)* | PostgreSQL URL |
| `LOCAL_STORAGE_ROOT` | `./storage` | Root of local object storage (`raw/`, `processed/`) and the queue (`queue/`) |
| `PROCESSING_DELAY_SECONDS` | `3` | Simulated processing time per document |
| `LOG_LEVEL` | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR` |
| `QUEUE_POLL_INTERVAL_SECONDS` | `0.5` | How often a long-polling consumer re-checks the queue |
| *optional* `QUEUE_VISIBILITY_TIMEOUT_SECONDS` | `60` | In-flight time before an unacknowledged message is redelivered |
| *optional* `QUEUE_MAX_RECEIVE_COUNT` | `3` | Deliveries (= attempts) before a message is dead-lettered |
| *optional* `RETRY_BACKOFF_SECONDS` | `2` | First retry delay, doubles per attempt |
| *optional* `MAX_UPLOAD_BYTES` | `10485760` | Upload size limit |
| *optional* `API_HOST` / `API_PORT` | `127.0.0.1` / `8000` | API bind address |
| *optional* `WORKER_METRICS_PORT` | `9101` | Worker Prometheus endpoint (`0` disables; use a different port per worker) |
| *optional* `SIMULATE_FAILURE_RATE` | `0` | Dev aid: chance (0–1) that an attempt fails, to exercise retries |

Keep `PROCESSING_DELAY_SECONDS` below `QUEUE_VISIBILITY_TIMEOUT_SECONDS`.

## 3. Run migrations

```bash
alembic upgrade head
```

## 4. Start the API

```bash
python -m app.api
```

Open **http://127.0.0.1:8000/** for the live dashboard, `/docs` for the OpenAPI UI.

## 5. Start a worker

In another terminal (start as many as you like; give each its own `WORKER_METRICS_PORT`):

```bash
python -m app.worker
WORKER_METRICS_PORT=9102 python -m app.worker     # a second one
```

`Ctrl-C` / `SIGTERM` finishes the current job, then exits. A second signal exits immediately.

## 6. Test

```bash
pytest                                   # 120+ tests, temporary SQLite + temp dirs, no external services
TEST_DATABASE_URL=postgresql://user:pw@localhost:5432/vectorpipe_test pytest   # same suite on PostgreSQL
```

Tests never touch AWS. They cover upload, status, invalid requests, object storage, queue
(visibility, redelivery, dead-lettering, concurrency), worker success/failure/duplicate/concurrent
paths, graceful shutdown, migrations vs. models, and the full end-to-end flow.

## Run with Docker

Starts PostgreSQL, the API (migrations run on boot) and two workers:

```bash
docker compose up --build
```

Dashboard at **http://localhost:8000/**; PostgreSQL is published on host port `5433`.
Scale workers with `docker compose up --scale worker=4`.

## 7. Submit a document

```bash
curl -F "file=@report.pdf" http://127.0.0.1:8000/documents
# 202 {"id":"6b0e…","status":"PENDING","filename":"report.pdf","status_url":"/documents/6b0e…"}
```

(or drop files on the dashboard).

## 8. Check status

```bash
curl http://127.0.0.1:8000/documents/<id>          # PENDING → PROCESSING → COMPLETED (or FAILED)
curl http://127.0.0.1:8000/documents/<id>/result   # the embedding JSON, once COMPLETED
```

## API

| Endpoint | |
|---|---|
| `POST /documents` | multipart upload → `202` |
| `GET /documents/{id}` | id, filename, status, created_at, updated_at, completed_at, raw/processed object keys, error_message (+ size, content type, attempts) |
| `GET /health` | `{"status":"ok"}` |
| `GET /metrics` | Prometheus: HTTP request count / latency / errors, documents submitted, processing failures, documents by status, queue depth |
| *extras for the dashboard* | `GET /documents` (list/filter/paginate), `GET /documents/{id}/result`, `GET /stats`, `GET /` |

Workers also serve their own metrics (`vectorpipe_worker_*`) on `WORKER_METRICS_PORT`.
`vectorpipe_processing_failures_total` on the API is derived from the database, so it counts failures
from every worker and survives restarts.

## Reliability design

* **At-least-once delivery, idempotent processing.** A message is deleted only after the row is `COMPLETED`.
* **Atomic claim.** `PENDING/FAILED → PROCESSING` is one conditional `UPDATE`; of N racing workers exactly one wins.
* **Duplicates** of a completed document are acknowledged without reprocessing. A document being processed
  elsewhere is left alone. Results have a deterministic key and bytes, so re-running is harmless.
* **Failures:** the error is stored and the message is *not* acknowledged. The row goes back to `PENDING`
  (retry pending) and the message reappears after an exponential backoff. After `QUEUE_MAX_RECEIVE_COUNT`
  attempts the row becomes `FAILED` and the queue dead-letters the message.
* **Crashed worker:** its row stays `PROCESSING`; when the message is redelivered (visibility timeout) the row
  is considered abandoned (untouched for 0.8× the timeout) and reclaimed.
* **Submission consistency:** if the DB insert or enqueue fails after the upload, the object/row are removed and the API returns 503.
* Structured JSON logs, request IDs, DB/connect timeouts, graceful shutdown for API and worker.

Inspect dead-lettered messages: `sqlite3 storage/queue/queue.sqlite3 "select * from messages where state='dead'"`.

## Infrastructure

Terraform lives in `terraform/`. State is stored remotely in S3 with native locking and is never
committed (`*.tfstate` is git-ignored).

| Path | Purpose |
|---|---|
| `terraform/bootstrap/` | One-time stack that creates the hardened state bucket `ingest-pipeline-tfstate-<env>` (versioning, encryption, public access block, TLS-only, `prevent_destroy`). Its own state lives in the bucket under `bootstrap/`. |
| `terraform/backend.tf` | Main stack backend: `vectorpipe/terraform.tfstate` in that bucket, `use_lockfile = true` |

```bash
cd terraform && terraform init     # connects to the S3 backend
```

Requires Terraform >= 1.10 and AWS credentials with access to the bucket. See
[`terraform/bootstrap/README.md`](terraform/bootstrap/README.md) for first-time setup.
`.github/workflows/` and `docs/` are placeholders for CI and runbooks.

## Layout

```
app/api        FastAPI app, routes, middleware, dashboard (static/dashboard.html)
app/worker     processor (claim/process/ack), runner loop, fake embeddings
app/services   object_storage.py, queue.py, documents.py (DB ops), stats.py
app/models     SQLAlchemy models        app/schemas   Pydantic schemas
app/db         engine/session           app/config    settings + logging
app/metrics    Prometheus metrics       migrations/   Alembic
tests/         pytest suite             storage/      raw/ processed/ (runtime data)
terraform/     remote-state backend + bootstrap
```
