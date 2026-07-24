resource "databricks_repo" "nyctaxi_pipeline" {
  url = var.git_repo_url
  path = "/Repos/production/nyctaxi-pipeline"
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

