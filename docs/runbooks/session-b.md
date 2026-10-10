# Runbook: Session B (ALB ingress and Argo CD)

How to build the environment, run the app behind an ALB with Argo CD deploying it, and tear it all down.
Written from [step 14](../steps/14-first-install.md) (the first install) and [step 15](../steps/15-session-b.md)
(what changed for this session). There was no earlier runbook in the repo, so the first half repeats what
step 14 did.

**Rules for the session**

- Nothing is applied or destroyed without an explicit go-ahead.
- Never print a secret value. Look at key names only.
- The Argo CD UI is reached with `kubectl port-forward` and nothing else. It is never exposed.
- The foundation layer stays up between sessions; everything else bills by the hour.

## Pinned versions

| Component | Chart | App | Where |
|---|---|---|---|
| AWS Load Balancer Controller | `eks/aws-load-balancer-controller` **3.6.0** | v3.6.0 | `kube-system` |
| Argo CD | `argo/argo-cd` **10.10.2** | v3.5.4 | `argocd` |
| External Secrets | `external-secrets/external-secrets` **2.12.0** | v2.12.0 | `external-secrets` |
| Kubernetes | | 1.37 | Argo CD's docs only list 1.33 to 1.36 as tested |

Always pass `--version`. Never install "latest". The LBC's IAM policy is pinned to the same tag
(`terraform/workloads/lbc-iam-policy.json`, from the controller's repository at `v3.6.0`), so bump the two
together.

## 0. Before spending anything

```bash
curl -s https://checkip.amazonaws.com      # the EKS API and the ALB only accept this IP
aws sts get-caller-identity                # you, as the admin user
git status                                 # clean
```

## 1. Build the AWS layers (the clock starts)

Order: network, then data and eks (they only need the network, so two terminals), then workloads.

```bash
cd terraform/network && terraform init && terraform apply        # 19 to add. Read it first.

cd terraform/data && terraform init && terraform apply           # 10 to add

cd terraform/eks && terraform init
IP=$(curl -s https://checkip.amazonaws.com)
terraform plan -var kubernetes_version=1.37 -var my_ip_cidr=$IP/32 -out eks.tfplan    # 15 to add
terraform apply eks.tfplan

cd terraform/workloads && terraform init && terraform apply      # 14 to add on a fresh build
```

The workloads layer is now **14** resources: the 10 from step 12 (the api, worker and external-secrets roles,
their attachments and associations, and the database security group rule) plus 4 for the load balancer
controller (a role, a policy, an attachment and a Pod Identity association). All 14 must exist **before** the
pods start, because credentials are injected when a pod is created.

If `data` fails with `InsufficientDBInstanceCapacity`, show the raw error, wait and retry. Never loosen a
security setting to get past it.

## 2. Check the cluster

```bash
aws eks update-kubeconfig --name vectorpipe --region us-east-1
kubectl get nodes          # 2 nodes Ready
kubectl get pods -A        # coredns, kube-proxy, aws-node, eks-pod-identity-agent Running
```

If `kubectl` times out, your IP changed: re-run the eks apply with the new `my_ip_cidr`.

## 3. External Secrets, then the bootstrap manifests

```bash
helm repo add external-secrets https://charts.external-secrets.io && helm repo update
helm install external-secrets external-secrets/external-secrets --version 2.12.0 \
  -n external-secrets --create-namespace --set installCRDs=true
kubectl -n external-secrets rollout status deploy/external-secrets
kubectl -n external-secrets rollout status deploy/external-secrets-webhook
kubectl get crd externalsecrets.external-secrets.io -o jsonpath='{.spec.versions[*].name}'; echo   # includes v1

kubectl apply -f k8s/bootstrap/00-namespace.yaml -f k8s/bootstrap/10-serviceaccounts.yaml
kubectl apply -f k8s/bootstrap/20-secretstore.yaml
sed "s|REPLACE_DB_SECRET_ARN|$(terraform -chdir=terraform/data output -raw db_secret_arn)|g" \
  k8s/bootstrap/30-externalsecret.yaml | kubectl apply -f -

kubectl -n vectorpipe get externalsecret      # READY True, SecretSynced
kubectl -n vectorpipe get secret db-credentials -o jsonpath='{.data}' | jq 'keys'   # keys only
```

Argo CD runs the app's migration before anything else, and the migration needs `db-credentials`. So this step
must finish **before** the Application is applied. The Application also has `CreateNamespace=false`, so the
`vectorpipe` namespace has to exist already.

## 4. The AWS Load Balancer Controller

Set `clusterName`, `region` and `vpcId` explicitly, and give the service account the exact name the Pod
Identity association expects:

```bash
helm repo add eks https://aws.github.io/eks-charts && helm repo update
helm install aws-load-balancer-controller eks/aws-load-balancer-controller --version 3.6.0 \
  -n kube-system \
  --set clusterName=vectorpipe \
  --set region=us-east-1 \
  --set vpcId=$(terraform -chdir=terraform/network output -raw vpc_id) \
  --set serviceAccount.create=true \
  --set serviceAccount.name=aws-load-balancer-controller

kubectl -n kube-system rollout status deploy/aws-load-balancer-controller
```

Do not go on until that deployment is Ready: the Ingress is created minutes later and nothing would act on it.
A pod started before its Pod Identity association existed has no credentials; restart it if the logs say so.

## 5. Argo CD

```bash
helm repo add argo https://argoproj.github.io/argo-helm && helm repo update
helm install argocd argo/argo-cd --version 10.10.2 -n argocd --create-namespace \
  --set dex.enabled=false \
  --set notifications.enabled=false \
  --set applicationSet.replicas=0

kubectl -n argocd rollout status deploy/argocd-server
kubectl -n argocd rollout status deploy/argocd-repo-server
kubectl -n argocd rollout status deploy/argocd-redis
kubectl -n argocd rollout status statefulset/argocd-application-controller
```

This chart version has **no** `applicationSet.enabled` switch (setting it does nothing), so the ApplicationSet
controller is turned off by scaling it to zero. Dex and notifications do have `enabled` switches. The chart
creates only ClusterIP Services.

To open the UI, and only this way:

```bash
kubectl port-forward svc/argocd-server -n argocd 8080:443
```

## 6. Deploy the app through Argo CD

```bash
TAG=$(aws ecr describe-images --repository-name vectorpipe \
  --query 'sort_by(imageDetails,&imagePushedAt)[-1].imageTags[0]' --output text)
IP=$(curl -s https://checkip.amazonaws.com)
scripts/gen-argocd-app.sh $TAG $IP/32        # writes k8s/argocd/application.yaml (git-ignored)
kubectl apply -f k8s/argocd/application.yaml

kubectl -n argocd get application vectorpipe -w
kubectl -n vectorpipe get pods -w
kubectl -n vectorpipe get ingress
```

Watch for, in order: the migration Job running as a **PreSync** hook (Argo CD maps Helm's `pre-install` and
`pre-upgrade` hooks to PreSync) and completing, then the API ×2 and the worker, then the Ingress getting an
`ADDRESS` after a couple of minutes (that is the ALB being created).

```bash
ALB=$(kubectl -n vectorpipe get ingress -o jsonpath='{.items[0].status.loadBalancer.ingress[0].hostname}')
curl -s http://$ALB/health          # {"status":"ok"}, from your own IP only
```

From any other IP the ALB's security group drops the connection. That is deliberate: the ALB is public, so the
allowed CIDR is the only thing protecting it.

The pipeline check from step 14 still applies: upload a document through the ALB, poll its status until
`COMPLETED`, then look for `raw/<id>` and `processed/<id>.json` in the bucket and an empty queue.

If an Argo CD component misbehaves on Kubernetes 1.37, capture the logs
(`kubectl -n argocd logs <pod>`, `describe pod`) and report them before changing anything.

## 7. Teardown: the ALB must go first

**This order is critical.** The ALB, its target groups and its security groups are created by the controller,
not by Terraform. If the cluster or the network is destroyed while they exist, the ALB is orphaned: its
network interfaces and security groups block deleting the VPC, and the ALB keeps billing by the hour.

1. **Delete the Application and let Argo CD remove everything it created.** Its finalizer deletes the Ingress,
   and the controller then deletes the ALB.

   ```bash
   kubectl delete application vectorpipe -n argocd
   ```

   Wait for it to finish. It can take a few minutes.

2. **Verify the ALB is gone. Do not continue until both are empty.**

   ```bash
   aws elbv2 describe-load-balancers --query 'LoadBalancers[].LoadBalancerName'
   aws elbv2 describe-target-groups --query 'TargetGroups[].TargetGroupName'
   aws ec2 describe-security-groups --filters Name=tag-key,Values=elbv2.k8s.aws/cluster \
     --query 'SecurityGroups[].GroupId'
   ```

   No `vectorpipe` or `k8s-...` load balancer, no target groups, and no security group tagged
   `elbv2.k8s.aws/cluster`.

3. Remove the cluster add-ons, namespaces first so nothing is left waiting on a controller that no longer
   exists:

   ```bash
   kubectl delete namespace vectorpipe        # before External Secrets goes (step 14: it can get stuck otherwise)
   helm uninstall argocd -n argocd
   helm uninstall aws-load-balancer-controller -n kube-system
   helm uninstall external-secrets -n external-secrets
   ```

4. **Only when told:** destroy the layers in reverse, one at a time.

   ```
   workloads  →  eks  →  data  →  network
   ```

   `eks` needs `-var kubernetes_version=1.37 -var my_ip_cidr=<any valid /32>`.

5. Run the leak checks from [step 14](../steps/14-first-install.md): EKS clusters, RDS, NAT gateways, EC2
   instances, Elastic IPs, volumes, **load balancers and target groups**, VPCs, network interfaces and tagged
   security groups should all be empty. Delete the generated `charts/vectorpipe/values-aws.yaml` and
   `k8s/argocd/application.yaml`, which hold old endpoints.

## What bills while this is up

The EKS control plane, the two nodes, the NAT gateway with its Elastic IP, RDS, and now the ALB. The ALB is
the one that gets forgotten, which is why step 7 comes first.
