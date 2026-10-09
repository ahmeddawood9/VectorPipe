# Step 14: The first install on the cluster

*No code changes; this page is the record.*

## What I was trying to do

Everything so far was built and checked in pieces: the app on AWS from my laptop, the network, the
database, the cluster, the roles, the Helm chart. This was the first time all of it ran together. The goal
was to put the app on the cluster with no AWS keys and no password that anyone typed, and to prove the
whole path works.

I built the AWS layers myself first (network, data, eks, then workloads) and checked the cluster. The rest
was the part that touches the cluster itself.

## Where I started

Before touching the cluster I checked that what I thought was up really was:

| Layer | Resources in state |
|---|---|
| network | 19 |
| data | 10 |
| eks | 15 |
| workloads | 10 |
| foundation | 16 |

The kube context was the `vectorpipe` cluster, both nodes were `Ready`, and every system pod (`aws-node`,
`coredns`, `kube-proxy`, `eks-pod-identity-agent`) was `Running`. The workloads layer matters here: it had to
exist *before* any pod started, because Pod Identity credentials are injected at the moment a pod is created.

## Part 1: Installing External Secrets

The database password lives in Secrets Manager, and RDS generated it, so I never see it. Kubernetes can't
read Secrets Manager by itself. External Secrets is the helper that copies it into the cluster as a normal
Kubernetes Secret.

```bash
helm repo add external-secrets https://charts.external-secrets.io && helm repo update
helm show values external-secrets/external-secrets | grep -i crd      # confirm the flag name
helm install external-secrets external-secrets/external-secrets \
  -n external-secrets --create-namespace --set installCRDs=true
```

What I checked, and found:

- **The flag name was right.** `installCRDs` still exists, and defaults to true. The chart was v2.12.0.
- **The release name matters.** I named the release `external-secrets`, so the chart creates a service
  account called `external-secrets`, which is exactly the name my Pod Identity association expects. If I had
  picked another release name, the controller would have had no AWS access and I'd have had a confusing
  failure later.
- **All three deployments rolled out:** the controller, the webhook and the cert controller.
- **The CRDs serve `v1`** (and `v1beta1`), so the `apiVersion: external-secrets.io/v1` in my manifests was
  correct. I'd been unsure about this since step 12, and it's why the check mattered.
- **Pod Identity was working for the controller.** Its pod spec had `AWS_CONTAINER_CREDENTIALS_FULL_URI` and
  the token file injected. I tried to confirm it by running a command inside the pod, and that failed because
  the image is distroless and has no shell. That says nothing about the credentials, only that I looked in
  the wrong way. `aws eks list-pod-identity-associations` showed all three associations (external-secrets,
  api, worker).

## Part 2: The bootstrap manifests

```bash
kubectl apply -f k8s/bootstrap/00-namespace.yaml -f k8s/bootstrap/10-serviceaccounts.yaml
kubectl apply -f k8s/bootstrap/20-secretstore.yaml
sed "s|REPLACE_DB_SECRET_ARN|$(terraform -chdir=terraform/data output -raw db_secret_arn)|g" \
  k8s/bootstrap/30-externalsecret.yaml | kubectl apply -f -
```

That creates the `vectorpipe` namespace, the `api` and `worker` service accounts, the store that points at
Secrets Manager, and the `ExternalSecret` with the real secret ARN swapped in. I checked the sync **before**
installing the app:

- The `ExternalSecret` showed `SecretSynced` and `READY True` within seconds, on the first try.
- The `ClusterSecretStore` was `Valid`.
- The Secret `db-credentials` had exactly the keys `DB_USER` and `DB_PASSWORD_ENCODED`. I looked at the key
  names only, never the values.

This also answered a worry I'd left open in step 12: the controller *can* decrypt the RDS-managed secret.

## Part 3: Installing the app

```bash
scripts/gen-values.sh
TAG=$(aws ecr describe-images --repository-name vectorpipe \
  --query 'sort_by(imageDetails,&imagePushedAt)[-1].imageTags[0]' --output text)
helm template vectorpipe charts/vectorpipe -f charts/vectorpipe/values-aws.yaml --set image.tag=$TAG
helm install vectorpipe charts/vectorpipe -n vectorpipe \
  -f charts/vectorpipe/values-aws.yaml --set image.tag=$TAG --timeout 10m
```

