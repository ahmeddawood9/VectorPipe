# Step 2: Docker and docker-compose

*Commit: "Add Dockerfile, docker-compose and env template".*

## What I was trying to do

Start the database, the API and a couple of workers with one command, and make every setting visible
in one place.

## What I built

- **`Dockerfile`**: `python:3.12-slim`, `libpq-dev` for Postgres, the requirements, then the code. One
  image serves as both the API and the worker. The command decides which one a container is, so the
  two can never run different versions of the code.
- **`docker-compose.yml`**: Postgres 15 with a healthcheck (published on `5433` so it doesn't clash
  with a local Postgres), the API (runs migrations on startup, then serves on `8000`), and two worker
  replicas that wait until the database is healthy.
- **`.env.example`**: every setting with its default and a short note on what it does.
- **`.dockerignore` and `.gitignore`**: keep `.env`, `storage/`, virtualenvs and tests out of the image
  and out of git.

## One thing worth noting

Locally, storage and the queue are files, so the API and workers have to share a volume
(`storage_data`). That's the coupling S3 and SQS removed later. Once those were in, containers
didn't need to share a disk at all.

## Checking it

```bash
docker compose up --build
# dashboard at http://localhost:8000/
docker compose up --scale worker=4
```

## Still open

The Dockerfile has no default `CMD`, and it runs as root. Neither matters locally, but I want to fix
both before anything runs in a cluster.
