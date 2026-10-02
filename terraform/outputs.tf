output "s3_bucket" {
  value = aws_s3_bucket.documents.bucket
}

output "sqs_queue_url" {
  value = aws_sqs_queue.jobs.url
}

output "sqs_dlq_url" {
  value = aws_sqs_queue.dlq.url
}

output "ecr_repository_url" {
  value = aws_ecr_repository.app.repository_url
}

output "dev_role_arn" {
  value = aws_iam_role.dev.arn
}

output "api_policy_arn" {
  value = aws_iam_policy.api.arn
}

output "worker_policy_arn" {
  value = aws_iam_policy.worker.arn
}

output "region" {
  description = "Region of the bucket, queues and ECR repo; use it as AWS_REGION in the app"
  value       = var.region
}
