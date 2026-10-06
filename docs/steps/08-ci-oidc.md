# Step 8: CI, and pushing images without AWS keys

*Commits: "Add CI: run tests, push the image to ECR from main", "Isolate tests from ambient
backend and AWS settings", "Add the GitHub OIDC provider and CI role for ECR pushes".*

## What I was trying to do

Run the tests on every push, and on `main` also build the Docker image and push it to ECR, tagged
with the commit SHA. I didn't want any AWS keys stored in GitHub.

## What I built

**`.github/workflows/ci.yml`** has two jobs:

- `test` runs on every pull request and push: Python 3.12 (the same as the Dockerfile), install the
  requirements, run `pytest`.
- `push-image` runs only on pushes to `main`, and only if `test` passed. It gets temporary AWS
  credentials through OIDC, logs in to ECR, then builds and pushes `vectorpipe:<git-sha>`.

**`terraform/ci.tf`** sets up the AWS side:

- an OIDC provider, so AWS trusts tokens that GitHub Actions signs (one per account)
- a role, `vectorpipe-ci`, that only this repo's `main` branch can use
- a policy on that role allowing ECR login and pushing to the `vectorpipe` repository only

The role's ARN goes into a GitHub repo **variable**, `AWS_ROLE_ARN`. It's a variable, not a secret,
because an ARN isn't a credential.

## How OIDC works, the way I think about it

It's like a guest list at a door. GitHub hands the job a signed ID card, and the card says who it is
(`sub`) and who it's meant for (`aud`). AWS checks that the card really comes from GitHub, then checks
the role's guest list: right `aud`, and an exact match on `sub`. If everything matches, the job gets
temporary credentials that expire when it ends. No keys are stored anywhere.

## What went wrong

**1. A test that only failed in CI.** The workflow sets `AWS_REGION` for every job. My settings read
environment variables, so one test, "S3 without a region should be rejected", suddenly had a region,
and the error it expected never came. I reproduced it locally with:

```bash
AWS_REGION=us-east-1 pytest tests/test_aws_backends.py -k incomplete
```

To fix it, a fixture in `tests/conftest.py` now clears the backend and AWS variables before every test,
so tests only see what they set themselves. That also protects against an exported `AWS_PROFILE`
quietly pulling real credentials into the moto tests.

**2. "Not authorized to perform sts:AssumeRoleWithWebIdentity".** This one took some digging. I
installed the GitHub CLI so I could read the logs, and compared what the role expected with what GitHub
actually sent:

| | `sub` |
|---|---|
| The role expected | `repo:ahmeddawood9/VectorPipe:ref:refs/heads/main` |
| GitHub sent | `repo:ahmeddawood9@147309010/VectorPipe@1398670261:ref:refs/heads/main` |

My repo has GitHub's **immutable subject** turned on, so the token includes the numeric owner and repo
IDs. AWS needs an exact match, so it said no.

I could have turned the setting off, but I kept it, because it's the safer option. If the repo were
ever renamed, or deleted and someone recreated `ahmeddawood9/VectorPipe`, the new repo would get
different IDs and couldn't use my role. So I changed `ci.tf` to build the `sub` from the owner and repo
IDs. The plan showed one change, the trust policy, and nothing else.

The trade-off: if I ever recreate the repo myself, I'll have to update the IDs before CI can push
again. That's the behaviour I want, but it's easy to forget.

**3. A stuck Terraform lock.** An earlier version of `ci.tf` got applied, and the plan run after it was
killed partway, which left a lock on the state. I removed the lock, then renamed the policy in the
state (`terraform state mv`) to match the final file. Otherwise Terraform would have deleted one
policy and created an identical one, and depending on the order, the role could have ended up with no
policy at all.

## Checking it

```bash
gh run list --limit 3                       # test ✓, push-image ✓
aws ecr describe-images --repository-name vectorpipe --region us-east-1 \
  --query 'imageDetails[].imageTags'        # includes the commit SHA
gh api repos/<owner>/<repo>/actions/oidc/customization/sub   # must match the role's sub
cd terraform && terraform plan              # no changes
```

After the fix, the next push to `main` went green straight away, and the image showed up in ECR
tagged with that commit.

## Still open

- The workflow's actions now run on Node 24 (`checkout` v7, `setup-python` v7, `configure-aws-credentials`
  v6; `amazon-ecr-login` v2 already did). `ubuntu-latest` is about to move to Ubuntu 26, which I'll watch.
- The Dockerfile still has no `CMD` and runs as root.
- The images aren't deployed anywhere yet.
