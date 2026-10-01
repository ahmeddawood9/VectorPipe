output "state_bucket" {
  value = aws_s3_bucket.tf_state.id
}

output "backend_config" {
  description = "Paste into the backend \"s3\" block of the main stack"
  value       = <<-EOT
    bucket         = "${aws_s3_bucket.tf_state.id}"
    key            = "vectorpipe/terraform.tfstate"
    region         = "${var.aws_region}"
    use_lockfile   = true
    encrypt        = true
  EOT
}
