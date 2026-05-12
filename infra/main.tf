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
# Role compartido por todas las Lambdas de ingesta. Los permisos
# (S3 PutObject + CloudWatch Logs) son genéricos y no acoplan el role
# a una fuente específica.

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
# LAMBDA: FETCH-BINANCE
# ---------------------------

resource "aws_lambda_function" "fetch_binance" {
  function_name = "fetch-binance"
  filename      = "${path.module}/../build/fetch_binance.zip"
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

  source_code_hash = filebase64sha256("${path.module}/../build/fetch_binance.zip")
}

# ---------------------------
# LAMBDA: FETCH-BUDA
# ---------------------------

resource "aws_lambda_function" "fetch_buda" {
  function_name = "fetch-buda"
  filename      = "${path.module}/../build/fetch_buda.zip"
  handler       = "handler.main"
  runtime       = "python3.11"
  role          = aws_iam_role.lambda_role.arn

  # Timeout más generoso que Binance (300s):
  # Buda exige throttling de ~3s/req; un mes con alta liquidez puede
  # acercarse a 1500 páginas = ~75 min. El piloto ajustará este valor.
  # 900s (15 min) es el máximo de Lambda y un buffer razonable para empezar.
  timeout     = 900
  memory_size = 256

  environment {
    variables = {
      BUCKET_NAME = var.bucket_name
    }
  }

  source_code_hash = filebase64sha256("${path.module}/../build/fetch_buda.zip")
}

# ---------------------------
# STEP FUNCTIONS: BINANCE INGESTION
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

# ---------------------------
# STEP FUNCTIONS: BUDA INGESTION
# ---------------------------
# Decisión de diseño: state machine SEPARADA de la de Binance.
# Justificación:
#   - MaxConcurrency distinta (Buda=1 por rate limit, Binance=3).
#   - Métricas y dashboards separados por fuente.
#   - Políticas de retry/backoff potencialmente distintas a futuro.

resource "aws_iam_role" "sfn_buda_role" {
  name = "sfn-${var.project_name}-buda-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "states.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "sfn_buda_lambda_policy" {
  name = "sfn-buda-lambda-invoke-policy"
  role = aws_iam_role.sfn_buda_role.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = [aws_lambda_function.fetch_buda.arn]
    }]
  })
}

resource "aws_sfn_state_machine" "buda_orchestrator" {
  name     = "${var.project_name}-buda-ingestion"
  role_arn = aws_iam_role.sfn_buda_role.arn
  definition = jsonencode({
    StartAt = "IterarMeses"
    States = {
      IterarMeses = {
        Type      = "Map"
        ItemsPath = "$.periodos"

        # MaxConcurrency=1 deliberada:
        # Buda tiene rate limit global por IP de ~20 req/min. Paralelizar
        # múltiples meses simultáneos compartiría esa cuota y degradaría
        # cada worker. Mejor secuencial: cada mes es ~75min máximo, y la
        # Step Function puede correr varias horas sin problema.
        MaxConcurrency = 1

        Iterator = {
          StartAt = "DescargarTrades"
          States = {
            DescargarTrades = {
              Type     = "Task"
              Resource = aws_lambda_function.fetch_buda.arn

              # Retry sólo en errores transitorios de la plataforma Lambda
              # (no de Buda). Errores de Buda los maneja el handler
              # internamente con su propio backoff.
              Retry = [{
                ErrorEquals = [
                  "Lambda.ServiceException",
                  "Lambda.AWSLambdaException",
                  "Lambda.SdkClientException"
                ]
                IntervalSeconds = 30
                MaxAttempts     = 2
                BackoffRate     = 2.0
              }]

              End = true
            }
          }
        }
        End = true
      }
    }
  })
}

# ---------------------------
# LAMBDA: FETCH-MINDICADOR
# ---------------------------
# Descarga el histórico USD/CLP de MIndicador.cl en un único request por año.
# Sin layer adicional: usa solo stdlib (urllib.request) + boto3 del runtime.
# Timeout conservador: benchmark empírico mostró ~17s para 12 años con pausa
# de cortesía. 60s da margen 3.5x sin acercarse al límite de Lambda (900s).

resource "aws_lambda_function" "fetch_mindicador" {
  function_name = "fetch-mindicador"
  filename      = "${path.module}/../build/fetch_mindicador.zip"
  handler       = "handler.main"
  runtime       = "python3.11"
  role          = aws_iam_role.lambda_role.arn
  timeout       = 60
  memory_size   = 128

  environment {
    variables = {
      BUCKET_NAME = var.bucket_name
    }
  }

  source_code_hash = filebase64sha256("${path.module}/../build/fetch_mindicador.zip")
}


