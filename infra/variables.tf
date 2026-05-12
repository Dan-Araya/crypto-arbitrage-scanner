variable "aws_region" {
  description = "Región de AWS"
  type        = string
  default     = "us-east-2"
}

variable "project_name" {
  description = "Project name"
  type        = string
  default     = "btc-arbitrage"
}

variable "environment" {
  description = "Environment"
  type        = string
  default     = "dev"
}

variable "bucket_name" {
  description = "Bucket name for the Data Lake"
  type        = string
  default     = "btc-arbitrage-data-lake-001"
}

variable "aws_sdk_pandas_layer_arn" {
  description = "ARN del layer público AWS SDK for Pandas (Python 3.11). Trae pandas + pyarrow + numpy precompilados, evitando empaquetar ~80MB de dependencias en cada zip de Lambda. Versión pineada deliberadamente para reproducibilidad: cambios de versión deben ser explícitos."
  type        = string
  default     = "arn:aws:lambda:us-east-2:336392948345:layer:AWSSDKPandas-Python311:31"
}

variable "athena_results_bucket_name" {
  description = "Bucket S3 para query results de Athena (ephemeral, 7d lifecycle)"
  type        = string
  default     = "btc-arbitrage-athena-results-001"
}

variable "unified_candles_year_min" {
  description = "Año mínimo para partition projection de unified_candles"
  type        = number
  default     = 2017
}

variable "unified_candles_year_max" {
  description = "Año máximo para partition projection de unified_candles"
  type        = number
  default     = 2026
}

variable "athena_bytes_scanned_cutoff" {
  description = "Cutoff de bytes escaneados por query en el workgroup. Default 100MB; subir temporal a 1GB (1073741824) para queries de análisis sobre todo el histórico."
  type        = number
  default     = 104857600 # 100 MB
}