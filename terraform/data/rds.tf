locals {
  vpc_id             = data.terraform_remote_state.network.outputs.vpc_id
  private_subnet_ids = data.terraform_remote_state.network.outputs.private_subnet_ids
}

# --- Security groups -------------------------------------------------------

resource "aws_security_group" "client" {
  name        = "${var.project}-db-client"
  description = "Temporary DB test client: no inbound, access via SSM only"
  vpc_id      = local.vpc_id
}

# Outbound is needed to reach SSM (through the NAT) and the database.
resource "aws_vpc_security_group_egress_rule" "client_all" {
  security_group_id = aws_security_group.client.id
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_security_group" "db" {
  name        = "${var.project}-db"
  description = "Postgres: reachable only from approved security groups"
  vpc_id      = local.vpc_id
}

# Referenced by security group ID, never by CIDR. The EKS workloads get
# their own rule like this one later.
resource "aws_vpc_security_group_ingress_rule" "db_from_client" {
  security_group_id            = aws_security_group.db.id
  referenced_security_group_id = aws_security_group.client.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

# --- RDS -------------------------------------------------------------------

resource "aws_db_subnet_group" "main" {
  name       = var.project
  subnet_ids = local.private_subnet_ids # spans two AZs
}

resource "aws_db_instance" "main" {
  identifier     = var.project
  engine         = "postgres"
  engine_version = var.db_engine_version
  instance_class = var.db_instance_class

  allocated_storage = var.db_allocated_storage
  storage_type      = "gp3"
  storage_encrypted = true

  db_name  = var.db_name
  username = var.db_username
  # RDS generates the password and keeps it in Secrets Manager:
  # it never appears in code, .env or Terraform state.
  manage_master_user_password = true

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.db.id]
  publicly_accessible    = false
  multi_az               = var.multi_az

  auto_minor_version_upgrade = true
  backup_retention_period    = 1 # automated backups die with the instance anyway
  skip_final_snapshot        = true
  deletion_protection        = false # we destroy this every session
  apply_immediately          = true
}
