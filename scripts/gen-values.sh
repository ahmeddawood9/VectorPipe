#!/usr/bin/env bash
# Writes charts/vectorpipe/values-aws.yaml from Terraform outputs. The file is git-ignored
# (it holds your account ID and endpoints). Run it after every rebuild: the DB endpoint changes.
set -euo pipefail
ROOT=$(git rev-parse --show-toplevel)
out() { terraform -chdir="$1" output -raw "$2"; }

cat > "$ROOT/charts/vectorpipe/values-aws.yaml" <<YAML
image:
  repository: $(out "$ROOT/terraform" ecr_repository_url)
aws:
  region: $(out "$ROOT/terraform" region)
  s3Bucket: $(out "$ROOT/terraform" s3_bucket)
  sqsQueueUrl: $(out "$ROOT/terraform" sqs_queue_url)
  sqsDlqUrl: $(out "$ROOT/terraform" sqs_dlq_url)
db:
  host: $(out "$ROOT/terraform/data" db_endpoint)
  port: $(out "$ROOT/terraform/data" db_port)
  name: $(out "$ROOT/terraform/data" db_name)
YAML
echo "wrote charts/vectorpipe/values-aws.yaml"
