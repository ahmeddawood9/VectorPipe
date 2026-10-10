# Step 15: Getting ready for an ALB and Argo CD

*Commits: "Add the AWS Load Balancer Controller's IAM role and policy", "Add an Ingress to the Helm chart",
"Add the script that generates the Argo CD Application", "Document step 15 and the session B runbook",
"Make the Argo script fail clean and validate the CIDR", "Use api.port for the Ingress backend".*

## What I was trying to do

Two things were missing from the cluster. I could only reach the API with `kubectl port-forward`, so there
was no real front door, and I installed the chart by hand with `helm install`, so nothing kept the cluster
matching what's in git. This step prepares both: an Application Load Balancer in front of the API, created by
the AWS Load Balancer Controller, and Argo CD deploying the chart from git.

This was code only. I didn't apply or destroy anything, and I made no AWS calls beyond reading versions and
reading Terraform state. The environment itself is built in the next session, following the runbook I wrote
here ([`docs/runbooks/session-b.md`](../runbooks/session-b.md)).

Four files arrived from outside the repo (the controller's Terraform, the Ingress template, a values snippet
and the Argo script). As before, I read every one and checked each against the real chart before trusting it.

## Pinning the versions

I never install "latest". I added the two chart repositories and took the newest stable release of each. The
newest was also the newest even with pre-releases included, so nothing was skipped:

| Chart | Chart version | App version |
|---|---|---|
| `eks/aws-load-balancer-controller` | **3.6.0** | v3.6.0 |
| `argo/argo-cd` | **10.10.2** | v3.5.4 |

The load balancer controller is on a **v3** major line now, so I was careful about which IAM policy goes with
it. The policy has to come from the controller's own repository at the **same tag** as the app version, or it
can be missing permissions the newer controller needs. I downloaded
`docs/install/iam_policy.json` at `v3.6.0` into `terraform/workloads/lbc-iam-policy.json`. I checked that the
tag exists and is a real release, that the file is valid JSON (16 statements, 80 actions), that it contains no
account IDs, and that it is **5,196 characters** once compacted. That last number matters because an IAM
managed policy is limited to 6,144 characters, so it fits with room to spare.

## The Terraform: a role for the controller

`lb-controller.tf` adds four things to the workloads layer: a role the controller can assume, the policy above,
the attachment, and a Pod Identity association binding the role to the `aws-load-balancer-controller` service
account in `kube-system`. It follows the same pattern as the api and worker roles.

Two small notes on the file I was given:

- Its remote-state reference (`eks`) already matched the name in `providers.tf`, so nothing needed aligning.
- A comment in it described who would download the policy file, which is process chatter that doesn't belong in
  committed code. I reworded it to say the policy comes from the controller's repository at the same tag.

`terraform validate` passes. The plan is where it got awkward, and I want to be exact about it:

- **A full `terraform plan` can't complete right now.** The workloads layer reads the `eks` and `data` states,
  and both are destroyed between sessions, so Terraform stops at "object with no attributes". This is the same
  limit as in step 12.
- **A targeted plan works for the three resources that don't need those layers**: the role, the policy and the
  attachment, which gives **3 to add, 0 to change, 0 to destroy**. The fourth, the association, needs the
  cluster name from `eks`, so it can only be planned once the cluster exists.
- On a fresh build the whole layer is **14 resources**, not the 4 I had expected: the 10 from before plus these
  4. The "4 to add" would only be true on top of an already-built layer.

## The chart: an Ingress

I added `templates/ingress.yaml` and an `ingress:` block to `values.yaml`. It is off by default. When it's on,
it asks for an internet-facing ALB, targeting pod IPs, with the health check on the API's `/health` and an
`inbound-cidrs` annotation so that only my own IP can reach it. The ALB is public by design, so that
annotation is the only thing protecting it. I checked the chart in four ways:

| Case | Result |
|---|---|
| `helm lint`, ingress on and off | passes both ways |
| `helm template` with `ingress.enabled=true` and a CIDR | renders the Ingress with that CIDR |
| `helm template` with `ingress.enabled=false` | renders **no** Ingress, and everything else is unchanged |
| enabled with no CIDR | refuses, with `ingress.allowedCidr is required (your.ip/32)` |

Two things I noticed in the template as it arrived: the backend port was hardcoded as `8000` instead of
reading `api.port`, and it carries none of the labels the other templates have. I reported both, and then
fixed the port after approval. It now reads `api.port`, the same value the Service uses for its own port
(which is what the Ingress backend has to equal). I proved it flows end to end by rendering with the default
and with `api.port=9000`: the Ingress backend, the Service port, the container port and `API_PORT` were equal in
both. Lint passes with the ingress on and off, and with it off no Ingress is rendered. The missing labels are
still open.

