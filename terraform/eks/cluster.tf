locals {
  private_subnet_ids = data.terraform_remote_state.network.outputs.private_subnet_ids
}

resource "aws_eks_cluster" "main" {
  name     = var.project
  role_arn = aws_iam_role.cluster.arn
  version  = var.kubernetes_version

  # Access entries (the API) instead of the old aws-auth ConfigMap.
  access_config {
    authentication_mode                         = "API"
    bootstrap_cluster_creator_admin_permissions = false # admin is granted explicitly below
  }

  vpc_config {
    subnet_ids = local.private_subnet_ids

    # Private endpoint ON so nodes reach the API privately. If it were off and the
    # public endpoint restricted to your IP, the nodes (coming from the NAT) would
    # be locked out and never join.
    endpoint_private_access = true
    endpoint_public_access  = true
    public_access_cidrs     = [var.my_ip_cidr]
  }

  depends_on = [aws_iam_role_policy_attachment.cluster]
}

# --- Who may use the cluster ---------------------------------------------------

resource "aws_eks_access_entry" "admin" {
  cluster_name  = aws_eks_cluster.main.name
  principal_arn = var.admin_principal_arn
  type          = "STANDARD"
}

resource "aws_eks_access_policy_association" "admin" {
  cluster_name  = aws_eks_cluster.main.name
  principal_arn = var.admin_principal_arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"

  access_scope {
    type = "cluster"
  }

  depends_on = [aws_eks_access_entry.admin]
}

# --- Add-ons ---------------------------------------------------------------------
# Networking add-ons first, so nodes can become Ready.

resource "aws_eks_addon" "vpc_cni" {
  cluster_name                = aws_eks_cluster.main.name
  addon_name                  = "vpc-cni"
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"
}

resource "aws_eks_addon" "kube_proxy" {
  cluster_name                = aws_eks_cluster.main.name
  addon_name                  = "kube-proxy"
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"
}

# --- Worker nodes ------------------------------------------------------------------

resource "aws_eks_node_group" "main" {
  cluster_name    = aws_eks_cluster.main.name
  node_group_name = "main"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = local.private_subnet_ids

  instance_types = var.node_instance_types
  capacity_type  = var.node_capacity_type
  ami_type       = "AL2023_x86_64_STANDARD"

  scaling_config {
    desired_size = var.node_desired_size
    min_size     = var.node_min_size
    max_size     = var.node_max_size
  }

  update_config {
    max_unavailable = 1
  }

  depends_on = [
    aws_iam_role_policy_attachment.node,
    aws_eks_addon.vpc_cni,
    aws_eks_addon.kube_proxy,
  ]
}

# These two run as pods, so they need nodes to exist first.
resource "aws_eks_addon" "coredns" {
  cluster_name                = aws_eks_cluster.main.name
  addon_name                  = "coredns"
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"

  depends_on = [aws_eks_node_group.main]
}

resource "aws_eks_addon" "pod_identity_agent" {
  cluster_name                = aws_eks_cluster.main.name
  addon_name                  = "eks-pod-identity-agent"
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"

  depends_on = [aws_eks_node_group.main]
}
