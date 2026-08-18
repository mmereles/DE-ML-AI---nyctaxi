# Fase 4.2 - Lambda Function URL para el agente de lenguaje natural (BYOK).
#
# No usa API Gateway a proposito: el timeout de integracion de API Gateway
# (REST y HTTP) tiene un techo duro de ~29s, no configurable. Un loop de
# agente con varias vueltas de tool-calling puede pasarse de eso facil. Una
# Function URL no tiene ese techo - el limite real pasa a ser el timeout de
# la propia Lambda (hasta 900s).
#
# La key de OpenAI es del visitante (BYOK) - viaja en el body de cada
# request, nunca vive en este Terraform ni en ninguna variable de entorno.
#
# El codigo vive en agent/backend/ (no en lambda/) a proposito: sus
# dependencias (openai, databricks-sql-connector, pandas via el connector)
# son pesadas y no tiene sentido que las otras 3 Lambdas las arrastren sin
# usarlas - carpeta propia, requirements.txt propio, zip propio.

resource "aws_lambda_function" "ask_agent" {
  function_name = "nyctaxi-ask-agent"
  description   = "Agente de lenguaje natural (BYOK) sobre los datos del pipeline - Fase 4.2"
  role          = aws_iam_role.ask_agent_exec.arn
  handler       = "ask_agent.lambda_handler"
  runtime       = "python3.13"
  timeout       = 120
  memory_size   = 512

  # Cota dura de concurrencia: protege el costo de Lambda/Databricks del
  # lado del dueño (la key de OpenAI la trae el visitante, pero el warehouse
  # de Databricks y el computo de Lambda siguen siendo a su cargo).
  reserved_concurrent_executions = 5

  # Via S3, no "filename" directo: el zip pesa ~60MB (pandas/numpy entran
  # como dependencia transitiva de databricks-sql-connector, aunque run_sql
  # no los usa - trabaja con el cursor crudo). Terraform manda el zip
  # codificado en base64 en el request de UpdateFunctionCode cuando se sube
  # directo, lo que infla el tamaño efectivo por encima del limite de la API
  # (70.167.211 bytes) aunque el archivo en si este por debajo. Subiendolo a
  # S3 primero evita ese limite.
  s3_bucket        = aws_s3_bucket.nyc_taxi_bucket.id
  s3_key           = aws_s3_object.ask_agent_zip.key
  source_code_hash = data.archive_file.ask_agent_zip.output_base64sha256

  environment {
    variables = {
      DATABRICKS_HOST             = var.databricks_host
      DATABRICKS_SQL_WAREHOUSE_ID = var.databricks_sql_warehouse_id
      DATABRICKS_SECRET_ARN       = data.aws_secretsmanager_secret.databricks_token.arn
      QUOTE_API_URL               = "${aws_apigatewayv2_api.quote_api.api_endpoint}/quote"
    }
  }

  tags = {
    Project     = "nyctaxi-ml-pipeline"
    Environment = "production"
  }
}

resource "aws_lambda_function_url" "ask_agent" {
  function_name      = aws_lambda_function.ask_agent.function_name
  authorization_type = "NONE" # publico a proposito: la key de OpenAI la trae el visitante

  cors {
    allow_origins = ["*"] # acotar al dominio real del frontend (GitHub Pages) una vez desplegado
    allow_methods = ["POST"]
    allow_headers = ["content-type"]
    max_age       = 300
  }
}

# AWS ya crea automaticamente (fuera de este Terraform, al setear
# authorization_type = "NONE" arriba) un resource-based policy statement
# para lambda:InvokeFunctionUrl - confirmado: declararlo de nuevo tira
# ResourceConflictException, el statement id ya existe.
#
# Lo que SI faltaba, y era el bloqueante real del 403 (confirmado en el
# aviso de la consola de AWS, "missing permissions required for public
# access"): un permiso separado para la accion lambda:InvokeFunction
# (distinta de InvokeFunctionUrl) - sin este, cualquier invocacion no
# autenticada devuelve 403 AccessDeniedException aunque el auth type ya
# diga NONE.
resource "aws_lambda_permission" "ask_agent_invoke_public" {
  statement_id  = "AllowPublicInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ask_agent.function_name
  principal     = "*"
}

# Rol de la Lambda: solo logs + leer el secret de Databricks (mismo secret
# que ya usa processing_trigger). Nada de SageMaker ni S3 - get_fare_quote
# llama a la API publica por HTTPS, no necesita permisos de AWS.
resource "aws_iam_role" "ask_agent_exec" {
  name = "nyctaxi-ask-agent-role"

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

resource "aws_iam_role_policy" "ask_agent_policy" {
  name = "nyctaxi-ask-agent-policy"
  role = aws_iam_role.ask_agent_exec.id

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
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = data.aws_secretsmanager_secret.databricks_token.arn
      }
    ]
  })
}

resource "aws_cloudwatch_log_group" "ask_agent_logs" {
  name              = "/aws/lambda/${aws_lambda_function.ask_agent.function_name}"
  retention_in_days = 14

  tags = {
    Project = "nyctaxi-ml-pipeline"
  }
}

# Sube el zip al mismo bucket que ya usa el resto del proyecto, bajo un
# prefix propio - Lambda lee el codigo desde ahi (ver nota en el recurso
# aws_lambda_function de arriba, sobre por que no via "filename" directo).
resource "aws_s3_object" "ask_agent_zip" {
  bucket = aws_s3_bucket.nyc_taxi_bucket.id
  key    = "lambda-deployments/ask_agent.zip"
  source = data.archive_file.ask_agent_zip.output_path
  etag   = data.archive_file.ask_agent_zip.output_md5
}

# Zip con el contenido de agent/backend/ (carpeta propia, ver nota arriba) -
# excluye la version CLI (no es el handler de esta Lambda) y __pycache__.
data "archive_file" "ask_agent_zip" {
  type        = "zip"
  source_dir  = "${path.module}/../agent/backend"
  output_path = "${path.module}/../ask_agent_function.zip"
  excludes = [
    "nyctaxi_assistant.py",
    "__pycache__/**",
  ]
}

output "ask_agent_url" {
  description = "URL publica de la Lambda Function URL del agente (BYOK, sin el limite de 29s de API Gateway)"
  value       = aws_lambda_function_url.ask_agent.function_url
}
