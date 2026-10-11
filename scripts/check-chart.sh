#!/usr/bin/env bash
# Lints and renders the chart with dummy values and checks the Ingress switch. Touches nothing on AWS.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

BASE=(--set image.repository=x --set image.tag=y --set aws.region=us-east-1
      --set aws.s3Bucket=b --set aws.sqsQueueUrl=q --set aws.sqsDlqUrl=d --set db.host=h)
ING=(--set ingress.enabled=true --set ingress.allowedCidr=1.2.3.4/32)

helm lint charts/vectorpipe "${BASE[@]}" >/dev/null
helm lint charts/vectorpipe "${BASE[@]}" "${ING[@]}" >/dev/null

off=$(helm template vp charts/vectorpipe "${BASE[@]}" | grep -c '^kind: Ingress' || true)
on=$(helm template vp charts/vectorpipe "${BASE[@]}" "${ING[@]}" | grep -c '^kind: Ingress' || true)
[[ "$off" == 0 ]] || { echo "FAIL: Ingress rendered while disabled"; exit 1; }
[[ "$on" == 1 ]] || { echo "FAIL: Ingress missing while enabled"; exit 1; }

if helm template vp charts/vectorpipe "${BASE[@]}" --set ingress.enabled=true >/dev/null 2>&1; then
  echo "FAIL: Ingress enabled without a CIDR should be refused"; exit 1
fi
echo "chart ok"
