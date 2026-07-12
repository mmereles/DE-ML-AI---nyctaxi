# === VARIABLES ===

variable "databricks_host" {
  description = "Databrics workspace host URL"
  type        = string
  default     = "https://dbc-114a8cba-5f9f.cloud.databricks.com"
}

variable "databricks_job_id" {
  description = "Databricks job ID for processing"
  type        = string
  default     = "352495504571507"
}