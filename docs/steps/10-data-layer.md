# Step 10 — Data layer: RDS PostgreSQL and an SSM-only test client

**Date:** 2026-10-04 · **Commit:** the commit adding `terraform/data/` and this page

## Goal

A managed PostgreSQL that nothing on the internet can reach, plus a temporary client inside the VPC to
test it, reachable without SSH keys or open ports.

## Layer order

```
create:   foundation → network → data
destroy:  data → network        (foundation stays)
```

The data layer has its own state (`data/terraform.tfstate`) and reads the network's `vpc_id` and
`private_subnet_ids` through a `terraform_remote_state` data source, instead of hardcoding IDs.

## What was built (`terraform/data/`)

| Resource | Settings |
|---|---|
| RDS instance `vectorpipe` | PostgreSQL 15 (matches compose), `db.t3.micro`, 20 GB gp3, encrypted, **single-AZ**, **not publicly accessible**, `skip_final_snapshot`, no deletion protection (destroyed every session) |
| Master password | `manage_master_user_password`: RDS generates it and stores it in Secrets Manager. It is never in code, `.env` or Terraform state |
| DB subnet group | both private subnets (two AZs, as RDS requires) |
| DB security group | only ingress: TCP 5432 **from the client security group, by ID, never a CIDR** |
| Client EC2 instance | `t3.micro`, Amazon Linux 2023, private subnet, no public IP, IMDSv2 only, encrypted root volume |
| Client security group | **no inbound rules**; all outbound (to SSM via the NAT, and to the database) |
| Client IAM role + instance profile | `AmazonSSMManagedInstanceCore`, so the client registers with SSM |

Outputs: `db_endpoint`, `db_port`, `db_name`, `db_secret_arn`, `db_security_group_id`,
`client_instance_id`.

## Differences from the app

| | Local / compose | RDS |
|---|---|---|
| Postgres major | 15 | 15 |
| Database | `vectorpipe` | `vectorpipe` |
| User | `vectorpipe` | **`vectorpipe_admin`** (RDS master user; kept deliberately) |
| Password | in `.env` | Secrets Manager (`db_secret_arn`) |

To use RDS, the app's `DATABASE_URL` uses `vectorpipe_admin` and the password from the secret.

## How it got here

1. **The layer arrived as a zip drafted by another AI.** Every file was reviewed before use; the backend,
   provider constraint and the network outputs it reads all matched. No changes to the drafted files
   except the one below.
2. **`InsufficientDBInstanceCapacity`.** The first apply created 9 of 10 resources, then RDS refused
   `db.t4g.micro` with gp3: "there are no Availability Zones with sufficient capacity". A retry, unchanged,
   failed the same way. The error named no AZ, so pinning an AZ was not an option. An apply with
   `-var db_instance_class=db.t3.micro` succeeded (about 9.5 minutes).
3. **Drift after the `-var` apply.** The config still said `db.t4g.micro`, so the next plan wanted to change
   the class back (an in-place change with a restart, onto a class with no capacity). The default in
   `variables.tf` is now `db.t3.micro`, with a comment saying why. Plan: no changes. Moving back to t4g
   would cost a restart and gain nothing worth the risk.

## How to verify

```bash
aws rds describe-db-instances --db-instance-identifier vectorpipe \
  --query 'DBInstances[0].[DBInstanceStatus,MultiAZ,PubliclyAccessible,StorageEncrypted]'
# available, false, false, true
aws ec2 describe-security-groups --group-ids <db_security_group_id>
# only ingress: tcp 5432 from the client security group; no CIDR ranges
aws ssm describe-instance-information     # client: Online
cd terraform/data && terraform plan       # No changes
```

Verified on 2026-10-04: all four RDS values as expected; DB ingress only from the client SG; client SG
with no inbound rules; client `Online` in SSM; plan reports no changes.

## Connecting (via SSM port forwarding)

```bash
# terminal 1: tunnel local 5433 → RDS 5432 through the client
aws ssm start-session --target <client_instance_id> \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters '{"host":["<db_endpoint>"],"portNumber":["5432"],"localPortNumber":["5433"]}'

# terminal 2, from terraform/data
export PGPASSWORD=$(aws secretsmanager get-secret-value \
  --secret-id "$(terraform output -raw db_secret_arn)" \
  --query SecretString --output text | jq -r .password)
psql "host=localhost port=5433 dbname=vectorpipe user=vectorpipe_admin sslmode=require"
```

The secret ARN contains `!` (`rds!db-…`): if you paste it by hand, use single quotes, or the shell's
history expansion mangles it.

## Tear down

```bash
cd terraform/data && terraform destroy
cd ../network && terraform destroy
# all should be empty:
aws rds describe-db-instances
aws ec2 describe-nat-gateways --filter Name=state,Values=available
aws ec2 describe-instances --filters Name=instance-state-name,Values=running
aws ec2 describe-addresses        # catches a leaked Elastic IP, which keeps billing
```

## Left open

* Migrations, a `psql` connection and the full pipeline on RDS have not been run yet.
* The app still connects as the master user; a separate least-privilege app user is a later step.
* EKS workloads will need their own ingress rule on the DB security group (by SG ID).
