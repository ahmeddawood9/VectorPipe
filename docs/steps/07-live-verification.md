# Step 7: Proving it on real AWS

*October 3 and 4. No code changes; this page is the record.*

## What I was trying to do

moto proved the logic. It couldn't prove the IAM permissions or how real SQS behaves. So I ran the
real thing against real AWS, as the `vectorpipe-dev` role.

## Round 1: the backend classes on their own

I used the app's own `S3ObjectStorage` and `SqsQueue`, nothing mocked:

- S3 put, get and delete all worked, and a missing key came back as "not found". That proved
  `ListBucket` was in the policy.
- The redrive policy check passed at startup.
- I sent a message and received it (count 1), made it visible again, received it again (count 2), then
  deleted it.
- `stats()` read both the queue and the DLQ. That's the `GetQueueAttributes` permission moto couldn't
  check.

## Round 2: the whole pipeline

I started the real API and worker against live S3 and SQS. To keep test rows out of my dev database, I
used a throwaway SQLite file.

- I uploaded a document and got `202 PENDING`. It went to `PROCESSING`, then `COMPLETED` in about 3
  seconds, on the first attempt.
- S3 had `raw/<id>` and `processed/<id>.json`, and the result endpoint returned the embeddings.
- The queue was empty afterwards, so the message had been acknowledged.
- `SIGTERM` made the worker finish and exit cleanly.

## Round 3: making a job fail on purpose

I started a worker with `SIMULATE_FAILURE_RATE=1`, so every attempt fails.

| Attempt | Receive count | Last attempt? | Row |
|---|---|---|---|
| 1 | 1 | no | `PENDING`, 1 attempt |
| 2 | 2 | no | `PENDING`, 2 attempts |
| 3 | 3 | yes | `FAILED`, 3 attempts, error saved |

After that, SQS moved the message to the DLQ by itself. I read the DLQ to make sure it was really that
job, and it was. `/stats` showed `dead: 1`.

## Round 4: killing a worker in the middle of a job

This was the test I cared most about. Same setup, except with Postgres (in a throwaway container) and
two workers. For credentials I used `AWS_PROFILE=vectorpipe-dev` from my real `~/.aws/config`. To keep
the run short, I set a 20-second visibility timeout and made each job take 10 seconds.

| Time | What happened |
|---|---|
| 0 s | Worker 1 picks up the job. Status `PROCESSING`, attempt 1 |
| ~3 s | I `kill -9` worker 1. No shutdown and no acknowledgement; the row is stuck at `PROCESSING` |
| ~21 s | The visibility timeout runs out and SQS gives the job to worker 2 (receive count 2). The row has been still for longer than 16 s (80% of the timeout), so worker 2 takes it over: attempt 2 |
| ~34 s | Worker 2 finishes. `COMPLETED`, attempt 2. Both files in S3, queue and DLQ empty |

Nothing was lost, nothing ended up in the DLQ, and only one worker finished the job.

## Round 5: Postgres with two workers

I uploaded six documents at once. All six completed on the first attempt in about 30 seconds, split
3 and 3 between the two workers.

## Cleaning up

After every round I deleted the test files from S3, consumed any messages I'd left, stopped the
processes and removed the Postgres container.

## Still open

- This isn't a real cluster yet, and it isn't using real workload roles.
- I haven't tried killing a worker when its job is already close to the visibility timeout. Here the
  job took 10 seconds against a 20-second timeout.
