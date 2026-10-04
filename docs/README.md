# VectorPipe documentation

How VectorPipe was built, one step at a time. Each step has its own page: what was done, why,
how it works, how to verify it, and what was left open. The steps follow the git history, so every
commit on `main` belongs to exactly one step.

| # | Step | Date | Commits |
|---|---|---|---|
| 1 | [Application: API, worker, local storage and queue](steps/01-application.md) | 2026-10-01 | `e62c40a` |
| 2 | [Containerization: Docker and docker-compose](steps/02-containerization.md) | 2026-10-01 | `8e8d132` |
| 3 | [Terraform remote state bootstrap](steps/03-terraform-state-bootstrap.md) | 2026-10-02 | `f927c7a` `39d2428` `69681ef` |
| 4 | [S3 and SQS backends in the app](steps/04-aws-backends.md) | 2026-10-02 | `5424e8f` `c95f4fc` |
| 5 | [Running locally as an assumed IAM role](steps/05-assume-role-profile.md) | 2026-10-03 | `ad878c6` |
| 6 | [Foundation infrastructure: S3, SQS + DLQ, ECR, IAM](steps/06-foundation-infrastructure.md) | 2026-10-03 | `6694942` `2d4de1e` `a960260` `a744c99` `cd9d729` `d648288` `8f2b9a7` `ede7fdc` `4bd2970` |
| 7 | [Verification on live AWS](steps/07-live-verification.md) | 2026-10-03, 2026-10-04 | – (operational, no code change) |
| 8 | [CI: tests, and the image to ECR via GitHub OIDC](steps/08-ci-oidc.md) | 2026-10-04 | `08f8595` `2977c3f` + `terraform/ci.tf` |

## Where things stand

```
                      ┌──────────── AWS us-east-1 ────────────┐
Client ─▶ API ──put──▶│ S3  vectorpipe-documents-<account>     │
           │          │       raw/<id>   processed/<id>.json   │
           └─enqueue─▶│ SQS vectorpipe-jobs ──3 fails──▶ DLQ   │
                      │ ECR vectorpipe:<git-sha>  ◀── CI (OIDC) │
Worker ◀──receive─────│ IAM api/worker policies, dev role      │
  └─ status ─▶ PostgreSQL (local)                              │
                      └────────────────────────────────────────┘
```

* The app runs locally by default and switches to S3 + SQS with two settings.
* The infrastructure exists and was verified end to end on PostgreSQL with two workers, including
  the dead-letter path and a worker killed mid-job (the job was redelivered and completed).
* CI runs the tests on every push and, on `main`, pushes the image to ECR using GitHub OIDC.
* **Not yet done:** running the app on a cluster (EKS) and a managed database.

## Conventions

See [DOCS_INSTRUCTIONS.md](DOCS_INSTRUCTIONS.md) for how to add the next step.
