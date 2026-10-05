# Step 11: A Kubernetes cluster

*Commit: "Add the EKS layer: cluster, managed nodes, add-ons and admin access".*

## What I was trying to do

Get a real Kubernetes cluster up that I can run the app on later. This step is only the cluster:
the control plane, worker nodes, the core add-ons and a way for me to log in. Putting the app on it
comes next.

It's another session layer, so it needs the network first and gets destroyed before it:

```
build:    network → eks
destroy:  eks → network
```

## What I built (`terraform/eks/`, state at `eks/terraform.tfstate`)

The layer reads `private_subnet_ids` from the network layer through `terraform_remote_state`, like the
data layer does. It creates 15 resources.

**The control plane.** An EKS cluster called `vectorpipe` in the two private subnets.
- The private endpoint is **on**, so the nodes reach the API from inside the VPC.
- The public endpoint is **on too**, but limited to my own IP as a `/32`. If I turned the private
  endpoint off, the nodes (which come out through the NAT) would be locked out and never join.
- Authentication is in API mode, so I use EKS access entries instead of the old `aws-auth` ConfigMap.

**Admin access.** The cluster doesn't make its creator an admin automatically. I grant it explicitly: an
access entry for my IAM user, with the `AmazonEKSClusterAdminPolicy` at cluster scope. My ARN lives in
`terraform.tfvars` (git-ignored) and not in the code, the same way `dev_user_arn` does, with a
`terraform.tfvars.example` showing the shape.

**Worker nodes.** A managed node group called `main`: two nodes to start (minimum 1, maximum 3),
On-Demand, Amazon Linux 2023 on x86. I stayed off Graviton because my CI builds amd64 images.

**Add-ons.** `vpc-cni` and `kube-proxy` first, so nodes can become Ready, then `coredns` and the pod
identity agent after the nodes exist, because they run as pods.

**IAM.** A cluster role, and a node role with four policies: worker node, CNI, ECR read-only (to pull
images) and SSM (a shell on a node without SSH). Putting the CNI's permissions on the node role is a
shortcut. In production the CNI would get its own role through Pod Identity.

## Picking the Kubernetes version

I asked AWS instead of guessing:

```bash
aws eks describe-cluster-versions --query 'clusterVersions[].[clusterVersion,status]' --output table
```

The newest version in standard support was **1.37**, so that's what I used. EKS's own default was 1.36,
which is the fallback if a tool I install later doesn't support 1.37 yet. The rule I set myself: check
each tool's published compatibility list before installing it (the AWS Load Balancer Controller, KEDA,
ArgoCD, Karpenter), and if one doesn't support 1.37, rebuild on 1.36 instead of fighting it.

## How it actually went

**The network was gone.** My first plan failed with "object with no attributes", because the cluster layer
reads the network's outputs and I had destroyed the network at the end of the last session. The fix was
simply to apply the network layer again first (19 resources) and re-plan.

**The nodes never started.** The control plane came up fine, but the node group sat at `CREATING` with no
instances and no health issues. About 33 minutes later it gave up as `CREATE_FAILED`. Looking at why:

- The node group's `health.issues` was empty while it was creating, so that wasn't where the answer was.
- The autoscaling group had been created, with 0 instances, so the problem was launching them.
- Its scaling activities had the real error, repeated every couple of minutes:

```
InvalidParameterCombination - The specified instance type is not eligible for Free Tier.
```

My account is on the AWS Free plan, which only lets you launch free-tier-eligible instance types, and
`t3.medium` isn't one of them. I confirmed it with `aws freetier get-account-plan-state` (plan `FREE`) and
by listing the eligible types:

```bash
aws ec2 describe-instance-types --filters Name=free-tier-eligible,Values=true
```

Of the x86 options, `m7i-flex.large` (2 vCPU, 8 GB) and `c7i-flex.large` (2 vCPU, 4 GB) were the sensible
ones. `t3.micro` only fits 4 pods per node, and the Graviton `t4g` types don't run my amd64 images. So
the node group now uses `["m7i-flex.large", "c7i-flex.large"]`, with a comment in `variables.tf` saying
free-plan accounts can only launch specific instance types and to revisit it if the account moves to a
paid plan.

**Why the rebuild was cheap.** The failed node group was recorded in Terraform's state as tainted, so the
next plan showed exactly one replacement (the node group that never had a node in it) plus the two
add-ons that hadn't been created yet: 3 to add, 0 to change, 1 to destroy. The cluster itself was
untouched. The second node group came up in under two minutes.

## What it looked like when it worked

```
$ kubectl get nodes
NAME                         STATUS   ROLES    AGE   VERSION
ip-10-0-30-31.ec2.internal   Ready    <none>   93s   v1.37.0-eks-3b4a6ca
ip-10-0-33-46.ec2.internal   Ready    <none>   95s   v1.37.0-eks-3b4a6ca
```

Both nodes were `m7i-flex.large`, one in each AZ with no public IP. Every system pod (`aws-node`,
`coredns`, `kube-proxy`, `eks-pod-identity-agent`) was `Running`, all four add-ons were `ACTIVE`, the node
group had no health issues, and a plan afterwards showed no changes.

## Using it

```bash
# 1. network first
cd terraform/network && terraform apply

# 2. then the cluster
cd ../eks
cp terraform.tfvars.example terraform.tfvars        # set admin_principal_arn
curl -s https://checkip.amazonaws.com               # your current public IP
terraform apply -var kubernetes_version=1.37 -var my_ip_cidr=<your-ip>/32

# 3. log in
aws eks update-kubeconfig --name vectorpipe --region us-east-1
kubectl get nodes
kubectl get pods -A
```

The API is reachable only from the IP I give it. If my IP changes, `kubectl` times out until I apply
again with the new one. The cluster takes about 12 minutes to come up and the nodes about two more.

## Tearing it down

```bash
cd terraform/eks     && terraform destroy -var kubernetes_version=1.37 -var my_ip_cidr=<your-ip>/32
cd ../network        && terraform destroy
```

The cluster layer took about six minutes to destroy and the network under a minute. Then I checked
nothing was left billing, and all of these came back empty: EKS clusters, RDS instances, NAT gateways,
EC2 instances, Elastic IPs, EBS volumes, load balancers and loose network interfaces. The last two
matter, because a stray volume or network interface keeps quietly costing money.

## Still open

- Nothing from the app is running on the cluster yet.
- The CNI uses the node role's permissions. That should become its own role through Pod Identity.
- The control plane, nodes and NAT bill by the hour, so the cluster only exists while I'm using it.
