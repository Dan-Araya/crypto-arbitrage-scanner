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