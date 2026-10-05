# Step 4: Teaching the app to talk to S3 and SQS

*Commits: "Add S3 and SQS backends selectable by settings", "Document the S3/SQS backends
and AWS setup".*

## What I was trying to do

Use S3 instead of local files and SQS instead of the SQLite queue, without touching the API or worker
logic, and without breaking local development.

## How I thought about it

Think of the two interfaces from step 1 as sockets. Local storage and the local queue were the only
plugs. I just needed a second plug for each:

```
ObjectStorage ──┬── LocalObjectStorage   (files)
                └── S3ObjectStorage      (S3)
Queue ──────────┬── LocalQueue           (SQLite)
                └── SqsQueue             (SQS + DLQ)
```

The routes and the worker only hold the socket, so `documents.py`, `processor.py` and `runner.py`
didn't change at all.

## What I built

**`S3ObjectStorage`** does put, get and delete with server-side encryption, retries and timeouts. A
missing key raises `ObjectNotFoundError`, the same as locally. Anything else, like a missing bucket,
raises a general storage error. I moved key validation into a shared function, so both backends accept
and reject exactly the same keys.

**`SqsQueue`** maps each queue call onto SQS:

| App | SQS |
|---|---|
| enqueue | `send_message` |
| receive | `receive_message`, up to 10 at a time, long-polling in short chunks so the worker still notices `SIGTERM` quickly |
| delete | `delete_message` |
| change visibility | `change_message_visibility` (SQS caps it at 12 hours) |
| stats | `get_queue_attributes` on the queue and on the DLQ, cached for 5 seconds |

**Two settings pick the backend**: `STORAGE_BACKEND` (`local` or `s3`) and `QUEUE_BACKEND` (`local`
or `sqs`), plus the region, bucket and queue URLs. Local is the default. If you choose AWS and forget
a value, the app refuses to start and tells you which one is missing. A small factory builds the right
classes, and the API and worker both use it.

## Things I had to get right

**SQS does the dead-lettering now, not my code.** Locally, my queue moved failed messages itself. On
AWS the queue's redrive policy does it. But the worker still needs to know which attempt is the last
one, so the app and the redrive policy have to agree on the number (3). At startup `SqsQueue` reads the
policy. If there isn't one, it fails. If the number is different, it warns.

**On SQS, a successful delete doesn't prove the message is gone.** If a message has been redelivered,
deleting it with the old receipt handle can "succeed" without removing anything. That sounds scary,
but the atomic claim from step 1 covers it. I wrote a test that delivers the same job twice with
different handles and checks that the document ends up `COMPLETED` once, with the right result.

**No AWS keys in the app.** boto3 uses whatever identity it finds: a role, SSO or a profile.

**I left out presigned URLs.** They'd change the interface and the API response, and I didn't need
them to get onto S3 and SQS. Maybe later.

## Testing without AWS

`tests/test_aws_backends.py` runs on moto, which fakes S3 and SQS in memory. The same contract tests
run against the local and the AWS version of each interface, so the two must behave the same. To
retry a message I set its visibility to 0 instead of sleeping, which keeps the suite fast and stable.

Writing these tests caught a real bug. When none of the requested attributes are set, SQS leaves the
`Attributes` key out of the response completely, and my redrive check crashed on it. I fixed it before
committing.

Result: 173 tests passing, 51 of them new.

## A setup trap I hit

I ran `pytest` and got `No module named 'botocore'`. I had activated a different virtualenv, one level
up, which didn't have the new packages. `which pytest` tells you which one you're using.

## Still open

moto doesn't check IAM permissions, so a missing permission would only show up on real AWS. I tested
that in step 7.
