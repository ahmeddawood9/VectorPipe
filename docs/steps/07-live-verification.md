# Step 7 — Verification on live AWS

**Dates:** 2026-10-03 (runs 1–3), 2026-10-04 (runs 4–6) · **Commits:** none (operational step; results recorded here and in the README)

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

## 4. Worker killed mid-job (crash recovery)

Setup changes for this run and the next two: **PostgreSQL 15** (throwaway container), **two workers**,
credentials from **`AWS_PROFILE=vectorpipe-dev`** in the real `~/.aws/config` (no `AWS_*` variables
set), `QUEUE_VISIBILITY_TIMEOUT_SECONDS=20`, `PROCESSING_DELAY_SECONDS=10`.

| Time | Event |
|---|---|
| 0 s | worker 1 receives the job (receive_count 1); row `PROCESSING`, attempts 1 |
| ~3 s | worker 1 gets **SIGKILL** mid-job: no graceful shutdown, no acknowledgement, row left `PROCESSING` |
| ~21 s | visibility timeout expires; SQS redelivers to worker 2 (receive_count 2). The row is older than the stale threshold (0.8 × 20 s = 16 s), so worker 2 reclaims it: `PROCESSING`, attempts 2 |
| ~31 s | worker 2 finishes: `COMPLETED`, attempts 2; raw and processed objects in S3; queue empty, DLQ empty |

The job was not lost, not dead-lettered and not processed twice to completion.

## 5. Flow on PostgreSQL with two workers

Six documents uploaded at once to two workers: all six `COMPLETED` with attempts 1 within about
30 s, split 3/3 between the workers (each SQS message went to exactly one worker). Database:
`COMPLETED|1|6` plus the crash-test row `COMPLETED|2|1`. Queue and DLQ empty afterwards.

## 6. `AWS_PROFILE` against the real `~/.aws/config`

Both workers logged `storage_backend=s3 queue_backend=sqs`, and every AWS call above went through
the `vectorpipe-dev` profile, so boto3 assumed the role from `role_arn` + `source_profile` itself.

All test objects were deleted afterwards and the container removed.

## Not covered yet

* Running the app in containers on a cluster (EKS) with workload IAM roles instead of the dev role.
* A worker killed while its job is already near the visibility timeout under real embedding load
  (here the job took 10 s against a 20 s timeout).
