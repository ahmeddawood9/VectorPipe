locals {
  cluster_name      = data.terraform_remote_state.eks.outputs.cluster_name
  cluster_sg_id     = data.terraform_remote_state.eks.outputs.cluster_security_group_id
  db_sg_id          = data.terraform_remote_state.data.outputs.db_security_group_id
  db_secret_arn     = data.terraform_remote_state.data.outputs.db_secret_arn
  api_policy_arn    = data.terraform_remote_state.foundation.outputs.api_policy_arn
  worker_policy_arn = data.terraform_remote_state.foundation.outputs.worker_policy_arn
}

# --- Pod Identity: "pods using THIS service account get THIS role" -------------

data "aws_iam_policy_document" "pods_trust" {
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]

    principals {
      type        = "Service"
      identifiers = ["pods.eks.amazonaws.com"]
    }
  }
}

# API: S3 read/write/delete, SQS send (policy defined in the foundation layer)
resource "aws_iam_role" "api" {
  name               = "${var.project}-api-pod"
  assume_role_policy = data.aws_iam_policy_document.pods_trust.json
}

resource "aws_iam_role_policy_attachment" "api" {
  role       = aws_iam_role.api.name
  policy_arn = local.api_policy_arn
}

resource "aws_eks_pod_identity_association" "api" {
  cluster_name    = local.cluster_name
  namespace       = var.app_namespace
  service_account = "api"
  role_arn        = aws_iam_role.api.arn
}

# Worker: S3 read raw / write processed, SQS receive/delete/visibility
resource "aws_iam_role" "worker" {
  name               = "${var.project}-worker-pod"
  assume_role_policy = data.aws_iam_policy_document.pods_trust.json
}

resource "aws_iam_role_policy_attachment" "worker" {
  role       = aws_iam_role.worker.name
  policy_arn = local.worker_policy_arn
}

resource "aws_eks_pod_identity_association" "worker" {
  cluster_name    = local.cluster_name
  namespace       = var.app_namespace
  service_account = "worker"
  role_arn        = aws_iam_role.worker.arn
}

# External Secrets controller: the ONLY identity allowed to read the DB secret.
# The app pods never get Secrets Manager access.
resource "aws_iam_role" "eso" {
  name               = "${var.project}-external-secrets"
  assume_role_policy = data.aws_iam_policy_document.pods_trust.json
}

data "aws_iam_policy_document" "eso" {
  statement {
    sid       = "ReadDbSecret"
    actions   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
    resources = [local.db_secret_arn]
  }
}

resource "aws_iam_role_policy" "eso" {
  name   = "read-db-secret"
  role   = aws_iam_role.eso.id
  policy = data.aws_iam_policy_document.eso.json
}

resource "aws_eks_pod_identity_association" "eso" {
  cluster_name    = local.cluster_name
  namespace       = var.eso_namespace
  service_account = var.eso_service_account
  role_arn        = aws_iam_role.eso.arn
}

# --- Network path to the database -----------------------------------------------
# Nodes (and the pods on them) carry the cluster security group, so allowing it
# lets pods reach Postgres. Referenced by security group ID, never by CIDR.

resource "aws_vpc_security_group_ingress_rule" "db_from_cluster" {
  security_group_id            = local.db_sg_id
  referenced_security_group_id = local.cluster_sg_id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  description                  = "Postgres from EKS nodes and pods"
}
