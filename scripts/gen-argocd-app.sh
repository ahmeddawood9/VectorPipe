#!/usr/bin/env bash
# Generates k8s/argocd/application.yaml (git-ignored: contains account-specific values).
# Usage: scripts/gen-argocd-app.sh <image-tag> <your.ip/32>
set -euo pipefail
TAG="${1:?image tag (git sha)}"; CIDR="${2:?your ip/32}"
cd "$(git rev-parse --show-toplevel)"
repo=$(terraform -chdir=terraform output -raw ecr_repository_url)
region=$(terraform -chdir=terraform output -raw region)
bucket=$(terraform -chdir=terraform output -raw s3_bucket)
q=$(terraform -chdir=terraform output -raw sqs_queue_url)
dlq=$(terraform -chdir=terraform output -raw sqs_dlq_url)
dbh=$(terraform -chdir=terraform/data output -raw db_endpoint)
dbp=$(terraform -chdir=terraform/data output -raw db_port)
dbn=$(terraform -chdir=terraform/data output -raw db_name)
mkdir -p k8s/argocd; rm -f k8s/argocd/application.yaml
cat > k8s/argocd/application.yaml <<YAML
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: vectorpipe
  namespace: argocd
  finalizers: [resources-finalizer.argocd.argoproj.io]   # deleting the app deletes its resources (and the ALB)
spec:
  project: default
  source:
    repoURL: https://github.com/ahmeddawood9/VectorPipe.git
    targetRevision: main
    path: charts/vectorpipe
    helm:
      valuesObject:
        image: {repository: "${repo}", tag: "${TAG}"}
        aws: {region: "${region}", s3Bucket: "${bucket}", sqsQueueUrl: "${q}", sqsDlqUrl: "${dlq}"}
        db: {host: "${dbh}", port: "${dbp}", name: "${dbn}"}
        ingress: {enabled: true, allowedCidr: "${CIDR}"}
  destination: {server: https://kubernetes.default.svc, namespace: vectorpipe}
  syncPolicy:
    automated: {prune: true, selfHeal: true}
    syncOptions: [CreateNamespace=false]
YAML
echo "wrote k8s/argocd/application.yaml (git-ignored). Check keys match charts/vectorpipe/values.yaml."