# ---------------------------
# LAMBDA: SILVER-BUDA
# ---------------------------
# Transformación Bronze → Silver para Buda BTC-CLP.
# Lee todos los JSON de bronze/backtest/buda/, reconstruye velas OHLCV de
# 1 minuto con forward-fill (is_interpolated=true para minutos sin trades),
# y escribe Parquet particionado a silver/backtest/unified_candles/.
#
# Decisión de diseño: single-Lambda procesa todo Buda en una invocación.
# Volumen real medido (~1.6M trades, archivo máximo 1MB) cabe holgado en
# memoria. Procesar todo junto resuelve trivialmente el forward-fill que
# cruza fronteras de mes, sin acoplamiento entre archivos. Ver ADR-007.
#
# Layer público AWSSDKPandas-Python311 v20 trae pandas 2.x + pyarrow,
# evitando empaquetar ~80MB de dependencias en el zip.

resource "aws_lambda_function" "silver_buda" {
  function_name = "silver-buda"
  filename      = "${path.module}/../build/silver_buda.zip"
  handler       = "handler.main"
  runtime       = "python3.11"
  role          = aws_iam_role.lambda_role.arn

  # Memoria: 3008 MB es un sweet spot — Lambda asigna 2 vCPU a partir de
  # 1769 MB, suficiente para pandas en este volumen. El resampling 1m
  # sobre ~1.6M trades + reindex a grilla continua es el pico de memoria.
  memory_size = 3008

  # Timeout: 5 min holgado. Benchmarks locales con pandas sobre ~1.5M
  # filas resamplean en <30s; el grueso del tiempo será descarga de
  # ~250 archivos JSON desde S3 (~500ms cada uno = ~2 min).
  timeout = 300

  layers = [var.aws_sdk_pandas_layer_arn]

  environment {
    variables = {
      DATA_LAKE_BUCKET = var.bucket_name
      BRONZE_PREFIX    = "bronze/backtest/buda/"
      SILVER_PREFIX    = "silver/backtest/unified_candles/"
    }
  }

  source_code_hash = filebase64sha256("${path.module}/../build/silver_buda.zip")
}

# ---------------------------
# SILVER LAMBDA: BINANCE (Bronze → Silver)
# ---------------------------
# Patrón idéntico a silver-buda (ADR-007 schema unificado). Diferencias:
#   - Memoria 3008 MB: igual a silver-buda. No es decisión de diseño sino
#     límite de cuenta AWS (quota default = 3008 MB; subir requiere request
#     a AWS Support, no justificable para portfolio). Peak estimado ~800-
#     1200 MB con 2-3 DataFrames coexistiendo durante el JOIN FX; queda
#     ~60% de headroom. Si en producción vemos peak >70%, optimizar el
#     handler (del df_raw post-reindex, etc.) antes de pedir quota.
#   - Timeout 600s (vs 300s de buda): binance procesa ~3x más bytes Bronze
#     (~780 MB vs ~250 MB) y suma JOIN FX vectorizado sobre ~4.5M klines.
#     Estimación 2-5 min; 600s da margen 2-3x. El techo absoluto es 900s
#     (15min) y no requiere quota.
#   - Env var adicional FX_BRONZE_KEY: el handler lee FX directo desde
#     Bronze (ADR-008 Decisión 1) en lugar de depender de un Silver FX
#     intermedio.
# El zip incluye lambdas/common/fx.py embebido (ver build_lambdas.sh
# y LAMBDAS_NEEDING_COMMON).
resource "aws_lambda_function" "silver_binance" {
  function_name = "silver-binance"
  filename      = "${path.module}/../build/silver_binance.zip"
  handler       = "handler.main"
  runtime       = "python3.11"
  role          = aws_iam_role.lambda_role.arn
  memory_size   = 3008
  timeout       = 600
  layers        = [var.aws_sdk_pandas_layer_arn]
  environment {
    variables = {
      DATA_LAKE_BUCKET = var.bucket_name
      BRONZE_PREFIX    = "bronze/backtest/binance/"
      SILVER_PREFIX    = "silver/backtest/unified_candles/"
      FX_BRONZE_KEY    = "bronze/backtest/fx/usdclp_dolar_mindicador.json"
    }
  }
  source_code_hash = filebase64sha256("${path.module}/../build/silver_binance.zip")
}