**Will the ALB find its subnets?** This worried me, because the network layer only tags the public subnets with
`kubernetes.io/role/elb`. The controller's own docs for this version say the cluster tag isn't required (only
for controllers older than 2.1.1), that it picks subnets by the role tag, and that it needs two subnets in
different AZs with at least 8 free IPs each. Ours have the tag, are in two AZs, and are `/24`s, so no change to
the network is needed.

## The Argo CD Application script

`gen-argocd-app.sh` writes an Application that points Argo CD at `charts/vectorpipe` on `main` and passes the
AWS-specific values straight in, so it holds the account's endpoints and is git-ignored. I compared every key
it sets against `values.yaml` by generating a file with a fake `terraform`, and they all match exactly. Helm
renders the chart cleanly from those generated values, including the Ingress locked to the CIDR. So the
script needed no fix.

Two things about how it behaved when I first tested it with a destroyed data layer:

- It failed loudly (the error is printed and the exit code is 1), which is right.
- But it left a stale file behind: it deleted the old `application.yaml` only *after* fetching the outputs, so a
  failed run kept the previous one. Applying that would deploy an old database endpoint and image tag. This is
  the same problem I fixed in `gen-values.sh` in step 13.

It also didn't validate the CIDR: `not-a-cidr` was written as is. The repo is public, so Argo CD can clone it
without credentials.

I fixed both after approval. The script now deletes the old file as its very first step (before anything can
fail, including a bad argument) and checks the second argument against `^([0-9]{1,3}\.){3}[0-9]{1,3}/32$`,
stopping with a message that shows what it got and what it expected. I tested ten failure cases with a stale
file planted each time, using a fake `terraform` so nothing touched AWS: no arguments, a missing CIDR, `not-a-cidr`,
`1.2.3.4` (no `/32`), `1.2.3.4/24`, `1.2.3/32`, `1.2.3.4/32x`, an IPv6 address, an empty string, and a valid
CIDR with the data layer destroyed. Every one exited 1 and left no file behind. A valid address writes a file
that parses as YAML with the ingress enabled and locked to that address. One limit remains: the regex you
asked for checks the shape and not the values, so `999.999.999.999/32` is accepted.

## A trap in the Argo CD values

The plan called for turning off Dex, notifications and the ApplicationSet controller. I checked the exact
keys against chart 10.10.2 before writing them into the runbook, and found that **`applicationSet.enabled`
doesn't exist** in this version. Setting it would have done nothing, and the controller would have been
installed anyway, with no error to tell me. Dex and notifications do have `enabled` switches. For the
ApplicationSet controller the only lever is `applicationSet.replicas=0`, and I rendered it to confirm it
produces a Deployment with zero replicas. The chart creates only ClusterIP Services, so nothing is exposed,
and the three Argo CRDs ship with it.

I did the same for the load balancer controller: `clusterName`, `region`, `vpcId` and `serviceAccount.*` all
exist in chart 3.6.0. Rendered with the runbook's flags, it produces a Deployment and a service account both
named `aws-load-balancer-controller`, which is the name the Pod Identity association expects, and it passes
the cluster name, region and VPC ID as explicit arguments.

## Kubernetes 1.37

Argo CD's docs list 1.33 to 1.36 as tested, and I'm on 1.37. What I could confirm from published sources:

- Argo CD's chart only requires Kubernetes `>=1.25`, so Helm won't refuse it.
- The load balancer controller's docs say "v2.5.0+ requires Kubernetes 1.22+", an open-ended minimum, and its
  chart sets no limit.

So 1.37 isn't excluded, but it is untested for Argo CD. The runbook says: if anything in Argo CD misbehaves,
capture the logs and report them before changing anything.

## The teardown order is the dangerous part

The ALB, its target groups and its security groups are created by the controller, not by Terraform. If I
destroyed the cluster or the network with them still there, the ALB would be orphaned: its network
interfaces and security groups would block deleting the VPC, and it would keep billing by the hour. So the
runbook deletes the Argo CD Application first (its finalizer removes the Ingress, and the controller then
removes the ALB), then **checks** that no load balancer, target group or security group tagged
`elbv2.k8s.aws/cluster` is left, and only then goes on. It also deletes the `vectorpipe` namespace before
uninstalling External Secrets, from the stuck-namespace lesson in step 14.

## Still open

- Nothing from this step has run on AWS. I haven't applied the workloads layer with the new role, installed
  either chart, or created an ALB.
- Argo CD on Kubernetes 1.37 is untested, and so is the load balancer controller on it beyond its docs.
- The CIDR check validates the shape and not the values, so an address like `999.999.999.999/32` is accepted.
- The Ingress still has none of the labels the other templates carry.
- The ALB serves plain HTTP on port 80. There is no certificate or domain yet.
- The app still connects to the database as the master user.
