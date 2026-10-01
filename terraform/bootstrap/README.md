# Remote state bootstrap

Creates the S3 bucket that stores Terraform state for the main stack (`../`).
Locking uses S3-native `use_lockfile` (Terraform >= 1.10), so no DynamoDB table is needed.

The bucket protects state with versioning (old versions expire after 90 days), AES256 encryption,
a full public access block, a TLS-only bucket policy, ACLs disabled, and `prevent_destroy`.

## Already bootstrapped?

The bucket exists and this stack's own state lives at
`s3://ingest-pipeline-tfstate-dev/bootstrap/terraform.tfstate`. Just run `terraform init`.

## First-time bootstrap (new account / environment)

A backend cannot use a bucket that does not exist yet, so:

1. Comment out the `backend "s3"` block in `main.tf`.
2. `terraform init && terraform apply -var environment=<env>`
3. Restore the block (update `bucket` to match `<env>`), then `terraform init -migrate-state`.
4. Delete the leftover local `terraform.tfstate*` files.
5. `terraform output backend_config` prints the settings for the main stack's `backend.tf`.
