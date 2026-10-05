# Cluster role: lets the EKS control plane manage AWS resources for the cluster.
data "aws_iam_policy_document" "cluster_trust" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["eks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "cluster" {
  name               = "${var.project}-eks-cluster"
  assume_role_policy = data.aws_iam_policy_document.cluster_trust.json
}

resource "aws_iam_role_policy_attachment" "cluster" {
  role       = aws_iam_role.cluster.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
}

# Node role: what the worker EC2 instances themselves may do.
data "aws_iam_policy_document" "node_trust" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "node" {
  name               = "${var.project}-eks-node"
  assume_role_policy = data.aws_iam_policy_document.node_trust.json
}

locals {
  node_policies = toset([
    "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
    # Shortcut: the VPC CNI's permissions ride on the node role. Production gives
    # the CNI its own role through Pod Identity.
    "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
    "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly", # pull images from ECR
    "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",       # shell on a node via SSM, no SSH
  ])
}

resource "aws_iam_role_policy_attachment" "node" {
  for_each   = local.node_policies
  role       = aws_iam_role.node.name
  policy_arn = each.value
}
