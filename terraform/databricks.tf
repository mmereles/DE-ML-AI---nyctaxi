# GitHub PAT so Databricks can clone the (private) repo - created by
# bootstrap.sh in Secrets Manager, read-only here (same pattern as the
# Databricks token: this config runs repeatedly, so it must never try to
# create/own the secret itself).
data "aws_secretsmanager_secret" "github_pat" {
  name = "nyctaxi/github-pat"
}

data "aws_secretsmanager_secret_version" "github_pat" {
  secret_id = data.aws_secretsmanager_secret.github_pat.id
}

resource "databricks_git_credential" "github" {
  git_username          = "mmereles"
  git_provider          = "gitHub"
  personal_access_token = jsondecode(data.aws_secretsmanager_secret_version.github_pat.secret_string)["token"]
}

resource "databricks_repo" "nyctaxi_pipeline" {
  url        = var.git_repo_url
  path       = "/Repos/production/nyctaxi-pipeline"
  depends_on = [databricks_git_credential.github]
}

resource "databricks_job" "processing" {
    name = "nyctaxi-processing"

    task {
        task_key = "process"
        notebook_task {
            notebook_path = "${databricks_repo.nyctaxi_pipeline.path}/nyctaxi - processing.ipynb"
        }
        
    }

    tags = {
        Project = "nyctaxi-ml-pipeline"
    }
}

