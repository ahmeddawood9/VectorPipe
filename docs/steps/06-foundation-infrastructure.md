# Step 6: The foundation: S3, SQS, ECR and IAM

*Commits: "Set up the foundation Terraform layer: provider 6.x and variables", "Add the
documents S3 bucket", "Add the jobs SQS queue with a dead-letter queue", "Add the ECR repository for
the application image", "Add least-privilege IAM policies and a local dev role", "Expose the values
the app needs as Terraform outputs", "Fix the state key in the bootstrap backend_config output", "Add
terraform.tfvars.example and ignore Terraform config archives", "Document the foundation
infrastructure in the README".*

## What I was trying to do

Create the real AWS resources the app needs, all in one region, with each part allowed only what it
needs.

## What I built (all in `us-east-1`)

- **S3 bucket `vectorpipe-documents-<account-id>`**: public access blocked, encrypted with SSE-S3
  (which matches what the app asks for), TLS only. I left `force_destroy` off, so Terraform won't
  delete it while it holds documents.
- **SQS queue `vectorpipe-jobs`**: 60-second visibility timeout, 4-day retention, encrypted, and a
  redrive policy that sends a message to the DLQ after 3 receives.
- **Dead-letter queue `vectorpipe-jobs-dlq`**: 14-day retention, longer than the main queue, so failed
  jobs stay around long enough to look at.
- **ECR repository `vectorpipe`**: immutable tags (images are tagged with the git SHA, never `latest`),
  scanned on push, keeps the last 10 images.
- **IAM**: a policy for the API, a policy for the worker, and a `vectorpipe-dev` role with both, which my
  user can assume.
- **Outputs** for everything the app needs: bucket, queue URLs, region, ECR URL, role ARNs.

## Who can do what

| Permission | API | Worker | Why |
|---|---|---|---|
| `s3:PutObject` | raw/*, processed/* | processed/* | upload; write results |
| `s3:GetObject` | raw/*, processed/* | raw/* | serve results; read input |
| `s3:DeleteObject` | raw/*, processed/* | none | undo a failed submission |
| `s3:ListBucket` | the bucket | the bucket | without it, a missing key comes back as 403 instead of 404 |
| `sqs:SendMessage` | jobs | none | enqueue |
| `sqs:ReceiveMessage`, `DeleteMessage`, `ChangeMessageVisibility` | none | jobs | consume, acknowledge, back off |
| `sqs:GetQueueAttributes` | jobs + DLQ | jobs + DLQ | the startup redrive check, `/stats`, `/metrics` |

The `ListBucket` one isn't obvious. Without it, S3 won't tell you a key doesn't exist. It just says
access denied, and the app would treat a missing file as an error.

## How it actually went

**The Terraform came as a zip, and it didn't fit as-is.** Its `providers.tf` had its own backend
block, which clashed with the `backend.tf` I already had, and the bucket name was a placeholder. I
merged them into one block in `backend.tf` that points at the real state bucket.

**Provider upgrade.** The new config needed AWS provider 6.x, but my lock file pinned 5.100.0, so I
ran `terraform init -upgrade`.

**New state key.** I moved this stack's state to `foundation/terraform.tfstate`, one file per layer.
Nothing existed at the old key, so `init -reconfigure` was enough. The bootstrap output still printed
the old key, so I fixed that too.

**Two regions by accident.** The resources defaulted to `ap-south-1`, but the state bucket is in
`us-east-1`. That would have worked, but it's confusing, and the app, ECR and later EKS all need to be
in the same region. Nothing had been applied yet, and the state bucket is protected against deletion,
so I moved everything to `us-east-1`. `terraform output region` is now the single answer to "which
region?".

**The first apply only got halfway.** It created the bucket and then stopped: my IAM user had no
permission for SQS, ECR or IAM. I granted those, and the second apply created the remaining 9
resources.

**No more prompts.** `dev_user_arn` lives in `terraform.tfvars`, which git ignores because it contains
my account ID. `terraform.tfvars.example` shows what goes in it.

## Using it

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars   # set dev_user_arn
terraform init
terraform plan        # read the plan: + create, ~ change, - destroy
terraform apply
terraform output      # these go into .env
```

These settings have to match the app's: `max_receive_count` and `QUEUE_MAX_RECEIVE_COUNT`,
`visibility_timeout_seconds` and `QUEUE_VISIBILITY_TIMEOUT_SECONDS`, `region` and `AWS_REGION`.

## Still open

No image in ECR yet, and no CI.
