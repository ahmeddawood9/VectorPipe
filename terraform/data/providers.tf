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
    key          = "data/terraform.tfstate" # own state; destroyed before network
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
      Layer     = "data"
      ManagedBy = "terraform"
    }
  }
}

# Read the network layer's outputs instead of hardcoding IDs.
data "terraform_remote_state" "network" {
  backend = "s3"
  config = {
    bucket = "ingest-pipeline-tfstate-dev"
    key    = "network/terraform.tfstate"
    region = "us-east-1"
  }
}
