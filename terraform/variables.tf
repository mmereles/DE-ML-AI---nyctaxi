# === VARIABLES ===

variable "databricks_host" {
  description = "Databrics workspace host URL"
  type        = string
  default     = "https://dbc-114a8cba-5f9f.cloud.databricks.com"
}

## GitHub
variable "git_repo_url" {
  description = "Git URL for this repo, used to sync notebooks into the Databrics Workspace"
  type        = string
  default     = "https://github.com/mmereles/DE-ML-AI---nyctaxi"
}

variable "model_version" {
  description = "Version del modelo en s3"
  type        = string
  default     = "1"
}

variable "sagemaker_xgboost_image" {
  description = "Imagen ECR del contenedor XGBoost de SageMaker"
  type        = string
  default     = "683313688378.dkr.ecr.us-east-1.amazonaws.com/sagemaker-xgboost:1.7-1"
}

variable "databricks_sql_warehouse_id" {
  description = "ID del SQL warehouse de Databricks que usa el agente (run_sql) para consultar Unity Catalog"
  type        = string
  default     = "27ed2322723070c7" # "Serverless Starter Warehouse"
}