# DLQ first: the main queue's redrive policy references its ARN.
resource "aws_sqs_queue" "dlq" {
  name                      = "${var.project}-jobs-dlq"
  message_retention_seconds = 1209600 # 14 days; must exceed the main queue's retention
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "jobs" {
  name                       = "${var.project}-jobs"
  visibility_timeout_seconds = var.visibility_timeout_seconds
  message_retention_seconds  = 345600 # 4 days
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = var.max_receive_count
  })
}
