# Step 5 — Running locally as an assumed IAM role

**Date:** 2026-10-03 · **Commit:** `ad878c6` Add AWS_PROFILE so the app can run as an assumed role locally

## Goal

Run the app on a laptop with exactly the permissions it will have in production (the
`vectorpipe-dev` role from [Step 6](06-foundation-infrastructure.md)), not with the developer's
own, broader IAM user.

## The problem

The app reads `.env` into its settings, but never exports it to the process environment. So
`AWS_PROFILE=...` written in `.env` would never reach boto3, which only reads real environment
variables.

## What was built

* `app/services/aws.py` — `create_client()` builds every boto3 client from
  `boto3.Session(profile_name=..., region_name=...)` with the shared retry and timeout config.
* `S3ObjectStorage` and `SqsQueue` take an optional `profile` and use that helper.
* `Settings.aws_profile` (`AWS_PROFILE`), passed through by the factory. Unset = boto3's default
  credential chain, so nothing changes by default.
* Two tests: the profile reaches boto3 for both backends; no profile means the default chain.

No STS code was written: a profile with `role_arn` + `source_profile` makes boto3 assume the role
and refresh the temporary credentials before they expire.

## Set up the profile (one time)

Append to `~/.aws/config`:

```ini
[profile vectorpipe-dev]
role_arn       = arn:aws:iam::<account-id>:role/vectorpipe-dev   # terraform output dev_role_arn
source_profile = default                                          # profile with your user's keys
region         = us-east-1
```

Then in `.env`: `AWS_PROFILE=vectorpipe-dev`.

## How to verify

```bash
aws sts get-caller-identity --profile vectorpipe-dev
# arn:aws:sts::<account-id>:assumed-role/vectorpipe-dev/...
aws s3 ls s3://vectorpipe-documents-<account-id>/ --recursive --profile vectorpipe-dev
```

An empty listing with exit code 0 is success when the bucket has no documents yet. Permission or
credential problems print an error (`AccessDenied`, `could not be found`).

Expect `AccessDenied` for anything outside the app's needs, e.g. `aws sqs list-queues` — the role
only has the actions listed in Step 6.
