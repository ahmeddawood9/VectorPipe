# Step 9: The network

*October 4. Commit: "Add the network layer: VPC, two AZs, one NAT, S3 endpoint".*

## What I was trying to do

Build a private network for the database now, and for EKS later. I made it its own layer, so I can
destroy it at the end of every session and stop paying for the NAT, without touching the foundation.

## What I built (`terraform/network/`, state at `network/terraform.tfstate`)

- **VPC** `10.0.0.0/16`, with DNS support and hostnames on. EKS and RDS both need those.
- **Two AZs**, which is the minimum for an RDS subnet group, EKS, and a public load balancer. I take
  the first two the account returns, which were `us-east-1a` and `us-east-1b`. I keep away from
  `us-east-1e`, because EKS control planes don't support it.
- **Public subnets**, a `/24` in each AZ, for the load balancer and the NAT. They route to an internet
  gateway.
- **Private subnets**, a `/20` in each AZ. They're big on purpose, because EKS gives pods IP addresses
  from the node's subnet.
- **One NAT gateway**, with its Elastic IP, in the first public subnet.
- **One private route table per AZ**, each sending `0.0.0.0/0` to the NAT.
- **An S3 gateway endpoint** on the private route tables. It's free, and it keeps S3 traffic off the
  NAT.
- **Subnet tags** for the AWS Load Balancer Controller: `kubernetes.io/role/elb` on public subnets,
  `internal-elb` on private ones. That way I won't have to come back and edit this layer when I add EKS.

## The one-NAT decision

One NAT gateway means that if its AZ goes down, both private subnets lose internet access. I'm fine
with that here. I destroy the network after every session, nothing depends on it staying up, and my
budget is small.

In production I'd run one NAT per AZ. That's a single variable, `nat_per_az = true`. The private route
tables are already one per AZ, so nothing else has to change.

## Checking it

```bash
cd terraform/network && terraform plan    # 19 to add on a fresh build
aws ec2 describe-nat-gateways --filter Name=state,Values=available
aws ec2 describe-route-tables --filters Name=vpc-id,Values=<vpc_id>
```

Both private route tables should have `0.0.0.0/0` going to the NAT, plus a route to the S3 endpoint.
On the first build I got 19 added, the NAT was available, and both tables had both routes.

## Still open

The NAT and its Elastic IP bill every hour. At the end of a session I destroy this layer, after the
data layer.
