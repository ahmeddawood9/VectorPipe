# Step 3: Somewhere safe for Terraform state

*Commits: "Add Terraform remote state bootstrap and S3 backend", "Update README and add
CI/docs placeholders", "Ignore Terraform plans, crash logs and overrides".*

## What I was trying to do

Before creating any infrastructure I wanted Terraform's state somewhere safe: not on my laptop, and
never in git.

## What I built

A small, separate stack in `terraform/bootstrap/` that creates one S3 bucket,
`ingest-pipeline-tfstate-dev`, in `us-east-1`. I locked it down:

- **Versioning**, so I can roll back a bad state. Old versions expire after 90 days.
- **Encryption**, because state can hold sensitive values.
- **Public access blocked and ACLs off.**
- **A bucket policy that refuses anything not over TLS.**
- **`prevent_destroy`**, so a stray `terraform destroy` can't take it out.

Locking uses S3's native lock file (`use_lockfile`, Terraform 1.10+), so I didn't need a DynamoDB
table.

## The chicken-and-egg part

The bootstrap stack wants to keep its own state in the bucket it creates. You can't use a bucket that
doesn't exist yet, so the first apply runs with local state. Then you add the backend and run
`terraform init -migrate-state`. The steps are in `terraform/bootstrap/README.md`. Its state now
lives at `bootstrap/terraform.tfstate`.

## Smaller things in this step

- I added badges and an Infrastructure section to the README, plus empty `.github/workflows/` and
  `docs/` folders as placeholders.
- `.gitignore` now ignores saved plans, `crash.log` and override files. Plans can contain secrets, so
  they should never be committed.

## Checking it

```bash
aws s3 ls s3://ingest-pipeline-tfstate-dev/ --recursive
```

You should see `bootstrap/terraform.tfstate`, and later one state file per layer.

## Still open

The backend existed, but there was nothing in the main stack yet.
