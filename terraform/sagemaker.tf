locals {
  model_s3_key   = "sagemaker/fare-quote-model/v${var.model_version}/model.tar.gz"
  model_data_url = "s3://${aws_s3_bucket.nyc_taxi_bucket.id}/${local.model_s3_key}"
}

# ETag del artefacto real en S3 - ver el nombre de aws_sagemaker_model mas
# abajo para el porque: model_version solo no alcanza para detectar cuando
# se re-exporto el mismo champion con contenido distinto (ej. un fix en
# inference.py sin reentrenar).
data "aws_s3_object" "fare_quote_model" {
  bucket = aws_s3_bucket.nyc_taxi_bucket.id
  key    = local.model_s3_key
}

# Rol de ejecucion de SageMaker: solo puede leer el artefacto del modelo y
# escribir logs
resource "aws_iam_role" "sagemaker_exec" {
  name = "nyctaxi-sagemaker-exec-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "sagemaker.amazonaws.com" }
    }]
  })

  tags = {
    Project = "nyctaxi-ml-pipeline"
  }
}

resource "aws_iam_role_policy" "sagemaker_exec_policy" {
  name = "nyctaxi-sagemaker-exec-policy"
  role = aws_iam_role.sagemaker_exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "arn:aws:s3:::${aws_s3_bucket.nyc_taxi_bucket.id}/sagemaker/fare-quote-model/*"
      },
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = "arn:aws:s3:::${aws_s3_bucket.nyc_taxi_bucket.id}"
        Condition = {
          StringLike = { "s3:prefix" = ["sagemaker/fare-quote-model/*"] }
        }
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:*:*:log-group:/aws/sagemaker/Endpoints/*"
      }
    ]
  })
}

resource "aws_sagemaker_model" "fare_quote" {
  # El nombre incluye el ETag del artefacto en S3 (no solo model_version):
  # los Modelos de SageMaker son inmutables (no existe UpdateModel para
  # model_data_url), asi que cualquier cambio de contenido - incluso
  # re-exportar el mismo model_version con inference.py distinto, sin
  # reentrenar - tiene que forzar un nombre nuevo para poder reemplazar el
  # recurso con create_before_destroy. Con nombre fijo, Terraform no puede
  # crear el reemplazo antes de borrar el viejo (choque de nombres) y
  # create_before_destroy queda inutil - aws_sagemaker_model tampoco
  # soporta name_prefix (a diferencia de aws_sagemaker_endpoint_configuration
  # mas abajo), por eso se arma a mano.
  name = "nyctaxi-fare-quote-${substr(replace(data.aws_s3_object.fare_quote_model.etag, "-", ""), 0, 12)}"

  execution_role_arn = aws_iam_role.sagemaker_exec.arn

  primary_container {
    image          = var.sagemaker_xgboost_image
    model_data_url = local.model_data_url

    environment = {
      SAGEMAKER_PROGRAM             = "inference.py"
      SAGEMAKER_SUBMIT_DIRECTORY    = "/opt/ml/model/code"
      SAGEMAKER_CONTAINER_LOG_LEVEL = "20"
    }
  }

  lifecycle {
    create_before_destroy = true
  }

  tags = {
    Project = "nyctaxi-ml-pipeline"
  }
}

resource "aws_sagemaker_endpoint_configuration" "fare_quote" {
  name_prefix = "nyctaxi-fare-quote-config-"

  production_variants {
    variant_name = "AllTraffic"
    model_name   = aws_sagemaker_model.fare_quote.name

    serverless_config {
      max_concurrency   = 5
      memory_size_in_mb = 2048
    }
  }

  lifecycle {
    create_before_destroy = true
  }

  tags = {
    Project = "nyctaxi-ml-pipeline"
  }
}

resource "aws_sagemaker_endpoint" "fare_quote" {
  name                 = "nyctaxi-fare-quote"
  endpoint_config_name = aws_sagemaker_endpoint_configuration.fare_quote.name

  tags = {
    Project = "nyctaxi-ml-pipeline"
  }
}