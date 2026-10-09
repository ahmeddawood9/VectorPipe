# Build log

This folder is my build log for VectorPipe. I wanted a record of how the project actually came
together: what I built at each stage, why I made the calls I made, what broke along the way and how
I fixed it. The README at the root explains how to use the project. This explains how it got here.

Each step is one page. The commits on `main` line up with the steps, so you can read a page and then
look at the commits it mentions to see the actual change.

| # | Step |
|---|---|
| 1 | [The app: API, worker, local storage and queue](steps/01-application.md) |
| 2 | [Docker and docker-compose](steps/02-containerization.md) |
| 3 | [Somewhere safe for Terraform state](steps/03-terraform-state-bootstrap.md) |
| 4 | [Teaching the app to talk to S3 and SQS](steps/04-aws-backends.md) |
| 5 | [Running locally as a real IAM role](steps/05-assume-role-profile.md) |
| 6 | [The foundation: S3, SQS, ECR and IAM](steps/06-foundation-infrastructure.md) |
| 7 | [Proving it on real AWS](steps/07-live-verification.md) |
| 8 | [CI and pushing images without AWS keys](steps/08-ci-oidc.md) |
| 9 | [The network](steps/09-network.md) |
| 10 | [RDS, and a client nobody can SSH into](steps/10-data-layer.md) |
| 11 | [A Kubernetes cluster](steps/11-eks-cluster.md) |
| 12 | [Wiring the app to AWS from inside the cluster](steps/12-workloads-wiring.md) |
| 13 | [Packaging the app for Kubernetes](steps/13-helm-chart.md) |
| 14 | [The first install on the cluster](steps/14-first-install.md) |

## Where it stands

```
                      ┌──────────────── AWS us-east-1 ────────────────┐
Client ─▶ API ──put──▶│ S3   vectorpipe-documents-<account>            │
           │          │        raw/<id>   processed/<id>.json          │
           └─enqueue─▶│ SQS  vectorpipe-jobs ──3 failures──▶ DLQ        │
Worker ◀──receive─────│ ECR  vectorpipe:<git-sha>   ◀── GitHub Actions  │
  └─ status ─▶ Postgres (local, or RDS while the data layer is up)      │
                      └────────────────────────────────────────────────┘
```

The foundation (S3, SQS, ECR, IAM) stays up all the time because it costs nothing while idle. The
network, the database and the Kubernetes cluster are session layers: I build them when I need them and
destroy them when I'm done, because the NAT gateway, RDS and EKS bill by the hour. They go up in the
order network, data, eks, workloads, and come down in reverse.

The app has now run on the cluster end to end ([step 14](steps/14-first-install.md)). What's next: a database
user of its own instead of the master user, and an Ingress so I don't need `kubectl port-forward`.

How I keep this log is in [DOCS_INSTRUCTIONS.md](DOCS_INSTRUCTIONS.md).
