# Step 10: RDS, and a client nobody can SSH into

*October 4. Commits: "Add the data layer: private RDS PostgreSQL and an SSM-only test client",
"Document steps 9 and 10: network and data layers", "Record the RDS end-to-end run and tear down".*

## What I was trying to do

Get a managed Postgres that nothing on the internet can reach, plus a way to test it from inside the
VPC without opening a single port or managing an SSH key.

## Layer order

```
build:    foundation → network → data
destroy:  data → network         (foundation stays)
```

The data layer keeps its own state at `data/terraform.tfstate`. It reads `vpc_id` and
`private_subnet_ids` from the network layer through a `terraform_remote_state` data source. I didn't
want to copy IDs around by hand, because they change every time I rebuild the network.

## What I built (`terraform/data/`)

**The database.**
- PostgreSQL 15, the same major version I run locally.
- `db.t3.micro`, 20 GB gp3, encrypted.
- Single-AZ, and **not publicly accessible**.
- `skip_final_snapshot` is on and deletion protection is off, because I destroy it every session.

**The password.** I never see it in my code. RDS generates it and keeps it in Secrets Manager
(`manage_master_user_password`), so it isn't in code, `.env` or Terraform state.

**The DB security group.** It has one rule: TCP 5432 from the client's security group, referenced by
its ID. There's no CIDR range anywhere. When EKS arrives, it gets its own rule just like this one.

**The test client.**
- A `t3.micro` running Amazon Linux 2023 in a private subnet, with no public IP.
- IMDSv2 only, and an encrypted disk.
- Its security group has **no inbound rules at all**.
- I reach it through SSM Session Manager. Its IAM role has `AmazonSSMManagedInstanceCore`, so it
  registers with SSM through the NAT, and IAM decides who gets in.

## How the app's database differs

| | Local / compose | RDS |
|---|---|---|
| Postgres | 15 | 15 |
| Database | `vectorpipe` | `vectorpipe` |
| User | `vectorpipe` | `vectorpipe_admin` |
| Password | in `.env` | Secrets Manager |

I kept `vectorpipe_admin` on purpose. On RDS, the app's `DATABASE_URL` uses that user and the password
from the secret.

## How it actually went

**I reviewed every file before applying.** This layer was drafted outside the repo, so I read it all
first. The backend, the provider version and the network outputs it reads all matched. Postgres 15 and
the database name matched compose. The only difference was the username, and I decided to keep it.

**AWS was out of capacity.** The first apply created 9 of the 10 resources, then RDS refused:

```
InsufficientDBInstanceCapacity: You can't create a db.t4g.micro database instance because there are
no Availability Zones with sufficient capacity for VPC and storage type : gp3 for db.t4g.micro.
```

I retried, unchanged, and got the same error. It didn't name an AZ, so pinning one wasn't an option. I
applied again with `-var db_instance_class=db.t3.micro`, and that worked, after about 9 and a half
minutes.

**That left drift.** The config still said `db.t4g.micro`, so the next plan wanted to switch the
database back, which means a restart onto a class that had no capacity. I changed the default in
`variables.tf` to `db.t3.micro`, with a comment saying why. The plan went back to "no changes", which
is how I know the code and AWS agree. Switching back to t4g would cost a restart for no real gain.

## Proving it end to end

I did all of this while it was up and billing.

1. **Port-forward through the client.** Terminal 1:

   ```bash
   aws ssm start-session --target <client_instance_id> \
     --document-name AWS-StartPortForwardingSessionToRemoteHost \
     --parameters '{"host":["<db_endpoint>"],"portNumber":["5432"],"localPortNumber":["5433"]}'
   ```

   I use 5433 so it doesn't clash with a local Postgres. The AWS CLI needs the Session Manager plugin
   for this. I didn't have it, and installing it system-wide needs root, so I pulled the binary out of
   AWS's `.deb` package and put it on my `PATH`.

2. **Log in.** Terminal 2, from `terraform/data`:

   ```bash
   export PGPASSWORD=$(aws secretsmanager get-secret-value \
     --secret-id "$(terraform output -raw db_secret_arn)" \
     --query SecretString --output text | jq -r .password)
   psql "host=localhost port=5433 dbname=vectorpipe user=vectorpipe_admin sslmode=require"
   ```

   I was in as `vectorpipe_admin`, on PostgreSQL 15.17, with SSL on. Getting that prompt means the
   whole chain works: IAM, SSM, the security group and the login.

   Watch out: the secret's ARN contains a `!` (`rds!db-…`). If you paste it by hand, use single
   quotes, or the shell mangles it.

3. **Migrations.** The app reads `DATABASE_URL`, and the generated password can contain characters
   that break a URL, so I URL-encoded it:

   ```bash
   export DATABASE_URL="postgresql://vectorpipe_admin:$(python3 -c 'import os,urllib.parse;print(urllib.parse.quote(os.environ["PGPASSWORD"],safe=""))')@localhost:5433/vectorpipe?sslmode=require"
   alembic upgrade head
   ```

   Migration `0001` ran, and `\dt` showed `documents` and `alembic_version`.

4. **Privacy check.** From my laptop, straight at the endpoint, not through the tunnel: the name
   resolved to `10.0.16.8`, a private address, and the connection to port 5432 timed out after 5
   seconds. So the database isn't reachable from outside.

5. **The whole pipeline on RDS.** I ran the API and worker locally against RDS through the tunnel, with
   S3 and SQS through the `vectorpipe-dev` profile, and uploaded one document. It went to `COMPLETED`
   on the first attempt. The row was in RDS, both files were in S3, and the queue was empty.

## Tearing it down

I deleted the test document's S3 files, closed the tunnel, then:

```bash
cd terraform/data    && terraform destroy     # 10 destroyed
cd ../network        && terraform destroy     # 19 destroyed
```

Then I checked that nothing was left billing. All four came back empty:

```bash
aws rds describe-db-instances
aws ec2 describe-nat-gateways --filter Name=state,Values=available
aws ec2 describe-instances --filters Name=instance-state-name,Values=running
aws ec2 describe-addresses        # a leftover Elastic IP keeps billing
```

The RDS password secret went away with the database.

One surprise: `aws rds describe-db-snapshots` showed `database-1-snapshot`. It isn't from this
project. It's a manual Postgres 18 snapshot from September 11, from something I tried earlier, so I
left it where it was.

## Still open

- The app still connects as the master user. It should get its own user with only the grants it needs.
- EKS will need its own rule on the DB security group, by security group ID.
