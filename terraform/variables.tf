variable "region" {
  type    = string
  default = "us-east-1"
}

variable "project" {
  type    = string
  default = "vectorpipe"
}

variable "dev_user_arn" {
  type        = string
  description = "IAM user (your laptop identity) allowed to assume the dev role"
}

variable "max_receive_count" {
  type        = number
  default     = 3
  description = "Receives before SQS moves a message to the DLQ. Must equal QUEUE_MAX_RECEIVE_COUNT in the app."
}

variable "visibility_timeout_seconds" {
  type        = number
  default     = 60
  description = "Must match the app's queue visibility timeout setting."
}

variable "ecr_images_to_keep" {
  type    = number
  default = 10
}
