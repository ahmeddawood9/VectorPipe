# Step 6 — Foundation infrastructure: S3, SQS + DLQ, ECR, IAM

**Date:** 2026-10-03 · **Commits:**
`6694942` provider 6.x and variables ·
`2d4de1e` S3 bucket ·
`a960260` SQS queue + DLQ ·
`a744c99` ECR repository ·
`cd9d729` IAM policies and dev role ·
`d648288` outputs ·
`8f2b9a7` bootstrap output fix ·
`ede7fdc` tfvars example, ignore archives ·
`4bd2970` README

## Goal

Create the real AWS resources the app needs, in one region, with least-privilege access.

## What was built (all in `us-east-1`)

| File | Resource | Settings |
|---|---|---|
| `s3.tf` | `vectorpipe-documents-<account-id>` | Public access blocked, SSE-S3 (matches the app), TLS-only policy, no `force_destroy` |
| `sqs.tf` | `vectorpipe-jobs` | Visibility 60 s, retention 4 days, SSE, redrive to the DLQ after 3 receives |
| `sqs.tf` | `vectorpipe-jobs-dlq` | Retention 14 days (longer than the main queue), SSE |
| `ecr.tf` | `vectorpipe` | Immutable tags (tag by git SHA), scan on push, keep last 10 images |
| `iam.tf` | `vectorpipe-api`, `vectorpipe-worker` policies, `vectorpipe-dev` role | See below |
| `outputs.tf` | – | Bucket, queue URLs, region, ECR URL, role and policy ARNs |

**IAM, least privilege**

| Permission | API | Worker | Why |
|---|---|---|---|
| `s3:PutObject` | raw/*, processed/* | processed/* | Upload / write results |
| `s3:GetObject` | raw/*, processed/* | raw/* | Serve results / read input |
| `s3:DeleteObject` | raw/*, processed/* | – | Roll back a failed submission |
| `s3:ListBucket` | bucket | bucket | Without it a missing key is 403, not 404 |
| `sqs:SendMessage` | jobs | – | Enqueue |
| `sqs:ReceiveMessage`, `DeleteMessage`, `ChangeMessageVisibility` | – | jobs | Consume, acknowledge, back off |
| `sqs:GetQueueAttributes` | jobs + DLQ | jobs + DLQ | Startup redrive check, `/stats`, `/metrics` |

The `vectorpipe-dev` role trusts the IAM user in `dev_user_arn` and has both policies, for
[Step 5](05-assume-role-profile.md). Later workload roles attach only the one they need.

## How it got here (decisions and problems)

1. **The config arrived as `terraform/files.zip`.** Its `providers.tf` had its own `terraform` and
   `backend` blocks, clashing with `backend.tf`, and a placeholder bucket name. They were merged
   into one block in `backend.tf` pointing at the real state bucket; `providers.tf` keeps only the
   provider.
2. **Provider upgrade.** The config needs AWS provider `~> 6.0`; the lock file pinned 5.100.0.
   `terraform init -upgrade` updated it.
3. **New state key.** The key changed from `vectorpipe/terraform.tfstate` to
   `foundation/terraform.tfstate` (one state file per layer). Nothing existed at the old key, so
   `init -reconfigure` was used instead of a migration. The bootstrap output that still printed
   the old key was fixed in `8f2b9a7`.
4. **One region.** Resources first defaulted to `ap-south-1` while the state bucket was in
   `us-east-1`. The plan was checked (each resource records its region), then everything was put
   in `us-east-1`: the state bucket is already there and protected by `prevent_destroy`, and
   nothing had been applied yet, so switching cost nothing. App `AWS_REGION`, `.env.example` and
   the CLI default all agree; `terraform output region` is the source of truth.
5. **Partial apply, blocked by permissions.** The first apply created only the bucket. The IAM
   user had no SQS, ECR or IAM permissions (`AccessDenied`). After the account owner granted them,
   the remaining 9 resources were applied.
6. **`dev_user_arn` without prompts.** It lives in `terraform.tfvars` (git-ignored; holds the
   account id). `terraform.tfvars.example` shows the shape.

## How to use

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars   # set dev_user_arn
terraform init
terraform plan          # + create, ~ change, - destroy; stop on any unexpected -
terraform apply
terraform output        # values for .env
```

`terraform plan` only reads; `terraform apply` changes AWS. Run both from `terraform/`.

**Keep in sync with the app:** `max_receive_count` = `QUEUE_MAX_RECEIVE_COUNT`,
`visibility_timeout_seconds` = `QUEUE_VISIBILITY_TIMEOUT_SECONDS`, `region` = `AWS_REGION`.

## How to verify

```bash
terraform plan    # "No changes. Your infrastructure matches the configuration."
```

## Left open

* No image has been pushed to ECR; no CI workflow yet.
* No compute (EKS) or managed database. When the worker gets a Kubernetes Deployment, set
  `terminationGracePeriodSeconds` above the longest job so SIGTERM can finish it.
