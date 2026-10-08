# Step 12: Wiring the app to AWS from inside the cluster

*Commit: "Add the workloads layer and the Kubernetes bootstrap manifests".*

## What I was trying to do

The cluster exists, but nothing on it can reach AWS yet. Before I put the API and worker on it I wanted
three things in place: each pod gets only the AWS permissions it needs, the database password reaches the
pods without me ever handling it, and the pods can actually connect to Postgres. This step writes all of
that down. I haven't applied any of it, because the layers it reads are destroyed between sessions.

The layer was drafted outside the repo and had never been run, so I read every file before using it.

## What I added

**`terraform/workloads/`** is another session layer with its own state (`workloads/terraform.tfstate`).
It reads the foundation, data and eks layers through `terraform_remote_state` and creates:

| Piece | What it does |
|---|---|
| Pod Identity role `api-pod` | Attaches the foundation's `api` policy (S3 read/write/delete, SQS send). Bound to the `api` service account in the `vectorpipe` namespace. |
| Pod Identity role `worker-pod` | Attaches the foundation's `worker` policy (S3 read raw / write processed, SQS receive/delete/visibility). Bound to the `worker` service account. |
| Role `external-secrets` | Lets the External Secrets controller read **one** secret, the database one. Bound to its service account in the `external-secrets` namespace. |
| Security group rule | Lets the cluster's security group reach the database on 5432. Referenced by security group ID, never by a CIDR. |

Pod Identity works like this: instead of putting AWS keys in the pods, a pod that runs as a given service
account gets a given role, and the node's pod identity agent hands it temporary credentials. boto3 picks
them up without any setting from me. That is also why the app needs no `AWS_PROFILE` here.

**The app pods never get Secrets Manager access.** Only the External Secrets controller can read the
database secret, and it copies the username and password into an ordinary Kubernetes Secret
(`db-credentials`) that the pods use.

**`k8s/bootstrap/`** is the first set of manifests, applied in order:

| File | What it is |
|---|---|
| `00-namespace.yaml` | the `vectorpipe` namespace |
| `10-serviceaccounts.yaml` | the `api` and `worker` service accounts |
| `20-secretstore.yaml` | a `ClusterSecretStore` pointing at AWS Secrets Manager, with no auth block because the controller uses its own Pod Identity credentials |
| `30-externalsecret.yaml` | the `db-credentials` Secret, built from the RDS-managed password |

The password goes through `urlquery` on its way into the Secret, so a special character can't break the
connection URL later.

## What I checked before trusting it

I wrote the checks down because the files had never run anywhere:

| Check | Result |
|---|---|
| Outputs the layer reads: `api_policy_arn`, `worker_policy_arn`, `db_security_group_id`, `db_secret_arn`, `cluster_name`, `cluster_security_group_id` | all six exist in the other layers' code |
| Backend (bucket, region, `use_lockfile`, `encrypt`) and AWS provider `~> 6.0` | identical to the other layers (only `bootstrap` is still on `~> 5.0`, which was already so) |
| Service account names | `api` and `worker` in the YAML match the associations in `main.tf`; the namespace `vectorpipe` matches too |
| `terraform fmt`, `init`, `validate` | pass; `init` wrote nothing to the remote state |

Two things that aren't a mismatch but are easy to trip over:

- **The External Secrets service account isn't in my YAML.** The association expects
  `external-secrets/external-secrets`, which is the name the Helm chart creates. So I install the chart into
  the `external-secrets` namespace and leave its service account name alone. The association is looked up
  by name, so it doesn't matter whether it exists before the chart does.
- **The `ExternalSecret` is a template.** It contains `REPLACE_DB_SECRET_ARN`, which has to be substituted
  with the real ARN, and the ARN changes every time the database is recreated. Applying the file as it
  stands fails. The command is in the file's header:

  ```bash
  sed "s|REPLACE_DB_SECRET_ARN|$(terraform -chdir=terraform/data output -raw db_secret_arn)|g" \
    k8s/bootstrap/30-externalsecret.yaml | kubectl apply -f -
  ```

The `ClusterSecretStore` and `ExternalSecret` use `apiVersion: external-secrets.io/v1`. That depends on which
version of the controller I install, so I check it against the CRDs before applying.

