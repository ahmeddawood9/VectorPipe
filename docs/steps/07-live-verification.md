# Step 7 — Verification on live AWS

**Date:** 2026-10-03 · **Commits:** none (operational step; results recorded here and in the README)

## Goal

Prove on real AWS what moto cannot: the IAM permissions, the real SQS behaviour, and above all the
full pipeline and the dead-letter path.

## Setup

* Identity: the `vectorpipe-dev` role (temporary credentials from `aws sts assume-role`).
* Real S3 bucket and SQS queues in `us-east-1`; settings passed as environment variables.
* A throwaway SQLite database, so no test rows went into the developer's PostgreSQL.

## 1. Smoke test of the backend classes

| Check | Result |
|---|---|
| S3 put / get / delete | ✅ |
| Missing key → `ObjectNotFoundError` (needs `s3:ListBucket`) | ✅ |
| Redrive policy check at startup | ✅ |
| Send → receive (count 1) → `change_visibility(0)` → receive (count 2) → delete | ✅ |
| `stats()` on queue and DLQ (needs `GetQueueAttributes` on both) | ✅ |

## 2. Full pipeline (real API and worker processes)

1. `POST /documents` → 202, `PENDING`.
2. Worker: `PROCESSING` → `COMPLETED` in about 3 s, `attempts: 1`, no error.
3. S3 held `raw/<id>` (6000 bytes) and `processed/<id>.json` (959 bytes); `/result` returned
   3 chunks × 16 dimensions.
4. Queue afterwards: visible 0, in flight 0, dead 0 — the message was acknowledged.
5. SIGTERM: the worker logged "finishing the current job", then "worker stopped".

## 3. Dead-letter path

A worker ran with `SIMULATE_FAILURE_RATE=1` (every attempt fails), `RETRY_BACKOFF_SECONDS=1`.

| Attempt | receive_count | final | Row status |
|---|---|---|---|
| 1 | 1 | false | `PENDING`, attempts 1 |
| 2 | 2 | false | `PENDING`, attempts 2 |
| 3 | 3 | true | `FAILED`, attempts 3, error stored |

Then SQS itself moved the message to `vectorpipe-jobs-dlq` (its receive count there was 4: the
delivery that triggered the move). Reading the DLQ showed the poison document's job body, and
`/stats` reported `dead: 1`.

## Cleanup

The DLQ message and both documents' S3 objects were deleted. Bucket: 0 objects; both queues: 0
messages.

## How to repeat

1. Set in `.env`: `STORAGE_BACKEND=s3`, `QUEUE_BACKEND=sqs`, `AWS_REGION=us-east-1`,
   `AWS_PROFILE=vectorpipe-dev`, and `S3_BUCKET`, `SQS_QUEUE_URL`, `SQS_DLQ_URL` from
   `terraform output`.
2. `alembic upgrade head`, then `python -m app.api` and `python -m app.worker`.
3. Upload: `curl -F "file=@doc.txt" http://127.0.0.1:8000/documents`, then poll
   `GET /documents/<id>`.
4. DLQ path: start the worker with `SIMULATE_FAILURE_RATE=1` and upload again.
5. Inspect with `aws s3 ls s3://<bucket>/ --recursive --profile vectorpipe-dev`.
   (The role cannot read the DLQ; use your own user for `aws sqs receive-message` on it.)

## Not covered yet

* PostgreSQL instead of SQLite on the live run.
* Killing a worker mid-job on AWS (visibility-timeout redelivery + stale-claim reclaim are only
  covered by local tests).
