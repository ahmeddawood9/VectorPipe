# Step 4 — S3 and SQS backends in the app

**Date:** 2026-10-02 · **Commits:**
`5424e8f` Add S3 and SQS backends selectable by settings ·
`c95f4fc` Document the S3/SQS backends and AWS setup

## Goal

Let the app use Amazon S3 and SQS instead of local files and SQLite, without changing the API or
worker logic, and without breaking the local default.

## Mental model: plugs and sockets

```
ObjectStorage ──┬── LocalObjectStorage   (files)
                └── S3ObjectStorage      (S3 bucket)        ← new
Queue ──────────┬── LocalQueue           (SQLite)
                └── SqsQueue             (SQS + DLQ)        ← new
```

Routes and the processor only hold the interface, so `documents.py`, `processor.py` and
`runner.py` did not change.

## What was built

**`S3ObjectStorage`** (`app/services/object_storage.py`)

* put/get/delete with `ServerSideEncryption="AES256"`, standard retries, timeouts.
* `NoSuchKey` → `ObjectNotFoundError`; any other error (e.g. missing bucket) → `ObjectStorageError`.
* Key validation moved into a shared `validate_key()`, so both backends accept the same keys.

**`SqsQueue`** (`app/services/queue.py`)

| App call | SQS call |
|---|---|
| `enqueue` | `send_message` |
| `receive` | `receive_message` with `MessageSystemAttributeNames` (the non-deprecated name), max 10 per batch, long-polled in short chunks so SIGTERM is noticed promptly |
| `delete` | `delete_message` |
| `change_visibility` | `change_message_visibility` (clamped to 12 h) |
| `stats` | `get_queue_attributes` on the queue **and** the DLQ (cached 5 s) |

**Settings and wiring**

* `STORAGE_BACKEND` (`local`/`s3`), `QUEUE_BACKEND` (`local`/`sqs`), `AWS_REGION`, `S3_BUCKET`,
  `SQS_QUEUE_URL`, `SQS_DLQ_URL`. Missing fields for a chosen backend fail at startup.
* `app/services/factory.py` (`build_storage`, `build_queue`) replaces the hardcoded local classes
  in `app/api/main.py` and `app/worker/__main__.py`.

## Key design decisions

* **SQS does the dead-lettering.** Locally `LocalQueue` dead-letters; on AWS the queue's redrive
  policy does. The app must agree on the number (3) to know which attempt is final, so `SqsQueue`
  reads the redrive policy at startup: no policy is an error, a different count is a warning.
* **`delete() == True` does not prove the message is gone.** SQS cannot detect a stale receipt
  handle, so a delete after redelivery can succeed without removing anything. Correctness comes
  from the atomic claim of Step 1, and a test proves it (below).
* **No credentials in the app.** boto3 uses the ambient identity (role, SSO, profile).
* **Rejected for now:** presigned URLs. They change the interface and API response, and were not
  needed to get the app working on S3 and SQS.

## Tests

`tests/test_aws_backends.py` runs against **moto** (in-process fake AWS, no account needed):

* The same contract tests run on the local and the AWS implementation of each interface.
* S3: encryption at rest, error mapping. SQS: batch cap, long-poll wake-up, invalid handles,
  missing redrive policy, count-mismatch warning, stats cache.
* Redelivery uses `change_visibility(0)` instead of sleeping, to keep the suite fast and stable.
* **Duplicate delivery:** one job is delivered twice with different receipt handles; the first is
  `COMPLETED`, the second `DUPLICATE`, the row has `attempts == 1` and a correct result.
* While writing these tests, a real bug surfaced: SQS omits the `Attributes` key when the requested
  attribute is unset. It was fixed before the commit.

Result: 173 passed (122 existing, 51 new).

## How to verify

```bash
pip install -r requirements.txt     # boto3, moto
pytest -q tests/test_aws_backends.py
```

If `pytest` fails with `No module named 'botocore'`, the active virtualenv is not the one the
requirements were installed into (`which pytest` shows which one is used).

## Left open

Moto does not enforce IAM, so missing permissions could only show up on real AWS (checked in
[Step 7](07-live-verification.md)).
