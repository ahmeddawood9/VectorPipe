# Step 1: The app

*October 1. Commit: "Add application code".*

## What I was trying to do

I wanted a document ingestion pipeline where uploading is instant and the slow part, generating
embeddings, happens somewhere else. The API should never do the heavy lifting. Workers should be
separate processes that I can start, stop and run as many of as I like.

I also wanted the whole thing to run on my laptop with no cloud account, so I could build and test
it properly before paying for anything.

## What I built

**The API** (`app/api/`) is FastAPI. `POST /documents` takes a file, stores it, writes a database row
and puts a job on a queue, then returns `202` straight away. There are endpoints to check the status
and fetch the result, plus `/health`, Prometheus `/metrics`, `/stats`, and a small dashboard at `/`.

**The worker** (`app/worker/`) pulls jobs off the queue. For each one it claims the document, reads
the raw file, does the "embedding" work and writes the result. The embeddings are fake but
deterministic, and there's a configurable delay standing in for the real model.

**Two interfaces hold it together.** `ObjectStorage` (put, get, delete) and `Queue` (enqueue,
receive, delete, change visibility, stats). The API and worker only ever talk to these. For local
work there's `LocalObjectStorage`, which is just files under `storage/`, and `LocalQueue`, a SQLite
file that behaves like SQS: visibility timeouts, receive counts and dead-lettering.

That decision turned out to be the most important one in the project. When I moved to AWS later, I
only had to write new implementations of the two interfaces. Nothing else changed.

**Postgres** holds each document's status, through SQLAlchemy 2 and Alembic migrations.

## How a document moves through it

```
POST /documents ─▶ save raw/<id>
                ─▶ insert row, status PENDING
                ─▶ enqueue {document_id}
                ─▶ 202 {id, status}

worker ─▶ receive a job
       ─▶ claim it: PENDING → PROCESSING (one conditional UPDATE)
       ─▶ read raw/<id>, embed, write processed/<id>.json
       ─▶ status COMPLETED
       ─▶ delete the message (only now)
```

If saving the row or queueing the job fails after the upload, the API cleans up what it already wrote
and returns `503`, so I never end up with half-submitted documents.

## Decisions I care about

**The message is deleted last.** Delivery is at-least-once, so the worker only acknowledges a job
once the row says `COMPLETED`. If it crashes anywhere before that, the job comes back.

**Claiming is atomic.** If several workers grab the same document, a single conditional `UPDATE`
means exactly one of them wins. The others back off. This is what really prevents double work, and
it mattered a lot later on SQS.

**Duplicates are harmless.** If a finished document's job shows up again, the worker just
acknowledges it. Results have a fixed key and identical bytes, so redoing one changes nothing.

**Failures retry with backoff.** A failed job isn't acknowledged. Its visibility is pushed out with an
exponential backoff. After three attempts the row is marked `FAILED` and the message is dead-lettered.

**Crashed workers get cleaned up.** If a worker dies mid-job, its row is stuck at `PROCESSING`. When
the message comes back after the visibility timeout, a row that hasn't moved for 80% of that timeout
counts as abandoned, and the new worker takes it over.

**Shutdown is graceful.** `SIGTERM` lets the current job finish. A second signal exits immediately.

## Checking it

```bash
pip install -r requirements.txt
pytest -q                                        # 122 tests, no services needed
alembic upgrade head && python -m app.api        # terminal 1
python -m app.worker                             # terminal 2
curl -F "file=@report.pdf" http://127.0.0.1:8000/documents
```

## Still open

Everything is local. No cloud versions of storage or the queue yet.
