# Step 13: Packaging the app for Kubernetes

*Commit: "Add the Helm chart for the API, worker and migration job".*

## What I was trying to do

Step 12 listed every environment variable the app needs on Kubernetes. This step turns that list into
something I can install: a Helm chart that runs the API, the worker and the database migration from the
one image CI pushes to ECR.

The chart was drafted outside the repo and had never been rendered, so before trusting it I read every
file, checked each assumption in it against the real app, and rendered it. I haven't installed it
anywhere. The cluster and database don't exist between sessions, and I kept everything off AWS for this
step.

## What's in it

`charts/vectorpipe/` is the chart, and `scripts/gen-values.sh` generates the values that depend on AWS.

| Piece | What it does |
|---|---|
| `ConfigMap` | `STORAGE_BACKEND=s3`, `QUEUE_BACKEND=sqs`, the region, bucket and queue URLs, and the two queue numbers that must match Terraform |
| API `Deployment` + `Service` | two replicas running `python -m app.api`, probed on `/health`; the Service is a plain `ClusterIP`, so for now I reach it with `kubectl port-forward` |
| Worker `Deployment` | one replica running `python -m app.worker`, with a 120 second grace period so `SIGTERM` lets the current job finish; a TCP liveness probe on the metrics port |
| Migration `Job` | `alembic upgrade head`, as a Helm `pre-install,pre-upgrade` hook so it runs once before the pods start |
| `gen-values.sh` | writes `values-aws.yaml` from the Terraform outputs; that file is git-ignored because it holds the account ID and endpoints |

The database URL is assembled in the pod spec from the `db-credentials` Secret (step 12) and the database
host from `values-aws.yaml`. The two Secret variables are defined before `DATABASE_URL` in the env list,
because Kubernetes only expands `$(VAR)` for variables defined earlier. The migration is a Job and not an
init container on purpose: with two API replicas, both would try to migrate.

## What I checked against the real app

| | Question | Finding |
|---|---|---|
| a | The API's real health route | `GET /health`, returns `{"status":"ok"}`. It's the only one (`/healthz`, `/ready`, `/readyz` and `/livez` all 404). It does **not** check the database or the queue, so a pod can be "ready" while Postgres is unreachable. I set `api.probePath: /health`. |
| b | Does the worker's metrics server listen from startup? | Yes, in this order: database check, build the storage and queue backends, **then** start the metrics server, then the job loop. I connected to port 9101 and read `/metrics`. Two consequences: a worker that can't reach Postgres exits and never opens the port, and if binding the port fails the worker only logs a warning and carries on, so the TCP liveness probe would then restart it. |
| c | The entry points | `python -m app.api` and `python -m app.worker` both exist and both ran. |
| d | Are `alembic.ini` and the migrations in the image, and is `alembic` on `PATH`? | Yes. I built the image and ran `docker run --rm --entrypoint sh <image> -c 'which alembic; ls'`: `/usr/local/bin/alembic`, `alembic.ini` and `migrations/` are all in `/app`. |
| e | Root or non-root? | The Dockerfile has no `USER`, so it runs as **root**. It runs fine as a numeric non-root UID: as `10001` (which has no passwd entry) the migration applied, `/health` returned 200, the worker's metrics were served, and nothing was written under `/app`. I added `runAsNonRoot: true` and `runAsUser: 10001` to the API, worker and migration pods, with the UID in `values.yaml`. |
| f | Do the Terraform outputs `gen-values.sh` reads exist? | All eight do: `ecr_repository_url`, `region`, `s3_bucket`, `sqs_queue_url` and `sqs_dlq_url` in `terraform/`, and `db_endpoint`, `db_port` and `db_name` in `terraform/data/`. The script's paths match the layout, so nothing needed changing. |

For (d) and (e) I first built the image from an export of the committed files and not from my working
folder. The folder holds about 5 GB of Terraform provider caches that the old `.dockerignore` didn't
exclude, so a plain `docker build .` tried to upload all of it. CI builds from a clean checkout, so it
never saw them. I fixed the `.dockerignore` afterwards (below). The test images were local only and I
deleted them.

## What I changed in the draft

Only these, and everything else is as it arrived:

- `api.probePath` is confirmed as `/health` and the "CONFIRM" comment is replaced with what I found.
- The pod-level non-root setting from (e), through one small helper in `_helpers.tpl`.
- The worker's "CONFIRM" comment is replaced with what (b) found.

## Rendering it

I used Helm v4.3.0, downloaded as the official release binary, because installing it system-wide needs
root.