resource "aws_lambda_function" "silver_fx" {
  function_name = "silver-fx"
  filename      = "${path.module}/../build/silver_fx.zip"
  handler       = "handler.main"
  runtime       = "python3.11"
  role          = aws_iam_role.lambda_role.arn
  memory_size   = 512
  timeout       = 60
  layers        = [var.aws_sdk_pandas_layer_arn]
  environment {
    variables = {
      BUCKET_NAME = var.bucket_name
    }
  }
  source_code_hash = filebase64sha256("${path.module}/../build/silver_fx.zip")
}

# ---------------------------
# STEP FUNCTIONS: MINDICADOR INGESTION
# ---------------------------
# Decisión de diseño: state machine propia (no compartida con Binance/Buda).
# Justificación:
#   - Semántica distinta: no es un Map sobre periodos, es un Task único.
#   - Métricas y ejecuciones separadas por fuente (mismo criterio que Buda).
#   - Permite invocarla de forma independiente para re-runs del año en curso.

resource "aws_iam_role" "sfn_mindicador_role" {
  name = "sfn-${var.project_name}-mindicador-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "states.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "sfn_mindicador_lambda_policy" {
  name = "sfn-mindicador-lambda-invoke-policy"
  role = aws_iam_role.sfn_mindicador_role.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = [aws_lambda_function.fetch_mindicador.arn]
    }]
  })
}

resource "aws_sfn_state_machine" "mindicador_orchestrator" {
  name     = "${var.project_name}-mindicador-ingestion"
  role_arn = aws_iam_role.sfn_mindicador_role.arn
  definition = jsonencode({
    StartAt = "DescargarUSDCLP"
    States = {
      DescargarUSDCLP = {
        Type     = "Task"
        Resource = aws_lambda_function.fetch_mindicador.arn

        # Input opcional: {} para backfill completo (defaults en el handler),
        # o {"year_start": 2024, "year_end": 2026} para re-runs parciales.
        Retry = [{
          ErrorEquals = [
            "Lambda.ServiceException",
            "Lambda.AWSLambdaException",
            "Lambda.SdkClientException"
          ]
          IntervalSeconds = 30
          MaxAttempts     = 2
          BackoffRate     = 2.0
        }]

        End = true
      }
    }
  })
}



# ---------------------------
# ATHENA + GLUE CATALOG (Fase A.4)
# ---------------------------

# Bucket separado para query results de Athena (ephemeral)
resource "aws_s3_bucket" "athena_results" {
  bucket = var.athena_results_bucket_name
}

