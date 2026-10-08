#!/usr/bin/env bash
# Writes charts/vectorpipe/values-aws.yaml from Terraform outputs (git-ignored).
# Run after every rebuild: the DB endpoint changes.
set -euo pipefail
ROOT=$(git rev-parse --show-toplevel)

out() {
  local v
  v=$(terraform -chdir="$1" output -raw "$2" 2>/dev/null) || true
  [[ -n "$v" ]] || { echo "ERROR: output '$2' is empty in $1 (is that layer applied?)" >&2; exit 1; }
  printf '%s' "$v"
}

# Plain assignments: a failure here stops the script (inside a heredoc it would not).
ECR=$(out "$ROOT/terraform" ecr_repository_url)
REGION=$(out "$ROOT/terraform" region)
BUCKET=$(out "$ROOT/terraform" s3_bucket)
QUEUE=$(out "$ROOT/terraform" sqs_queue_url)
DLQ=$(out "$ROOT/terraform" sqs_dlq_url)
DB_HOST=$(out "$ROOT/terraform/data" db_endpoint)
DB_PORT=$(out "$ROOT/terraform/data" db_port)
DB_NAME=$(out "$ROOT/terraform/data" db_name)

cat > "$ROOT/charts/vectorpipe/values-aws.yaml" <<YAML
image:
  repository: ${ECR}
aws:
  region: ${REGION}
  s3Bucket: ${BUCKET}
  sqsQueueUrl: ${QUEUE}
  sqsDlqUrl: ${DLQ}
db:
  host: ${DB_HOST}
  port: ${DB_PORT}
  name: ${DB_NAME}
YAML
echo "wrote charts/vectorpipe/values-aws.yaml"
