# Retained catalogue exports: one immutable object per run under exports/<run-id>/.
resource "aws_s3_bucket" "catalogue_exports" {
  bucket_prefix = "${var.name_prefix}-exports-"
  force_destroy = false
}

resource "aws_s3_bucket_ownership_controls" "catalogue_exports" {
  bucket = aws_s3_bucket.catalogue_exports.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "catalogue_exports" {
  bucket                  = aws_s3_bucket.catalogue_exports.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "catalogue_exports" {
  bucket = aws_s3_bucket.catalogue_exports.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "catalogue_exports" {
  bucket = aws_s3_bucket.catalogue_exports.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "catalogue_exports" {
  bucket = aws_s3_bucket.catalogue_exports.id

  rule {
    id     = "expire-exports"
    status = "Enabled"
    filter {
      prefix = "exports/"
    }
    expiration {
      days = var.export_retention_days
    }
    noncurrent_version_expiration {
      noncurrent_days = 7
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

data "aws_iam_policy_document" "exports_bucket" {
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.catalogue_exports.arn,
      "${aws_s3_bucket.catalogue_exports.arn}/*",
    ]
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

resource "aws_s3_bucket_policy" "catalogue_exports" {
  bucket     = aws_s3_bucket.catalogue_exports.id
  policy     = data.aws_iam_policy_document.exports_bucket.json
  depends_on = [aws_s3_bucket_public_access_block.catalogue_exports]
}

# Ledger: run, batch and per-SKU delivery records in one table.
#   (run_id=<run>,           batch_id=RUN)            run status
#   (run_id=<run>,           batch_id=BATCH#000001)   batch claim and outcome
#   (run_id=PRODUCT#<sku>,   batch_id=STATE)          fingerprint and delivery state
resource "aws_dynamodb_table" "sync_ledger" {
  name                        = "${var.name_prefix}-ledger"
  billing_mode                = "PAY_PER_REQUEST"
  hash_key                    = "run_id"
  range_key                   = "batch_id"
  deletion_protection_enabled = true

  attribute {
    name = "run_id"
    type = "S"
  }

  attribute {
    name = "batch_id"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }
}
