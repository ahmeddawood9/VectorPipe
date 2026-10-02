terraform {
  required_version = ">= 1.10" # native S3 state locking (use_lockfile)

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # State bucket created by terraform/bootstrap. One state file per layer.
  backend "s3" {
    bucket       = "ingest-pipeline-tfstate-dev"
    key          = "foundation/terraform.tfstate"
    region       = "us-east-1" # same region as every resource (var.region); keep them equal
    use_lockfile = true
    encrypt      = true
  }
}
