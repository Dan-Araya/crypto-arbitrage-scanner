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

variable "account_id" {
  description = "AWS Account ID"
  type        = string
  default     = "366985589914"
}