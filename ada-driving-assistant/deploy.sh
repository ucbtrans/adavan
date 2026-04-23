#!/usr/bin/env bash
# deploy.sh — Build and deploy ADA Driving Assistant to AWS
#
# Usage:
#   ./deploy.sh              # deploy prod
#   ./deploy.sh --env dev    # deploy dev stack
#   ./deploy.sh --guided     # first-time prod deploy (prompts for parameters)
#
# Prerequisites:
#   - AWS CLI configured (aws configure)
#   - AWS SAM CLI installed

set -euo pipefail

REGION="${AWS_DEFAULT_REGION:-us-west-2}"

# ── Parse arguments ────────────────────────────────────────────────────────
ENV="prod"
GUIDED=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --env)   ENV="$2"; shift 2 ;;
    --guided) GUIDED=true; shift ;;
    *) shift ;;
  esac
done

if [[ "$ENV" == "dev" ]]; then
  STACK_NAME="ada-driving-assistant-dev"
  SAM_CONFIG="samconfig.dev.toml"
  API_FUNCTION="ada-api-dev"
  SIM_FUNCTION="ada-simulation-dev"
else
  STACK_NAME="ada-driving-assistant"
  SAM_CONFIG="samconfig.toml"
  API_FUNCTION="ada-api"
  SIM_FUNCTION="ada-simulation"
fi

echo "==> Environment : $ENV  (stack: $STACK_NAME)"

# v1 prod bucket — only updated on prod deploys
V1_BUCKET="ada-driving-assistant-web-173479170210"
V1_CF_DIST="E3UTZPP7B7X64F"
V1_CF_URL="https://d1v86oas7j7jis.cloudfront.net"

# ── Locate SAM CLI ─────────────────────────────────────────────────────────
SAM=""
if command -v sam &>/dev/null; then
  SAM="sam"
elif command -v cmd.exe &>/dev/null; then
  SAM="cmd.exe /c sam"
else
  echo "ERROR: sam CLI not found."
  exit 1
fi
echo "    Using SAM: $SAM"
# shellcheck disable=SC2206
SAM_CMD=($SAM)

echo "==> Building Lambda package..."
"${SAM_CMD[@]}" build

echo "==> Deploying stack: $STACK_NAME"
if [[ "$GUIDED" == "true" ]] || [[ ! -f "$SAM_CONFIG" ]]; then
  "${SAM_CMD[@]}" deploy \
    --config-file "$SAM_CONFIG" \
    --stack-name "$STACK_NAME" \
    --region "$REGION" \
    --capabilities CAPABILITY_IAM \
    --guided
else
  "${SAM_CMD[@]}" deploy \
    --config-file "$SAM_CONFIG" \
    --stack-name "$STACK_NAME" \
    --region "$REGION" \
    --capabilities CAPABILITY_IAM \
    --no-fail-on-empty-changeset
fi

echo "==> Fetching stack outputs..."
API_URL=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" \
  --output text)

WEB_BUCKET_V2=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='WebBucketNameV2'].OutputValue" \
  --output text)

echo "    API URL    : $API_URL"
echo "    Web bucket : $WEB_BUCKET_V2"

echo "==> Syncing source files into SAM build directory and rebuilding lambda.zip..."
SAM_BUILD_API=".aws-sam/build/ApiFunction"
SAM_BUILD_SIM=".aws-sam/build/SimulationFunction"
for f in app.py assistant.py detections_adapter.py events.py fetch_streets.py \
          lambda_function.py location.py objects.py parking.py \
          sessions.py simulator.py; do
  [[ -f "$f" ]] && cp "$f" "$SAM_BUILD_API/$f"
  [[ -f "$f" ]] && cp "$f" "$SAM_BUILD_SIM/$f"
done
(cd "$SAM_BUILD_API" && python -m zipfile -c ../../lambda.zip .) 2>/dev/null || \
(cd "$SAM_BUILD_API" && python3 -m zipfile -c ../../lambda.zip .) || \
(cd "$SAM_BUILD_API" && py -m zipfile -c ../../lambda.zip .)
echo "    lambda.zip: $(ls -lh lambda.zip | awk '{print $5}')"

# Package is >70MB — upload via S3 first
SAM_BUCKET=$(aws cloudformation describe-stack-resource \
  --stack-name "aws-sam-cli-managed-default" \
  --logical-resource-id "SamCliSourceBucket" \
  --region "$REGION" \
  --query "StackResourceDetail.PhysicalResourceId" \
  --output text 2>/dev/null || echo "")

