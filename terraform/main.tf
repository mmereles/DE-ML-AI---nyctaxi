# === DATA INGESTION RESOURCES ===

# Bucket
resource "aws_s3_bucket" "nyc_taxi_bucket" {
  bucket = "nyc-taxi-bucket-12313"
}

# lambda function for data ingestion
resource "aws_lambda_function" "nyctaxi_ingestion" {
  function_name = "nyctaxi-data-ingestion"
  description   = "Downloads NYC taxi data monthly and saves to s3"
  role          = aws_iam_role.lambda_exec.arn
  handler       = "ingestion.lambda_handler"
  runtime       = "python3.13"
  timeout       = 900
  memory_size   = 256

  filename         = data.archive_file.ingestion_lambda_zip.output_path
  source_code_hash = data.archive_file.ingestion_lambda_zip.output_base64sha256

  environment {
    variables = {
      S3_BUCKET = aws_s3_bucket.nyc_taxi_bucket.id
      S3_PREFIX = "nyctaxi/raw/"
    }
  }

  tags = {
    Project     = "nyctaxi-ml-pipeline"
    Environment = "production"
  }
}

# IAM role for Lambda
resource "aws_iam_role" "lambda_exec" {
  name = "nyctaxi-ingestion-lambda-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
      }
    ]
  })

  tags = {
    Project = "nyctaxi-ml-pipeline"
  }
}

resource "aws_iam_role_policy" "lambda_s3_policy" {
  name = "nyctaxi-ingestion-s3-policy"
  role = aws_iam_role.lambda_exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:PutObject",
          "s3:PutObjectAcl",
          "s3:GetObject",
          "s3:ListBucket"
        ]
        Resource = [
          "arn:aws:s3:::${aws_s3_bucket.nyc_taxi_bucket.id}",
          "arn:aws:s3:::${aws_s3_bucket.nyc_taxi_bucket.id}/*"
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "arn:aws:logs:*:*:*"
      },
      {
        Effect = "Allow"
        Action = [
          "cloudwatch:PutMetricData"
        ]
        Resource = "*"
      }
    ]
  })
}


# CloudWatch Event Rule
resource "aws_cloudwatch_event_rule" "monthly_trigger" {
  name                = "nyctaxi-monthly-ingestion"
  description         = "Triggers NYC taxi data ingestion on the 1st each month"
  schedule_expression = "cron(0 8 5 * ? *)"

  tags = {
    Project = "nyctaxi-ml-pipeline"
  }
}

# cloudwatch Event Target
# event target is the lambda function, event rule is the schedule
resource "aws_cloudwatch_event_target" "lambda_target" {
  rule      = aws_cloudwatch_event_rule.monthly_trigger.name
  target_id = "nyctaxi-lambda-target"
  arn       = aws_lambda_function.nyctaxi_ingestion.arn # refer to the lambda function
}

# lambda Permission for cloudwatch
resource "aws_lambda_permission" "allow_cloudwatch" {
  statement_id  = "AllowExecutionFromCloudwatch"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.nyctaxi_ingestion.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.monthly_trigger.arn
}

# cloudwatch Log group
resource "aws_cloudwatch_log_group" "lambda_log_group" {
  name              = "/aws/lambda/${aws_lambda_function.nyctaxi_ingestion.function_name}"
  retention_in_days = 7

  tags = {
    Project = "nyctaxi-ml-pipeline"
  }
}

# cloudWatch Alarm for lambda Errors
# Alarm if errorsc> 0 for 1 datapoints within 1 day
resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  alarm_name          = "nyctaxi-ingestion-lambda-errors"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 86400
  statistic           = "Sum"
  threshold           = 0
  alarm_description   = "this alarm triggers if lambda fnction has no errors"

  dimensions = {
    FunctionName = aws_lambda_function.nyctaxi_ingestion.function_name
  }

  alarm_actions = []

  tags = {
    Project = "nyctaxi-ml-pipeline"
  }
}

# Zip the lambda  folder and create lambda_fnction.zip
data "archive_file" "ingestion_lambda_zip" {
  type        = "zip"
  source_dir  = "${path.module}/../lambda"
  output_path = "${path.module}/..ingestion_lambda_function.zip"
  excludes    = [
    "processing_trigger.py",
    "venv/**"
  ]
}

# ==== DATA PROCESSING RESOURCES =====

# Databricks token secret - created by bootstrap.sh (one-time, before this
# ever runs), not by this Terraform. Read-only reference on purpose: this
# config runs repeatedly (local + CI), and a resource block here would try
# to (re)create the secret on every apply, colliding with the one bootstrap
# already made in AWS.
data "aws_secretsmanager_secret" "databricks_token" {
  name = "nyctaxi/databricks-token"
}

# Lambda function for data processing trigger
resource "aws_lambda_function" "nyctaxi_processing_trigger" {
  function_name = "nyctaxi-processing-trigger"
  description   = "Triggers databricks processing when new taxi data arrives"
  role          = aws_iam_role.processing_lambda_exec.arn
  handler       = "processing_trigger.lambda_handler"
  runtime       = "python3.13"
  timeout       = 60
  memory_size   = 256

  filename         = data.archive_file.processing_lambda_zip.output_path
  source_code_hash = data.archive_file.processing_lambda_zip.output_base64sha256

  environment {
    variables = {
      DATABRICKS_HOST       = var.databricks_host
      DATABRICKS_JOB_ID     = databricks_job.processing.id 
      DATABRICKS_SECRET_ARN = data.aws_secretsmanager_secret.databricks_token.arn
    }
  }

  tags = {
    Project     = "nyctaxi-ml-pipeline"
    Environment = "production"
  }
}

