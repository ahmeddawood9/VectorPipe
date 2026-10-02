# Policies are defined once. Later, EKS roles attach the one each workload needs;
# for now a dev role (assumed from your laptop) gets both.

data "aws_iam_policy_document" "api" {
  statement {
    sid       = "DocumentObjects"
    actions   = ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.documents.arn}/raw/*", "${aws_s3_bucket.documents.arn}/processed/*"]
  }

  # Without ListBucket, S3 answers 403 AccessDenied (not 404 NoSuchKey) for missing keys.
  statement {
    sid       = "ListBucketForNotFound"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.documents.arn]
  }

  statement {
    sid       = "Enqueue"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.jobs.arn]
  }

  # stats() reads both queues for the dashboard.
  statement {
    sid       = "QueueStats"
    actions   = ["sqs:GetQueueAttributes"]
    resources = [aws_sqs_queue.jobs.arn, aws_sqs_queue.dlq.arn]
  }
}

data "aws_iam_policy_document" "worker" {
  statement {
    sid       = "ReadRaw"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.documents.arn}/raw/*"]
  }

  statement {
    sid       = "WriteProcessed"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.documents.arn}/processed/*"]
  }

  statement {
    sid       = "ListBucketForNotFound"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.documents.arn]
  }

  statement {
    sid       = "ConsumeJobs"
    actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility"]
    resources = [aws_sqs_queue.jobs.arn]
  }

  # SqsQueue reads RedrivePolicy at startup and stats() reads the DLQ.
  statement {
    sid       = "QueueStats"
    actions   = ["sqs:GetQueueAttributes"]
    resources = [aws_sqs_queue.jobs.arn, aws_sqs_queue.dlq.arn]
  }
}

resource "aws_iam_policy" "api" {
  name   = "${var.project}-api"
  policy = data.aws_iam_policy_document.api.json
}

resource "aws_iam_policy" "worker" {
  name   = "${var.project}-worker"
  policy = data.aws_iam_policy_document.worker.json
}

# Laptop dev role: naming the user in the trust policy is enough within one account.
data "aws_iam_policy_document" "dev_trust" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "AWS"
      identifiers = [var.dev_user_arn]
    }
  }
}

resource "aws_iam_role" "dev" {
  name               = "${var.project}-dev"
  assume_role_policy = data.aws_iam_policy_document.dev_trust.json
}

resource "aws_iam_role_policy_attachment" "dev_api" {
  role       = aws_iam_role.dev.name
  policy_arn = aws_iam_policy.api.arn
}

resource "aws_iam_role_policy_attachment" "dev_worker" {
  role       = aws_iam_role.dev.name
  policy_arn = aws_iam_policy.worker.arn
}
