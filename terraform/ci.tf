# GitHub Actions -> AWS via OIDC: no stored access keys.
# Goes in terraform/ (foundation layer). One GitHub OIDC provider per account.

variable "github_repo" {
  type        = string
  default     = "ahmeddawood9/VectorPipe" # case-sensitive: must match the repo exactly
  description = "owner/name of the repo allowed to push images"
}

# The repo uses GitHub's immutable OIDC subject, so the token's sub embeds the numeric owner and repo
# ids: repo:<owner>@<owner_id>/<repo>@<repo_id>:ref:... A renamed or re-created repo of the same name
# gets new ids and cannot assume the role. Look them up with:
#   gh api repos/<owner>/<repo>/actions/oidc/customization/sub   (sub_claim_prefix)
variable "github_owner_id" {
  type    = number
  default = 147309010
}

variable "github_repo_id" {
  type    = number
  default = 1398670261
}

locals {
  github_owner     = split("/", var.github_repo)[0]
  github_repo_name = split("/", var.github_repo)[1]
  github_sub_repo  = "${local.github_owner}@${var.github_owner_id}/${local.github_repo_name}@${var.github_repo_id}"
}

# AWS verifies GitHub's signing certificates itself, so no thumbprint is needed.
resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

data "aws_iam_policy_document" "ci_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    # Only workflow runs on this repo's main branch can assume the role.
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${local.github_sub_repo}:ref:refs/heads/main"]
    }
  }
}

resource "aws_iam_role" "ci" {
  name               = "${var.project}-ci"
  assume_role_policy = data.aws_iam_policy_document.ci_trust.json
}

data "aws_iam_policy_document" "ci_push" {
  # The auth-token call cannot be scoped to a repository.
  statement {
    sid       = "EcrAuth"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid = "EcrPush"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:InitiateLayerUpload",
      "ecr:UploadLayerPart",
      "ecr:CompleteLayerUpload",
      "ecr:PutImage",
    ]
    resources = [aws_ecr_repository.app.arn]
  }
}

resource "aws_iam_role_policy" "ci_push" {
  name   = "ecr-push"
  role   = aws_iam_role.ci.id
  policy = data.aws_iam_policy_document.ci_push.json
}

output "ci_role_arn" {
  value = aws_iam_role.ci.arn
}