## What the app needs on Kubernetes

I read `app/config/settings.py`, `.env.example` and `docker-compose.yml`. Compose runs on local files; on the
cluster everything points at AWS. These are the variables, and where each value comes from.

**Both the API and the worker:**

| Variable | Value | Comes from |
|---|---|---|
| `DATABASE_URL` | `postgresql://$(DB_USER):$(DB_PASSWORD_ENCODED)@<db_endpoint>:<db_port>/<db_name>?sslmode=require` | assembled in the pod spec from the two Secret keys below and the three data-layer outputs |
| `DB_USER` | the RDS master user | **Secret** `db-credentials` (External Secrets, from Secrets Manager) |
| `DB_PASSWORD_ENCODED` | the URL-encoded password | **Secret** `db-credentials` |
| `STORAGE_BACKEND` | `s3` | ConfigMap (constant) |
| `QUEUE_BACKEND` | `sqs` | ConfigMap (constant) |
| `AWS_REGION` | `us-east-1` | ConfigMap, from the foundation output `region` |
| `S3_BUCKET` | the documents bucket | ConfigMap, from the foundation output `s3_bucket` |
| `SQS_QUEUE_URL` | the jobs queue | ConfigMap, from the foundation output `sqs_queue_url` |
| `SQS_DLQ_URL` | the dead-letter queue | ConfigMap, from the foundation output `sqs_dlq_url` |
| `QUEUE_VISIBILITY_TIMEOUT_SECONDS` | `60` | ConfigMap; must equal the queue's visibility timeout |
| `QUEUE_MAX_RECEIVE_COUNT` | `3` | ConfigMap; must equal the redrive policy's `maxReceiveCount` |
| `LOG_LEVEL` | `INFO` | ConfigMap (optional) |

`DB_USER` and `DB_PASSWORD_ENCODED` have to be defined *before* `DATABASE_URL` in the container's `env`
list, because Kubernetes only expands `$(VAR)` references to variables defined earlier.

**API only:**

| Variable | Value | Why |
|---|---|---|
| `API_HOST` | `0.0.0.0` | the default is `127.0.0.1`, so without this the probes and the Service can't reach the pod |
| `API_PORT` | `8000` | the default; set it to be explicit |
| `MAX_UPLOAD_BYTES` | optional | default is 10 MB |

**Worker only:**

| Variable | Value | Why |
|---|---|---|
| `PROCESSING_DELAY_SECONDS` | default `3` | the simulated work; keep it below the visibility timeout |
| `RETRY_BACKOFF_SECONDS` | default `2` | first retry delay, doubles each attempt |
| `WORKER_METRICS_PORT` | `9101` | every pod has its own IP, so the clash I had locally doesn't happen |

**Deliberately not set:** `AWS_PROFILE` (Pod Identity supplies the credentials), `LOCAL_STORAGE_ROOT` and
`QUEUE_POLL_INTERVAL_SECONDS` (only used by the local backends), and `SIMULATE_FAILURE_RATE`.

Other things the manifests will need:

- The image comes from ECR (`ecr_repository_url`, tagged with the git SHA). The Dockerfile has no `CMD`, so
  each Deployment sets `command` to `python -m app.api` or `python -m app.worker`.
- Migrations used to run when the API started under compose. On the cluster I'd run
  `alembic upgrade head` as its own Job or init container with the same `DATABASE_URL`, so two API replicas
  don't both try to migrate.
- The ConfigMap values come from `terraform output`. Several of them change when a layer is rebuilt, so I'll
  generate the ConfigMap from the outputs and not hand-write it.

## Order

```
build:    network → data → eks → workloads
destroy:  workloads → eks → data → network
```

The security group rule lives in the workloads state but sits on a security group the data layer owns, so
workloads has to go before data.

## Still open

- Nothing is applied. The layers it reads are destroyed, so a plan would fail, and I haven't run one.
- The Deployments, the Service, the ConfigMap and the migration Job still need writing.
- The External Secrets controller hasn't been installed, so the `ExternalSecret` has never synced. I haven't
  confirmed that it can decrypt the RDS-managed secret; I'll see that on the first sync.
- The Dockerfile still has no `CMD` and runs as root.