if [[ -n "$SAM_BUCKET" ]]; then
  S3_KEY="manual-deploy/lambda-$(date +%s).zip"
  echo "==> Uploading lambda.zip to s3://$SAM_BUCKET/$S3_KEY..."
  aws s3 cp lambda.zip "s3://$SAM_BUCKET/$S3_KEY" --region "$REGION"

  echo "==> Updating Lambda code from S3..."
  aws lambda update-function-code \
    --function-name "$API_FUNCTION" \
    --s3-bucket "$SAM_BUCKET" --s3-key "$S3_KEY" \
    --region "$REGION" \
    --query "[FunctionName, LastModified]" \
    --output text

  aws lambda update-function-code \
    --function-name "$SIM_FUNCTION" \
    --s3-bucket "$SAM_BUCKET" --s3-key "$S3_KEY" \
    --region "$REGION" \
    --query "[FunctionName, LastModified]" \
    --output text
else
  echo "    WARNING: SAM bucket not found — skipping direct Lambda update"
fi

echo "==> Preparing static files for S3..."
PYTHON=$(command -v python || command -v python3 || command -v py)

$PYTHON -c "
api_url = '$API_URL'
content = open('templates/index.html', encoding='utf-8').read()
content = content.replace(\"window.ADA_API_BASE || ''\", \"'\" + api_url + \"'\")
open('_deploy_index.html', 'w', encoding='utf-8').write(content)
"

echo "==> Uploading static files to s3://$WEB_BUCKET_V2..."
aws s3 cp _deploy_index.html \
  "s3://$WEB_BUCKET_V2/index.html" \
  --content-type "text/html; charset=utf-8" \
  --cache-control "no-cache"
aws s3 cp static/ada_logo.jpg \
  "s3://$WEB_BUCKET_V2/ada_logo.jpg" \
  --content-type "image/jpeg"

if [[ -f "addresses_pool.json" ]]; then
  echo "==> Uploading addresses_pool.json to s3://$WEB_BUCKET_V2..."
  aws s3 cp addresses_pool.json \
    "s3://$WEB_BUCKET_V2/addresses_pool.json" \
    --content-type "application/json" \
    --cache-control "public, max-age=86400"
fi

# v1 prod bucket — only on prod deploys
if [[ "$ENV" == "prod" ]]; then
  echo "==> Uploading static files to s3://$V1_BUCKET (v1)..."
  aws s3 cp _deploy_index.html \
    "s3://$V1_BUCKET/index.html" \
    --content-type "text/html; charset=utf-8" \
    --cache-control "no-cache"
  aws s3 cp static/ada_logo.jpg \
    "s3://$V1_BUCKET/ada_logo.jpg" \
    --content-type "image/jpeg"
  if [[ -f "addresses_pool.json" ]]; then
    aws s3 cp addresses_pool.json \
      "s3://$V1_BUCKET/addresses_pool.json" \
      --content-type "application/json" \
      --cache-control "public, max-age=86400"
  fi

  echo "==> Invalidating CloudFront cache (v1)..."
  aws cloudfront create-invalidation --distribution-id "$V1_CF_DIST" --paths "/*"
fi

rm -f _deploy_index.html

echo "==> Invalidating CloudFront cache..."
CF_DIST=$(aws cloudfront list-distributions \
  --query "DistributionList.Items[?Origins.Items[0].DomainName==\`${WEB_BUCKET_V2}.s3-website-${REGION}.amazonaws.com\`].Id" \
  --output text)
if [[ -n "$CF_DIST" ]]; then
  aws cloudfront create-invalidation --distribution-id "$CF_DIST" --paths "/*"
  echo "    Invalidation created for $CF_DIST"
else
  echo "    (CloudFront distribution not found — skipping)"
fi

CF_URL=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='CloudFrontUrlV2'].OutputValue" \
  --output text 2>/dev/null || echo "")

echo ""
echo "=========================================="
echo "  Deploy complete!  [$ENV]"
[[ "$ENV" == "prod" ]] && echo "  CloudFront v1 : $V1_CF_URL"
echo "  CloudFront    : $CF_URL"
echo "  API           : $API_URL"
echo "=========================================="
