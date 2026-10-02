# Step 2 — Containerization: Docker and docker-compose

**Date:** 2026-10-01 · **Commit:** `8e8d132` Add Dockerfile, docker-compose and env template

## Goal

Start the whole system (database, API, several workers) with one command, and make the
configuration explicit.

## What was built

| File | Purpose |
|---|---|
| `Dockerfile` | `python:3.12-slim`, installs `libpq-dev` + requirements, copies the app. One image for both API and worker. |
| `docker-compose.yml` | `postgres:15-alpine` (healthchecked, host port `5433`), `api` (runs `alembic upgrade head`, then the API on `8000`), `worker` (2 replicas) |
| `.env.example` | Every setting with its default and meaning |
| `.dockerignore` | Keeps `.git`, venvs, `storage/`, `tests/` and `.env` out of the image |
| `.gitignore` | Secrets (`.env`), runtime data (`storage/`), Python and Terraform artefacts |

## Key design decisions

* **One image, two commands.** The service's `command` decides whether a container is an API or a
  worker, so both always run the same code version.
* **Migrations run when the API starts**, so a fresh database is usable immediately.
* **Workers wait for a healthy database** (`depends_on: condition: service_healthy`).
* **Shared volume `storage_data`.** The local storage and the SQLite queue are files, so API and
  workers must share a volume. This is the coupling that S3 and SQS remove in Step 4.

## How to verify

```bash
docker compose up --build
open http://localhost:8000/            # dashboard
docker compose up --scale worker=4     # more workers
```

## Left open

* The Dockerfile has no default `CMD` and runs as root; worth fixing before the image goes to ECR.
* The image is not published anywhere yet (the ECR repository arrives in
  [Step 6](06-foundation-infrastructure.md)).