- `gen-values.sh` worked with every layer up. It's the script I'd hardened in step 13, so it would have
  stopped loudly if an output were missing.
- The newest image in ECR was the tip of `main`. I checked that the tag was a real commit.
- Helm ran the migration Job first, as a pre-install hook. The events showed the pod created, the image
  pulled, the container started and the Job completed in about 13 seconds. Then the API (two replicas) and the
  worker came up, and all three were Ready within about 20 seconds.

## Part 4: Proving it works

**Each workload has its own identity.** I asked each pod who AWS thinks it is:

| Pod | Identity |
|---|---|
| API | `assumed-role/vectorpipe-api-pod/...` |
| Worker | `assumed-role/vectorpipe-worker-pod/...` |

That's the Pod Identity proof: no keys in the pods, and each one holds a different role. They also ran as
UID 10001, as set in the chart.

**The full pipeline.** I port-forwarded to the API's Service and uploaded a small text file:

| Step | What I saw |
|---|---|
| `POST /documents` | `202`, status `PENDING` |
| One second later | `PROCESSING` |
| Two seconds in | `COMPLETED`, 1 attempt |
| The row, read back through the API | filename, object keys, no error message |
| `GET /documents/<id>/result` | 1 chunk, 16 dimensions |
| S3 | `raw/<id>` (17 bytes) and `processed/<id>.json` (490 bytes) |
| Queue and dead-letter queue | both empty |
| Worker log | `processing started`, then `processing completed` about three seconds later, attempt 1 |

Reading the row back through the API is the database proof: that read goes from the pod, through the security
group rule the workloads layer added, to RDS. It also shows the migration worked, since the `documents` table
had to exist. I couldn't read the Job's log to prove it directly, because the chart deletes the Job once it
succeeds.

**Least privilege.** I tried things each pod should *not* be able to do:

| Action | API | Worker |
|---|---|---|
| `sqs:ReceiveMessage` | denied | allowed |
| `sqs:SendMessage` | allowed | denied |
| `secretsmanager:GetSecretValue` on the DB secret | denied | denied |
| `s3:ListBucket` | allowed | allowed |

The last row of the secret test is the important one. Only the External Secrets controller can read the
database secret. The app pods never can, even though the app is the thing that uses the password. The probe
printed only the error code and never the secret.

## My mistake during that test

My least-privilege probe tried `SendMessage` from the API pod. That is *allowed* for the API, so it was not a
test at all: it put a real message on the live queue. I had checked the queue was empty before, and the
worker's own `ReceiveMessage` probe then picked it up.

The message wasn't a valid job, so the worker rejected it as invalid three times and SQS moved it to the
dead-letter queue. By the time I noticed (the queue showed one message in flight), it was already there, so my
purge of the main queue had nothing to remove. I received the message from the DLQ with my own credentials,
checked that its body was `x` before deleting it, and watched the counters settle back to 0 (SQS counts lag
by up to a minute).

The lesson is that a "should this be denied?" probe must never be an action the role is allowed to do. To
test a denial, use only the actions that should fail. As a side effect it showed the dead-letter path working
on the cluster: an invalid job, rejected three times, then parked.

## Things that differed from what I expected

- **The Job's log isn't available.** My checklist had `kubectl logs job/vectorpipe-migrate`. The chart deletes
  the Job when it succeeds, so that's "not found" by design. The events and the working database are the
  evidence.
- **A scary-looking log line wasn't.** A search for "error" in the API log matched four lines. All were `INFO`
  startup messages from uvicorn, whose logger is *named* `uvicorn.error`.
- **The pods run with group 0.** Only the user is set in the chart (`runAsUser: 10001`), so the group is root's
  group. It's not a problem, but it isn't a fully non-root identity either.

## Still open

- The app still connects to the database as the master user. It should get a user of its own with only the
  grants it needs.
- There's no Ingress or load balancer yet. I reached the API with `kubectl port-forward`.
- The worker is a single replica with no autoscaling.
- `/health` doesn't check the database or queue, so a pod could be "ready" while it can't work.
- Deleting the `vectorpipe` namespace timed out after I had already uninstalled External Secrets. My guess is
  that the `ExternalSecret` still had a finalizer that only the controller could clear, but I didn't confirm
  it: the cluster was being torn down and had already revoked my access. Next time I'll delete the namespace
  first and uninstall the controller after.
