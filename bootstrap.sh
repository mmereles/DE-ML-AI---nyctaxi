#!/usr/bin/env bash
# One-time bootstrap: creates the S3 state bucket, migrates local state to
# it, and applies the GitHub OIDC role. Safe to re-run - each step is
# idempotent (bucket creation is skipped if it exists, and `apply -target`
# just reports "no changes" if the OIDC resources are already there).
set -euo pipefail

BUCKET="nyctaxi-tfstate-98741313131"
REGION="us-east-1"

cd "$(dirname "$0")"
cd terraform

echo "== 1/3: S3 bucket for remote state =="
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
echo "== 2/3: Migrate local state to the S3 backend =="
terraform init -migrate-state

echo
echo "== 3/3: Apply GitHub OIDC role =="
terraform apply \
  -target=aws_iam_openid_connect_provider.github_actions \
  -target=aws_iam_role.github_actions \
  -target=aws_iam_role_policy_attachment.github_actions_poweruser \
  -target=aws_iam_role_policy.github_actions_iam_scoped

echo
echo "Done. Load this ARN into GitHub: Settings > Secrets and variables > Actions > Variables > AWS_ROLE_ARN"
terraform output github_actions_role_arn
