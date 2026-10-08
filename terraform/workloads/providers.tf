terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  backend "s3" {
    bucket       = "ingest-pipeline-tfstate-dev"
    key          = "workloads/terraform.tfstate" # destroyed BEFORE eks, data and network
    region       = "us-east-1"
    use_lockfile = true
    encrypt      = true
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = var.project
      Layer     = "workloads"
      ManagedBy = "terraform"
    }
  }
}

# This layer plugs the app into AWS, so it reads three other layers.
# Apply order: network -> data -> eks -> workloads.
data "terraform_remote_state" "foundation" {
  backend = "s3"
  config = {
    bucket = "ingest-pipeline-tfstate-dev"
    key    = "foundation/terraform.tfstate"
    region = "us-east-1"
  }
}

data "terraform_remote_state" "data" {
  backend = "s3"
  config = {
    bucket = "ingest-pipeline-tfstate-dev"
    key    = "data/terraform.tfstate"
    region = "us-east-1"
  }
}

data "terraform_remote_state" "eks" {
  backend = "s3"
  config = {
    bucket = "ingest-pipeline-tfstate-dev"
    key    = "eks/terraform.tfstate"
    region = "us-east-1"
  }
}
