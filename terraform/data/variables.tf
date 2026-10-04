variable "region" {
  type    = string
  default = "us-east-1"
}

variable "project" {
  type    = string
  default = "vectorpipe"
}

variable "multi_az" {
  type        = bool
  default     = false
  description = "Standby in a second AZ. Off for learning; flip on for a failover demo."
}

variable "db_instance_class" {
  type    = string
  default = "db.t3.micro" # t4g.micro had no capacity in us-east-1 on 2026-10-04
}

variable "db_allocated_storage" {
  type    = number
  default = 20
}

variable "db_engine_version" {
  type        = string
  default     = "15" # major only: AWS picks the current minor
  description = "Postgres version; match what you run locally."
}

variable "db_name" {
  type    = string
  default = "vectorpipe"
}

variable "db_username" {
  type    = string
  default = "vectorpipe_admin" # master user; "admin" is reserved
}

variable "client_instance_type" {
  type    = string
  default = "t3.micro"
}
