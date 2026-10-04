# Step 9 — Network layer: VPC, two AZs, one NAT

**Date:** 2026-10-04 · **Commit:** `81e1518` Add the network layer: VPC, two AZs, one NAT, S3 endpoint

## Goal

A private network for the database and, later, EKS. It's a separate layer so it can be destroyed
after every session to stop the NAT charges, without touching the foundation.

## What was built (`terraform/network/`, state `network/terraform.tfstate`)

| Resource | Settings |
|---|---|
| VPC | `10.0.0.0/16`, DNS support and hostnames on (EKS and RDS need them) |
| Public subnets | one `/24` per AZ, for the load balancer and the NAT; route to the internet gateway |
| Private subnets | one `/20` per AZ; large because EKS pods take IPs from the node subnet |
| NAT gateway | **one**, with its Elastic IP, in the first public subnet |
| Private route tables | one per AZ, `0.0.0.0/0` → NAT |
| S3 gateway endpoint | on the private route tables: S3 traffic skips the NAT, at no cost |
| Subnet tags | `kubernetes.io/role/elb` (public), `internal-elb` (private) for the AWS Load Balancer Controller |

AZs: the first two the account returns, **us-east-1a and us-east-1b** (us-east-1e is avoided: EKS
control planes don't support it).

## Key design decision: one NAT

One AZ outage would cut the private subnets off from the internet. That is acceptable here: the network
is destroyed after every session, nothing depends on it staying up, and the budget is small. In
production set `nat_per_az = true`; the private route tables are already one per AZ, so nothing else
changes.

## How to verify

```bash
cd terraform/network && terraform plan     # 19 to add on a fresh build
aws ec2 describe-nat-gateways --filter Name=state,Values=available
aws ec2 describe-route-tables --filters Name=vpc-id,Values=<vpc_id>
# both private tables: 0.0.0.0/0 → nat-…, plus the S3 endpoint route (vpce-…)
```

Verified on the first build: 19 added; NAT available; both private tables had the NAT and S3 routes.

## Left open

The NAT and its Elastic IP bill by the hour. Destroy this layer after the data layer at the end of
every session.
