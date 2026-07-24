terraform {
    backend "s3" {
        bucket = "nyctaxi-tfstate-98741313131"
        key = "nyctaxi-ml-pipeline/terraform.tfstate"
        region = "us-east-1"
        use_lockfile = true
    }
}