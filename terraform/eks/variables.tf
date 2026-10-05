variable "region" {
  type    = string
  default = "us-east-1"
}

variable "project" {
  type    = string
  default = "vectorpipe" # also the cluster name
}

variable "kubernetes_version" {
  type        = string
  description = "Newest version in standard support. Look it up: aws eks describe-cluster-versions"
}

variable "my_ip_cidr" {
  type        = string
  description = "Your public IP as /32. Only this range can reach the cluster's public API endpoint."

  validation {
    condition     = can(cidrhost(var.my_ip_cidr, 0)) && endswith(var.my_ip_cidr, "/32")
    error_message = "Use a single address in CIDR form, e.g. 203.0.113.7/32."
  }
}

variable "admin_principal_arn" {
  type        = string
  description = "IAM principal that gets cluster-admin through an EKS access entry. Set it in terraform.tfvars (git-ignored)."
}

variable "node_instance_types" {
  type = list(string)
  # Free-plan accounts can only launch specific instance types; revisit if the account moves to a paid plan.
  # x86: your CI builds amd64 images, so no Graviton (t4g) nodes. The second type is the fallback.
  default = ["m7i-flex.large", "c7i-flex.large"]
}

variable "node_capacity_type" {
  type    = string
  default = "ON_DEMAND" # SPOT is cheaper but can be interrupted
}

variable "node_desired_size" {
  type    = number
  default = 2
}

variable "node_min_size" {
  type    = number
  default = 1
}

variable "node_max_size" {
  type    = number
  default = 3
}
