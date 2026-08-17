# Lambda que traduce el request publico + endpoint de SageMaker
resource "aws_lambda_function" "quote_api" {
  function_name = "nyctaxi-quote-api"
  description   = "API de cotizacion de tarifas: valida input y consulta el modelo servido en SageMaker"
  role          = aws_iam_role.quote_api_exec.arn
  handler       = "quote_api.lambda_handler"
  runtime       = "python3.13"
  timeout       = 30
  memory_size   = 256

  filename         = data.archive_file.quote_api_zip.output_path
  source_code_hash = data.archive_file.quote_api_zip.output_base64sha256

  environment {
    variables = {
      SAGEMAKER_ENDPOINT_NAME = aws_sagemaker_endpoint.fare_quote.name
    }
  }

  tags = {
    Project     = "nyctaxi-ml-pipeline"
    Environment = "production"
  }
}

# Rol de la lambda: solo logs + invocar el endpoint de SageMaker
resource "aws_iam_role" "quote_api_exec" {
  name = "nyctaxi-quote-api-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })

  tags = {
    Project = "nyctaxi-ml-pipeline"
  }
}

resource "aws_iam_role_policy" "quote_api_policy" {
  name = "nyctaxi-quote-api-policy"
  role = aws_iam_role.quote_api_exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:*:*:*"
      },
      {
        Effect   = "Allow"
        Action   = ["sagemaker:InvokeEndpoint"]
        Resource = aws_sagemaker_endpoint.fare_quote.arn
      }
    ]
  })
}

# API Gateway HTTP API (v2)
resource "aws_apigatewayv2_api" "quote_api" {
  name          = "nyctaxi-quote-api"
  protocol_type = "HTTP"

  tags = {
    Project = "nyctaxi-ml-pipeline"
  }
}

resource "aws_apigatewayv2_integration" "quote_lambda" {
  api_id                 = aws_apigatewayv2_api.quote_api.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.quote_api.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "post_quote" {
  api_id    = aws_apigatewayv2_api.quote_api.id
  route_key = "POST /quote"
  target    = "integrations/${aws_apigatewayv2_integration.quote_lambda.id}"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.quote_api.id
  name        = "$default"
  auto_deploy = true

  default_route_settings {
    throttling_rate_limit  = 10
    throttling_burst_limit = 20
  }

  tags = {
    Project = "nyctaxi-ml-pipeline"
  }
}

resource "aws_lambda_permission" "apigw_invoke_quote" {
  statement_id  = "AllowExecutionFromAPIGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.quote_api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.quote_api.execution_arn}/*/*"
}

resource "aws_cloudwatch_log_group" "quote_api_logs" {
  name              = "/aws/lambda/${aws_lambda_function.quote_api.function_name}"
  retention_in_days = 14

  tags = {
    Project = "nyctaxi-ml-pipeline"
  }
}

# zip solo con el handler de esta lambda
data "archive_file" "quote_api_zip" {
  type        = "zip"
  source_dir  = "${path.module}/../lambda"
  output_path = "${path.module}/../quote_api_function.zip"
  excludes = [
    "ingestion.py",
    "processing_trigger.py",
    "venv/**"
  ]
}
