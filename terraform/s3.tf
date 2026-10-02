data "aws_caller_identity" "current" {}

# Bucket names are global; the account id keeps ours unique.
resource "aws_s3_bucket" "documents" {
  bucket = "${var.project}-documents-${data.aws_caller_identity.current.account_id}"
  # force_destroy stays false: destroy fails if the bucket holds data (intentional).
}

resource "aws_s3_bucket_public_access_block" "documents" {
  bucket                  = aws_s3_bucket.documents.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# AES256 = SSE-S3, matches ServerSideEncryption="AES256" in the adapter.
resource "aws_s3_bucket_server_side_encryption_configuration" "documents" {
  bucket = aws_s3_bucket.documents.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

data "aws_iam_policy_document" "documents_tls_only" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.documents.arn, "${aws_s3_bucket.documents.arn}/*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "documents" {
  bucket = aws_s3_bucket.documents.id
  policy = data.aws_iam_policy_document.documents_tls_only.json

  # Applying policy and public-access-block at the same time can fail with OperationAborted.
  depends_on = [aws_s3_bucket_public_access_block.documents]
}
