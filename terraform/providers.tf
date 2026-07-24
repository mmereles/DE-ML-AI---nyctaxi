terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.9"
    }
    databricks = {
      source = "databricks/databricks"
      version = "~> 1.55"
    }
  }
  required_version = ">= 1.5.0"
}

# Configure the AWS Provider
provider "aws" {
  region = "us-east-1"
}

provider "databricks" {
  host = var.databricks_host
}