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

## GitHub
variable "git_repo_url"{
  description = "Git URL for this repo, used to sync notebooks into the Databrics Workspace"
  type = string
  default = "https://github.com/mmereles/DE-ML-AI---nyctaxi"
}
