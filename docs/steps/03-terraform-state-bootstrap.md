# Step 3 — Terraform remote state bootstrap

**Date:** 2026-10-02 · **Commits:**
`f927c7a` Add Terraform remote state bootstrap and S3 backend ·
`39d2428` Update README and add CI/docs placeholders ·
`69681ef` Ignore Terraform plans, crash logs and overrides

## Goal

Before creating any infrastructure, give Terraform a safe, shared place to keep its state, so
state is never on a laptop or in git.

## What was built

`terraform/bootstrap/` is a small one-time stack that creates the state bucket
`ingest-pipeline-tfstate-dev` in `us-east-1`:

| Protection | Why |
|---|---|
| Versioning (old versions expire after 90 days) | Roll back a corrupted state |
| SSE-S3 encryption | State can contain sensitive values |
| Full public access block + ACLs disabled | State is never public |
| Bucket policy denying non-TLS requests | No plaintext transport |
| `prevent_destroy` | `terraform destroy` cannot delete it by accident |

Locking uses S3-native `use_lockfile` (Terraform >= 1.10), so no DynamoDB table is needed.

The chicken-and-egg problem (the stack's backend is the bucket it creates) is solved by applying
once with local state and then running `terraform init -migrate-state`. The bootstrap's own state
lives at `bootstrap/terraform.tfstate` in the bucket. Steps are in
[`terraform/bootstrap/README.md`](../../terraform/bootstrap/README.md).

The other two commits:

* `39d2428` added README badges, a Docker section and an Infrastructure section, plus the
  `.github/workflows/` and `docs/` placeholders.
* `69681ef` git-ignores `tfplan` / `*.tfplan`, `crash.log` and `override.tf` files, so plans (which
  can contain secrets) are never committed.

## How to verify

```bash
aws s3 ls s3://ingest-pipeline-tfstate-dev/ --recursive
# bootstrap/terraform.tfstate, and (after Step 6) foundation/terraform.tfstate
```

## Left open

The backend was configured, but the main stack had no resources yet. The state key was later
changed to `foundation/terraform.tfstate` in [Step 6](06-foundation-infrastructure.md).
