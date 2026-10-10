# --- AWS Load Balancer Controller: IAM + Pod Identity (add to terraform/workloads) ---
# The controller runs in kube-system and calls the ELB/EC2 APIs to create ALBs.
# Policy JSON is the official one, PINNED to the same tag as the controller version
# (see lbc-iam-policy.json; it is the policy file from the controller's own repository at that same tag).

data "aws_iam_policy_document" "lbc_trust" {
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]
    principals {
      type        = "Service"
      identifiers = ["pods.eks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lbc" {
  name               = "vectorpipe-lb-controller"
  assume_role_policy = data.aws_iam_policy_document.lbc_trust.json
}

resource "aws_iam_policy" "lbc" {
  name   = "vectorpipe-lb-controller"
  policy = file("${path.module}/lbc-iam-policy.json")
}

resource "aws_iam_role_policy_attachment" "lbc" {
  role       = aws_iam_role.lbc.name
  policy_arn = aws_iam_policy.lbc.arn
}

# Must exist BEFORE the controller pods start (credentials are injected at pod creation).
resource "aws_eks_pod_identity_association" "lbc" {
  cluster_name    = data.terraform_remote_state.eks.outputs.cluster_name # data source declared in providers.tf
  namespace       = "kube-system"
  service_account = "aws-load-balancer-controller"
  role_arn        = aws_iam_role.lbc.arn
}
