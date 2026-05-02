terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  backend "s3" {
    bucket         = "terraform-state-btc-arbitrage-001"
    key            = "btc-arbitrage/dev/terraform.tfstate"
    region         = "us-east-2"
    dynamodb_table = "terraform-locks-btc-arbitrage"
    encrypt        = true
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = var.project_name
      Environment = var.environment
    }
  }
}

data "aws_caller_identity" "current" {}

# ---------------------------
# S3 DATA LAKE
# ---------------------------

resource "aws_s3_bucket" "data_lake" {
  bucket = var.bucket_name
}

resource "aws_s3_bucket_versioning" "data_lake_versioning" {
  bucket = aws_s3_bucket.data_lake.id

  versioning_configuration {
    status = "Disabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "data_lake_encryption" {
  bucket = aws_s3_bucket.data_lake.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "data_lake_block" {
  bucket = aws_s3_bucket.data_lake.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ---------------------------
# IAM ROLE FOR LAMBDA
# ---------------------------

resource "aws_iam_role" "lambda_role" {
  name = "lambda-${var.project_name}-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })
}

resource "aws_iam_role_policy" "lambda_policy" {
  name = "lambda-${var.project_name}-policy"
  role = aws_iam_role.lambda_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:PutObject",
          "s3:GetObject",
          "s3:ListBucket"
        ]
        Resource = [
          "arn:aws:s3:::${var.bucket_name}",
          "arn:aws:s3:::${var.bucket_name}/*"
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "*"
      }
    ]
  })
}

# ---------------------------
# LAMBDA FUNCTION
# ---------------------------

resource "aws_lambda_function" "fetch_binance" {
  function_name = "fetch-binance"
  filename      = "${path.module}/../lambdas/fetch_binance.zip"
  handler       = "handler.main"
  runtime       = "python3.11"
  role          = aws_iam_role.lambda_role.arn
  timeout       = 300
  memory_size   = 256

  environment {
    variables = {
      BUCKET_NAME = var.bucket_name
    }
  }

  source_code_hash = filebase64sha256("${path.module}/../lambdas/fetch_binance.zip")
}
# ---------------------------
# ORQUESTACIÓN (STEP FUNCTIONS)
# ---------------------------

resource "aws_iam_role" "sfn_role" {
  name = "sfn-${var.project_name}-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "states.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "sfn_lambda_policy" {
  name = "sfn-lambda-invoke-policy"
  role = aws_iam_role.sfn_role.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = [aws_lambda_function.fetch_binance.arn]
    }]
  })
}

resource "aws_sfn_state_machine" "btc_orchestrator" {
  name     = "${var.project_name}-batch-ingestion"
  role_arn = aws_iam_role.sfn_role.arn
  definition = jsonencode({
    StartAt = "IterarMeses"
    States = {
      IterarMeses = {
        Type           = "Map"
        ItemsPath      = "$.periodos"
        MaxConcurrency = 3
        Iterator = {
          StartAt = "DescargarDatos"
          States = {
            DescargarDatos = {
              Type     = "Task"
              Resource = aws_lambda_function.fetch_binance.arn
              End      = true
            }
          }
        }
        End = true
      }
    }
  })
}

output "bucket_name" {
  description = "Nombre del bucket del Data Lake"
  value       = aws_s3_bucket.data_lake.id
}

output "state_machine_arn" {
  description = "ARN de la state machine de Step Functions"
  value       = aws_sfn_state_machine.btc_orchestrator.arn
}