resource "aws_s3_bucket_public_access_block" "athena_results" {
  bucket = aws_s3_bucket.athena_results.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "athena_results" {
  bucket = aws_s3_bucket.athena_results.id

  rule {
    id     = "expire-query-results-7d"
    status = "Enabled"

    filter {}

    expiration {
      days = 7
    }
  }
}

# Glue Data Catalog: database
resource "aws_glue_catalog_database" "arbitraje_btc" {
  name        = "arbitraje_btc"
  description = "Catálogo Silver para análisis de arbitraje BTC Buda-Binance"
}

# Tabla particionada: unified_candles (Buda + Binance)
# Partition projection: cero estado mutable en el catálogo
resource "aws_glue_catalog_table" "unified_candles" {
  name          = "unified_candles"
  database_name = aws_glue_catalog_database.arbitraje_btc.name
  table_type    = "EXTERNAL_TABLE"

  parameters = {
    "classification"            = "parquet"
    "projection.enabled"        = "true"
    "projection.year.type"      = "integer"
    "projection.year.range"     = "${var.unified_candles_year_min},${var.unified_candles_year_max}"
    "projection.month.type"     = "integer"
    "projection.month.range"    = "1,12"
    "projection.month.digits"   = "2"
    "storage.location.template" = "s3://${aws_s3_bucket.data_lake.id}/silver/backtest/unified_candles/year=$${year}/month=$${month}/"
  }

  partition_keys {
    name = "year"
    type = "int"
  }

  partition_keys {
    name = "month"
    type = "int"
  }

  storage_descriptor {
    location      = "s3://${aws_s3_bucket.data_lake.id}/silver/backtest/unified_candles/"
    input_format  = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat"

    ser_de_info {
      name                  = "parquet-serde"
      serialization_library = "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"

      parameters = {
        "serialization.format" = "1"
      }
    }

    columns {
      name = "timestamp"
      type = "timestamp"
    }
    columns {
      name = "exchange"
      type = "string"
    }
    columns {
      name = "open_clp"
      type = "double"
    }
    columns {
      name = "high_clp"
      type = "double"
    }
    columns {
      name = "low_clp"
      type = "double"
    }
    columns {
      name = "close_clp"
      type = "double"
    }
    columns {
      name = "volume_btc"
      type = "double"
    }
    columns {
      name = "buy_volume_btc"
      type = "double"
    }
    columns {
      name = "sell_volume_btc"
      type = "double"
    }
    columns {
      name = "trade_count"
      type = "bigint"
    }
    columns {
      name = "is_interpolated"
      type = "boolean"
    }
  }
}

# Tabla sin particionar: fx_usdclp (archivo único)
resource "aws_glue_catalog_table" "fx_usdclp" {
  name          = "fx_usdclp"
  database_name = aws_glue_catalog_database.arbitraje_btc.name
  table_type    = "EXTERNAL_TABLE"

  parameters = {
    "classification" = "parquet"
  }

  storage_descriptor {
    location      = "s3://${aws_s3_bucket.data_lake.id}/silver/backtest/fx/"
    input_format  = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat"

    ser_de_info {
      name                  = "parquet-serde"
      serialization_library = "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"

      parameters = {
        "serialization.format" = "1"
      }
    }

    columns {
      name = "date"
      type = "date"
    }
    columns {
      name = "usdclp"
      type = "double"
    }
    columns {
      name = "is_ffilled"
      type = "boolean"
    }
  }
}

# Athena workgroup con cost guardrail
resource "aws_athena_workgroup" "arbitraje_btc_wg" {
  name        = "arbitraje_btc_wg"
  description = "Workgroup para queries del proyecto BTC arbitrage. 100MB cutoff."

  configuration {
    enforce_workgroup_configuration    = true
    publish_cloudwatch_metrics_enabled = true
    bytes_scanned_cutoff_per_query     = var.athena_bytes_scanned_cutoff

    result_configuration {
      output_location = "s3://${aws_s3_bucket.athena_results.id}/queries/"

      encryption_configuration {
        encryption_option = "SSE_S3"
      }
    }
  }
}

# ---------------------------
# OUTPUTS
# ---------------------------

output "bucket_name" {
  description = "Nombre del bucket del Data Lake"
  value       = aws_s3_bucket.data_lake.id
}

output "state_machine_arn" {
  description = "ARN de la state machine de ingesta de Binance"
  value       = aws_sfn_state_machine.btc_orchestrator.arn
}

output "buda_state_machine_arn" {
  description = "ARN de la state machine de ingesta de Buda"
  value       = aws_sfn_state_machine.buda_orchestrator.arn
}

output "fetch_buda_lambda_arn" {
  description = "ARN de la Lambda fetch-buda"
  value       = aws_lambda_function.fetch_buda.arn
}

output "fetch_mindicador_lambda_arn" {
  description = "ARN de la Lambda fetch-mindicador"
  value       = aws_lambda_function.fetch_mindicador.arn
}

output "mindicador_state_machine_arn" {
  description = "ARN de la state machine de ingesta USD/CLP (MIndicador)"
  value       = aws_sfn_state_machine.mindicador_orchestrator.arn
}

output "silver_buda_lambda_arn" {
  description = "ARN de la Lambda silver-buda (transformación Bronze→Silver)"
  value       = aws_lambda_function.silver_buda.arn
}

output "silver_binance_lambda_arn" {
  description = "ARN de la Lambda silver-binance (transformación Bronze→Silver)"
  value       = aws_lambda_function.silver_binance.arn
}

output "silver_fx_lambda_arn" {
  description = "ARN de la Lambda silver-fx (transformación Bronze→Silver para USD/CLP)"
  value       = aws_lambda_function.silver_fx.arn
}

output "athena_workgroup_name" {
  description = "Workgroup de Athena para correr queries del proyecto"
  value       = aws_athena_workgroup.arbitraje_btc_wg.name
}

output "glue_database_name" {
  description = "Database de Glue Catalog (Silver layer)"
  value       = aws_glue_catalog_database.arbitraje_btc.name
}

output "athena_results_bucket" {
  description = "Bucket donde se guardan los results de Athena (lifecycle 7d)"
  value       = aws_s3_bucket.athena_results.id
}
