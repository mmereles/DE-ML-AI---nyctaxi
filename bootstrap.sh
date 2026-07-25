#!/usr/bin/env bash
# One-time bootstrap, run manually from your machine (never by CI):
#   1. Create the S3 state bucket for terraform/.
#   2. Apply the GitHub OIDC role (terraform-inicial/, its own local state -
#      the CI role must never be able to manage the resources that grant it
#      access, so this never runs as part of the terraform/ CI pipeline).
#   3. Ensure the Databricks token secret exists (create it with a
#      placeholder on a fresh account, or restore it if it's pending
#      deletion). terraform/main.tf only ever *reads* this secret (a `data`
#      source, not a `resource`) - it's bootstrap's job to create it, so
#      repeated applies (local + CI) never fight over owning it.
#   4. Migrate terraform/'s local state to the S3 backend.
# Safe to re-run - each step is idempotent.
set -euo pipefail

BUCKET="nyctaxi-tfstate-98741313131"
REGION="us-east-1"
SECRET_NAME="nyctaxi/databricks-token"

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"

echo "== 1/4: S3 bucket for remote state =="
if aws s3api head-bucket --bucket "$BUCKET" --region "$REGION" 2>/dev/null; then
  echo "Bucket $BUCKET already exists, skipping creation."
else
  aws s3api create-bucket --bucket "$BUCKET" --region "$REGION"
  aws s3api put-bucket-versioning \
    --bucket "$BUCKET" --versioning-configuration Status=Enabled
  aws s3api put-public-access-block \
    --bucket "$BUCKET" \
    --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
  echo "Bucket $BUCKET created."
fi

echo
echo "== 2/4: Apply GitHub OIDC role (terraform-inicial/, local state) =="
cd "$REPO_ROOT/terraform-inicial"
terraform init
terraform apply

echo
echo "== 3/4: Ensure Databricks token secret exists =="
if aws secretsmanager describe-secret --secret-id "$SECRET_NAME" >/dev/null 2>&1; then
  DELETED=$(aws secretsmanager describe-secret --secret-id "$SECRET_NAME" --query DeletedDate --output text 2>/dev/null)
  if [ "$DELETED" != "None" ]; then
    echo "Secret pending deletion, restoring..."
    aws secretsmanager restore-secret --secret-id "$SECRET_NAME"
  else
    echo "Secret already exists, skipping creation."
  fi
else
  echo "Secret doesn't exist yet - creating with a placeholder value."
  aws secretsmanager create-secret \
    --name "$SECRET_NAME" \
    --description "Databricks personal access token for NYC taxi processing" \
    --secret-string '{"token":"PLACEHOLDER_TOKEN"}'
  echo "IMPORTANT: replace the placeholder with your real Databricks token:"
  echo "  aws secretsmanager put-secret-value --secret-id $SECRET_NAME --secret-string '{\"token\":\"<your-real-token>\"}'"
fi

echo
echo "== 4/4: Migrate terraform/ local state to the S3 backend =="
cd "$REPO_ROOT/terraform"
terraform init -migrate-state

echo
echo "Done. Load this ARN into GitHub: Settings > Secrets and variables > Actions > Secrets > AWS_ROLE_ARN"
cd "$REPO_ROOT/terraform-inicial"
terraform output github_actions_role_arn
