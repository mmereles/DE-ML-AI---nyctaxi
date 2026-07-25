#!/usr/bin/env bash
# One-time bootstrap, run manually from your machine (never by CI):
#   1. Create the S3 state bucket for terraform/.
#   2. Apply the GitHub OIDC role (terraform-inicial/, its own local state -
#      the CI role must never be able to manage the resources that grant it
#      access, so this never runs as part of the terraform/ CI pipeline).
#   3. Restore the Databricks token secret if it's pending deletion, and
#      import it into terraform/'s state so the next apply doesn't try to
#      recreate it (which would either fail or clobber the real token with
#      the placeholder value in main.tf).
#   4. Migrate terraform/'s local state to the S3 backend.
# Safe to re-run - each step is idempotent.
set -euo pipefail

BUCKET="nyctaxi-tfstate-98741313131"
REGION="us-east-1"
SECRET_ARN="arn:aws:secretsmanager:us-east-1:575864492282:secret:nyctaxi/databricks-token-cW0E9g"

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
echo "== 3/4: Databricks token secret: restore + import into terraform/ state =="
cd "$REPO_ROOT/terraform"
if aws secretsmanager describe-secret --secret-id "$SECRET_ARN" --query DeletedDate --output text 2>/dev/null | grep -qv '^None$'; then
  echo "Secret pending deletion, restoring..."
  aws secretsmanager restore-secret --secret-id "$SECRET_ARN"
fi

echo
echo "== 4/4: Migrate terraform/ local state to the S3 backend =="
terraform init -migrate-state

if ! terraform state list | grep -q '^aws_secretsmanager_secret\.databricks_token$'; then
  terraform import aws_secretsmanager_secret.databricks_token "$SECRET_ARN"
fi
if ! terraform state list | grep -q '^aws_secretsmanager_secret_version\.databricks_token$'; then
  VERSION_ID=$(aws secretsmanager get-secret-value --secret-id "$SECRET_ARN" --query VersionId --output text)
  terraform import aws_secretsmanager_secret_version.databricks_token "${SECRET_ARN}|${VERSION_ID}"
fi

echo
echo "Done. Load this ARN into GitHub: Settings > Secrets and variables > Actions > Secrets > AWS_ROLE_ARN"
cd "$REPO_ROOT/terraform-inicial"
terraform output github_actions_role_arn
