# Step 8 — CI: tests on every push, image to ECR via GitHub OIDC

**Date:** 2026-10-04 · **Commits:**
`08f8595` Add CI: run tests, push the image to ECR from main ·
`2977c3f` Isolate tests from ambient backend and AWS settings ·
the commit adding `terraform/ci.tf` and this page

## Goal

Every push runs the tests; every push to `main` that passes them builds the Docker image and stores it
in ECR, tagged with the commit SHA — with **no AWS keys stored in GitHub**.

## What was built

**`.github/workflows/ci.yml`**

| Job | Runs on | Does |
|---|---|---|
| `test` | every pull request and push | Python 3.12 (same as the Dockerfile), `pip install`, `pytest -q` |
| `push-image` | pushes to `main`, after `test` passes | assumes the AWS role from repo variable `AWS_ROLE_ARN` via OIDC, logs in to ECR, builds and pushes `vectorpipe:<git-sha>` |

**`terraform/ci.tf`**

| Resource | Purpose |
|---|---|
| `aws_iam_openid_connect_provider.github` | Lets AWS trust tokens signed by GitHub Actions (one per account) |
| `aws_iam_role.ci` (`vectorpipe-ci`) | Assumable only by a token for **this repo's `main` branch** |
| `aws_iam_role_policy.ci_push` (`ecr-push`) | `ecr:GetAuthorizationToken` (cannot be scoped) + push actions on the `vectorpipe` repository only |

Output `ci_role_arn` → GitHub repo variable `AWS_ROLE_ARN` (a variable, not a secret: an ARN is not a
credential).

## How OIDC works here

```
GitHub Actions job ── signed token (aud, sub) ──▶ AWS STS
                                                   1. OIDC provider: signed by GitHub?          ✅
                                                   2. role trust: aud == sts.amazonaws.com?     ✅
                                                   3. role trust: sub == expected string?       (exact match)
                     ◀── temporary credentials ──  only if all pass; expire after the job
```

`sub` is the token's "who am I": repository and branch.

## How it got here (problems and fixes)

1. **A test failed in CI only.** The workflow sets `AWS_REGION` for every job; `Settings` reads the
   environment, so the "S3 without a region is rejected" test no longer saw a missing region.
   Reproduced with `AWS_REGION=us-east-1 pytest tests/test_aws_backends.py -k incomplete`. Fix
   (`2977c3f`): an autouse fixture in `tests/conftest.py` removes the backend/AWS variables for every
   test, so neither CI nor an exported `AWS_PROFILE` can leak in.
2. **`push-image`: "Not authorized to perform sts:AssumeRoleWithWebIdentity".** The repository uses
   GitHub's **immutable OIDC subject**, so the token's `sub` embeds numeric ids:

   | | `sub` |
   |---|---|
   | role expected | `repo:ahmeddawood9/VectorPipe:ref:refs/heads/main` |
   | GitHub sent | `repo:ahmeddawood9@147309010/VectorPipe@1398670261:ref:refs/heads/main` |

   Fix: `ci.tf` builds the expected `sub` from `github_repo`, `github_owner_id` and `github_repo_id`.
   Kept immutable subjects on rather than disabling them: a renamed or re-created repository with
   the same name gets new ids and cannot assume the role. `terraform plan` showed exactly one in-place
   change (the trust policy) and nothing else.
3. **Terraform state housekeeping.** An apply of an earlier draft of `ci.tf` ran even though it had been
   rejected in the session, and the follow-up plan was killed, leaving a stale state lock. The lock was
   removed and `aws_iam_role_policy.ci` renamed in state to `ci_push` (`terraform state mv`) to match
   the final file, so Terraform did not delete and re-create the `ecr-push` policy.

## How to verify

```bash
gh run list --limit 3                              # test ✓, push-image ✓
aws ecr describe-images --repository-name vectorpipe --region us-east-1 \
  --query 'imageDetails[].imageTags'               # contains the pushed commit SHA
gh api repos/<owner>/<repo>/actions/oidc/customization/sub   # sub_claim_prefix must match the role
cd terraform && terraform plan                     # No changes
```

Verified: run `37194092467` green after the fix; ECR holds `vectorpipe:2977c3f…` (≈229 MB).

## Left open

* Actions target Node 20, which GitHub is deprecating (`checkout@v4`, `setup-python@v5`,
  `configure-aws-credentials@v4`); `ubuntu-latest` moves to Ubuntu 26 from 2026-10-19.
* The Dockerfile still has no `CMD` and runs as root (see [Step 2](02-containerization.md)).
* Images are pushed but not deployed anywhere yet (EKS).
