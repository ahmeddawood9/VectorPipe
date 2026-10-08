variable "region" {
  type    = string
  default = "us-east-1"
}

variable "project" {
  type    = string
  default = "vectorpipe"
}

variable "app_namespace" {
  type    = string
  default = "vectorpipe"
}

variable "eso_namespace" {
  type    = string
  default = "external-secrets"
}

variable "eso_service_account" {
  type    = string
  default = "external-secrets" # the name the ESO Helm chart uses by default
}
