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

For (d) and (e) I built the image from an export of the committed files and not from my working folder.
The folder holds about 5 GB of Terraform provider caches that `.dockerignore` doesn't exclude, so a plain
`docker build .` would have tried to copy them. CI builds from a clean checkout, so it never sees them. The
test image was local only and I deleted it afterwards.

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

## Using it (not done yet)

```bash
scripts/gen-values.sh                                  # after the layers are up
helm install vp charts/vectorpipe -f charts/vectorpipe/values-aws.yaml \
  --set image.tag=<git sha from CI> -n vectorpipe
```

The `db-credentials` Secret has to exist first (step 12), because the migration Job runs before anything
else and needs it. Otherwise its pod just sits waiting.

## Still open

- **`gen-values.sh` fails silently.** I tested it with a fake `terraform` that fails on one output, as the
  real one does when the data layer is destroyed. The script printed the error, still wrote the file, and
  exited 0 with `host:` empty. The chart's `required` checks catch an empty host, region, bucket and queue
  URLs, but not an empty port or name. The fix is small (collect the values into variables first so
  `set -e` can fail), and I haven't made it.
- **The migration Job's container lacks** the `allowPrivilegeEscalation: false` and `drop: ALL` settings the
  API and worker containers have.
- **`.dockerignore`** doesn't exclude `terraform/`, `docs/`, `k8s/`, `charts/` or `.github/`, so the image
  carries them.
- **`/health` doesn't check the database or queue.** A readiness probe that did would keep traffic away from
  a pod that can't work.
- The chart has never been installed, and the Ingress/ALB comes later.
- The Dockerfile still has no `CMD` (the chart sets `command`), and the app still connects as the master
  database user.