```bash
helm lint charts/vectorpipe --set image.repository=x --set image.tag=y --set aws.region=us-east-1 \
  --set aws.s3Bucket=b --set aws.sqsQueueUrl=q --set aws.sqsDlqUrl=d --set db.host=h
helm template vp charts/vectorpipe <the same --set flags>
```

Lint passes, with only a note that the chart has no icon. The template renders five documents
(ConfigMap, Service, two Deployments, Job) and all of them parse as YAML. Without the values, the
`required` checks stop the render and name the missing one, which is what I want.

## Fixes I made afterwards

Three of the problems I found were small enough to fix straight away.

**`gen-values.sh` now fails loudly.** The problem was that the values were computed inside the heredoc,
where a failing command doesn't stop the script. It now fetches every output into a plain variable first,
rejects an empty one with a message naming it, and only then writes the file. I tested it three ways:

| Case | Result |
|---|---|
| The real script with the data layer destroyed (read-only `terraform output` calls) | `ERROR: output 'db_endpoint' is empty in .../terraform/data (is that layer applied?)`, exit 1, no file written |
| A fake `terraform` returning every output | exit 0, a valid values file, and `helm template` accepts it |
| A fake `terraform` where `db_port` exists but is empty | exit 1, and the good file from the previous case is left untouched |

**The migration Job's container is hardened** like the other two: `allowPrivilegeEscalation: false` and all
capabilities dropped. I rendered the chart and checked all three workloads, and each has the same pod-level
non-root setting and the same container-level settings.

**The `.dockerignore` now excludes** `terraform/`, `**/.terraform/`, `**/*.tfstate*`, `charts/`, `k8s/` and
`docs/` (`.git` and `.venv` were already there). I measured the build context with the classic builder, which
prints "Sending build context to Docker daemon":

| | Build context | Upload time |
|---|---|---|
| Before | **5.56 GB** | 41 s |
| After | **422 kB** | 1 s |

Then I rebuilt the real image from my working folder and ran the same check as before:
`which alembic` finds `/usr/local/bin/alembic`, and `alembic.ini`, `migrations/` and the whole `app/`
package are in `/app`. `terraform`, `charts`, `k8s`, `docs`, `.git`, `.venv`, `tests` and `.env` are
absent, and there isn't a single `*.tfstate*` or `.terraform` anywhere under `/app`. The migration, `/health`
and the worker's metrics still work as UID 10001.

**Env files, which I checked separately.** The old `.dockerignore` already had a bare `.env`, so my real `.env`
was never in an image. But a bare pattern only matches the top-level folder, so other variants were not
covered. I proved it with a throwaway folder containing `.env`, `.env.local`, `.env.production`,
`.env.example`, `app/.env` and `app/sub/.env.staging`: with the old rules everything except `.env` landed in
the image. The rules are now `**/.env` and `**/.env.*`, which excludes all of them. I left `.env.example`
excluded too, since the app never reads it. A rebuild from my working folder, which has a real `.env`,
contains no env-style file at all. CI was never affected, because a clean checkout has no `.env`.

One detail that tripped me: a bare `*.tfstate*` in a `.dockerignore` only matches the top-level folder, so I
wrote `**/*.tfstate*`.

## Using it (not done yet)

```bash
scripts/gen-values.sh                                  # after the layers are up
helm install vp charts/vectorpipe -f charts/vectorpipe/values-aws.yaml \
  --set image.tag=<git sha from CI> -n vectorpipe
```

The `db-credentials` Secret has to exist first (step 12), because the migration Job runs before anything
else and needs it. Otherwise its pod just sits waiting.

## Still open

- **A stale `values-aws.yaml` survives a failed run.** The script leaves an existing file alone when it
  fails, so a file from an earlier session (with an old database endpoint) would still be used. The error
  says the layer isn't applied, but I should delete the file when I tear things down.
- **The `.dockerignore` is still loose in two ways.** Its bare `__pycache__` and `*.pyc` patterns only match
  the top-level folder, so nested ones get copied, and `scripts/`, `.github/` and any stray folder in the
  working directory (I had an untracked one) end up in the image when it's built locally. CI builds from a
  clean checkout, so it only picks up `scripts/` and `.github/`.
- **`/health` doesn't check the database or queue.** A readiness probe that did would keep traffic away from
  a pod that can't work.
- The chart has never been installed, and the Ingress/ALB comes later.
- The Dockerfile still has no `CMD` (the chart sets `command`), and the app still connects as the master
  database user.
