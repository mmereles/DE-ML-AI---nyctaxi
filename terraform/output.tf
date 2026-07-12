output "lambda_function_name" {
  description = "Name of the Lambda function"
  value       = aws_lambda_function.nyctaxi_ingestion.function_name
}

output "lambda_function_arn" {
  description = "ARN of the Lambda Function"
  value       = aws_lambda_function.nyctaxi_ingestion.arn
}

output "cloudwatch_rule_arn" {
  description = "ARN oof the CloudWatch event rule"
  value       = aws_cloudwatch_event_rule.monthly_trigger.arn
}

output "iam_role_arn" {
  description = "ARN of the IAM role for Lambda"
  value       = aws_iam_role.lambda_exec.arn
}

output "processing_lambda_function_name" {
  description = "Name of the processing trigger Lambda Function"
  value       = aws_lambda_function.nyctaxi_processing_trigger.function_name
}

output "processing_lambda_function_arn" {
  description = "ARN of the processing trigger Lambda Function"
  value       = aws_lambda_function.nyctaxi_processing_trigger.arn
}

output "databricks_secret_arn" {
  description = "ARN of the DataBricks token secret"
  value       = aws_secretsmanager_secret.databricks_token.arn
}

output "eventbridge_rule_arn" {
  description = "ARN of the EventBridge rule for S3 events"
  value       = aws_cloudwatch_event_rule.s3_taxi_data_rule.arn
}

output "databricks_processing_instance_profile_arn" {
  description = "ARN to register as an Instance Profile in the Databricks account console (Compute > Instance profiles), then attach to the job cluster running the processing notebook, so it can call cloudwatch:PutMetricData"
  value       = aws_iam_instance_profile.databricks_processing_instance_profile.arn
}