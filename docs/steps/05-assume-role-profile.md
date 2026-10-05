# Step 5: Running locally as a real IAM role

*Commit: "Add AWS_PROFILE so the app can run as an assumed role locally".*

## What I was trying to do

Run the app on my laptop with exactly the permissions it will have in AWS, through the
`vectorpipe-dev` role, instead of my own IAM user, which can do a lot more.

## The problem

I assumed I could put `AWS_PROFILE=vectorpipe-dev` in `.env` and be done. That doesn't work. The app
reads `.env` into its own settings, but it never puts those values into the process environment, and
boto3 only looks at real environment variables. So boto3 never sees the profile.

## What I built

- `AWS_PROFILE` is now an app setting.
- One helper, `app/services/aws.py`, creates every boto3 client from a session with that profile and
  region, with the same retries and timeouts for S3 and SQS.
- If the profile isn't set, boto3 behaves exactly as before.

I didn't write any STS code. If a profile has `role_arn` and `source_profile`, boto3 assumes the role
itself and refreshes the temporary credentials before they expire.

## Setting up the profile (once)

Add this to `~/.aws/config`:

```ini
[profile vectorpipe-dev]
role_arn       = arn:aws:iam::<account-id>:role/vectorpipe-dev
source_profile = default
region         = us-east-1
```

Then set `AWS_PROFILE=vectorpipe-dev` in `.env`.

## Checking it

```bash
aws sts get-caller-identity --profile vectorpipe-dev
# ...:assumed-role/vectorpipe-dev/...
aws s3 ls s3://<bucket>/ --recursive --profile vectorpipe-dev
```

The first time I ran that `ls` it printed nothing, and I briefly thought it was broken. It wasn't. The
bucket was just empty. When something really is wrong, you get an error like `AccessDenied`, not
silence.

Anything the app doesn't need gets denied under this role. For example, `aws sqs list-queues` fails.
That's the point.