# IAM role for processing Lambda
resource "aws_iam_role" "processing_lambda_exec" {
  name = "nyctaxi-processing-lambda-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
      }
    ]
  })

  tags = {
    Project = "nyctaxi-ml-pipeline"
  }
}

# IAM policy for processing Lambda: create logs, put metrics, s3 read, secretsmanager
resource "aws_iam_role_policy" "processing_lambda_policy" {
  name = "nyctaxi-processing-lambda-policy"
  role = aws_iam_role.processing_lambda_exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "arn:aws:logs:*:*:*"
      },
      {
        Effect = "Allow"
        Action = [
          "cloudwatch:PutMetricData"
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject"
        ]
        Resource = [
          "arn:aws:s3:::${aws_s3_bucket.nyc_taxi_bucket.id}/*"
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue"
        ]
        Resource = data.aws_secretsmanager_secret.databricks_token.arn
      }
    ]
  })
}

# Enable S3 EventBridge notifications for the bucket
resource "aws_s3_bucket_notification" "taxi_data_notification" {
  bucket      = aws_s3_bucket.nyc_taxi_bucket.id
  eventbridge = true
}

# EventBridge rule for S3 object creation in the raw/ folder
# this rule triggers when a new object is created in the specifies S3 bucker and prefix
resource "aws_cloudwatch_event_rule" "s3_taxi_data_rule" {
  name        = "nyctaxi-s3-processing-trigger"
  description = "Triggers processing when new NYC taxi data arrives in S3"

  event_pattern = jsonencode({
    source      = ["aws.s3"]
    detail-type = ["Object Created"]
    detail = {
      bucket = {
        name = [aws_s3_bucket.nyc_taxi_bucket.id]
      }
      object = {
        key = [
          {
            prefix = "nyctaxi/raw/"
          }
        ]
      }
    }
  })

  tags = {
    Project = "nyctaxi-ml-pipeline"
  }
}

# EventBridge target for processing Lambda
# target is the lambda function, rule is the event pattern above "Object created" in an S3 bucket
resource "aws_cloudwatch_event_target" "processing_lambda_target" {
  rule      = aws_cloudwatch_event_rule.s3_taxi_data_rule.name
  target_id = "nytaxi-processing-target"
  arn       = aws_lambda_function.nyctaxi_processing_trigger.arn
}

# Lambda Permission for Eventbridge
# Allow EventBridge to invoke the processing lambda
resource "aws_lambda_permission" "allow_eventbridge" {
  statement_id  = "AllowExecutionFromEventBridge"
  action        = "lambda:Invokefunction"
  function_name = aws_lambda_function.nyctaxi_processing_trigger.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.s3_taxi_data_rule.arn
}

# CloudWatch Log Group for processing Lambda
resource "aws_cloudwatch_log_group" "processing_lambda_log_group" {
  name              = "/aws/lambda/${aws_lambda_function.nyctaxi_processing_trigger.function_name}"
  retention_in_days = 14

  tags = {
    Project = "nyctaxi-ml-pipeline"
  }
}

# CloudWatch Alarms for processing pipeline
# Alarm if Errors > 0 for 1 datapoints within 5 minutes
resource "aws_cloudwatch_metric_alarm" "processing_lambda_errors" {
  alarm_name          = "nyctaxi-processing-lambda-errors"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  alarm_description   = "This alarm triggers if processing Lambda function has errors"

  dimensions = {
    FunctionName = aws_lambda_function.nyctaxi_processing_trigger.function_name
  }

  alarm_actions = []

  tags = {
    Project = "nyctaxi-ml-pipeline"
  }
}

# Zip the lambda/ folder for processing trigger
data "archive_file" "processing_lambda_zip" {
  type        = "zip"
  source_dir  = "${path.module}/../lambda"
  output_path = "${path.module}/../processing_lambda_function.zip"
  excludes    = [
    "ingestion.py",
    "venv/**"
  ]
}

# === DATABRICKS CLUSTER -> CLOUDWATCH METRICS ===
# The processing notebook ("nyctaxi - processing.py") now calls
# boto3 cloudwatch.put_metric_data(...) directly to report business/data-quality
# metrics (records loaded, records removed, data quality %, validation checks,
# records written, run success). For those calls to succeed, the Databricks job
# cluster needs AWS credentials - on AWS, Databricks clusters get credentials via
# an "instance profile" attached to the cluster (same mechanism as a normal EC2
# instance profile).


resource "aws_iam_role" "databricks_processing_instance_role" {
  name = "nyctaxi-databricks-processing-role"

  # Instance profiles are assumed by the EC2 instance itself, not by Databricks
  # directly - Databricks just tells EC2 which profile to launch the instance with.
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "ec2.amazonaws.com"
        }
      }
    ]
  })

  tags = {
    Project = "nyctaxi-ml-pipeline"
  }
}

resource "aws_iam_instance_profile" "databricks_processing_instance_profile" {
  name = "nyctaxi-databricks-processing-profile"
  role = aws_iam_role.databricks_processing_instance_role.name
}

# Scoped to exactly what the notebook's send_metric() helper calls - nothing else.
resource "aws_iam_role_policy" "databricks_processing_cloudwatch_policy" {
  name = "nyctaxi-databricks-cloudwatch-metrics-policy"
  role = aws_iam_role.databricks_processing_instance_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "cloudwatch:PutMetricData"
        ]
        Resource = "*"
      }
    ]
  })
}