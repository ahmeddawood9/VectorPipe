variable "region" {
  type    = string
  default = "us-east-1"
}

variable "project" {
  type    = string
  default = "vectorpipe"
}

variable "vpc_cidr" {
  type    = string
  default = "10.0.0.0/16"
}

variable "nat_per_az" {
  type        = bool
  default     = false
  description = "false = one shared NAT (cheap, learning). true = one NAT per AZ (production)."
}
