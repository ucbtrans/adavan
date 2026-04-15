#!/usr/bin/env bash
# deploy.sh — Build and deploy ADA Driving Assistant to AWS
#
# Prerequisites:
#   - AWS CLI configured (aws configure)
#   - AWS SAM CLI installed (https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)
#
# First run:  ./deploy.sh --guided   (prompts for parameters, saves samconfig.toml)
# Subsequent: ./deploy.sh
#
# v1 bucket (ada-driving-assistant-web-<acct>) and CloudFront (d1v86oas7j7jis.cloudfront.net)
# are preserved outside this stack. This script only manages the v2 bucket and CloudFront.

set -euo pipefail

STACK_NAME="ada-driving-assistant"
REGION="${AWS_DEFAULT_REGION:-us-west-2}"
SAM_CONFIG="samconfig.toml"

# v1 — already deployed, managed outside this stack
V1_BUCKET="ada-driving-assistant-web-173479170210"
V1_CF_DIST="E3UTZPP7B7X64F"
V1_CF_URL="https://d1v86oas7j7jis.cloudfront.net"

# Locate sam — works in Git Bash, WSL, and native Linux/Mac
SAM=""
if command -v sam &>/dev/null; then
  SAM="sam"
elif command -v cmd.exe &>/dev/null; then
  # WSL: delegate to Windows cmd.exe which has SAM in its PATH
  SAM="cmd.exe /c sam"
else
  echo "ERROR: sam CLI not found. Install from https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html"
  exit 1
fi
echo "    Using SAM: $SAM"
# shellcheck disable=SC2206
SAM_CMD=($SAM)   # split into array so it works whether SAM is "sam" or "cmd.exe /c sam"

echo "==> Building Lambda package..."
"${SAM_CMD[@]}" build

echo "==> Deploying stack: $STACK_NAME"
if [[ "${1:-}" == "--guided" ]] || [[ ! -f "$SAM_CONFIG" ]]; then
  "${SAM_CMD[@]}" deploy \
    --stack-name "$STACK_NAME" \
    --region "$REGION" \
    --capabilities CAPABILITY_IAM \
    --guided
else
  "${SAM_CMD[@]}" deploy \
    --stack-name "$STACK_NAME" \
    --region "$REGION" \
    --capabilities CAPABILITY_IAM
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

echo "    API URL       : $API_URL"
echo "    Web bucket v2 : $WEB_BUCKET_V2"

echo "==> Syncing source files into SAM build directory and rebuilding lambda.zip..."
SAM_BUILD_API=".aws-sam/build/ApiFunction"
SAM_BUILD_SIM=".aws-sam/build/SimulationFunction"
for f in app.py assistant.py detections_adapter.py events.py fetch_streets.py \
          lambda_function.py location.py objects.py parking.py \
          sessions.py simulator.py; do
  [[ -f "$f" ]] && cp "$f" "$SAM_BUILD_API/$f"
  [[ -f "$f" ]] && cp "$f" "$SAM_BUILD_SIM/$f"
done
# zip from INSIDE SAM build dir so files land at root (not path/app.py)
(cd "$SAM_BUILD_API" && python -m zipfile -c ../../lambda.zip .) 2>/dev/null || \
(cd "$SAM_BUILD_API" && python3 -m zipfile -c ../../lambda.zip .) || \
(cd "$SAM_BUILD_API" && py -m zipfile -c ../../lambda.zip .)
echo "    lambda.zip: $(ls -lh lambda.zip | awk '{print $5}')"

# Package is >70MB so must upload via S3 before updating Lambda
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
    --function-name ada-api \
    --s3-bucket "$SAM_BUCKET" --s3-key "$S3_KEY" \
    --region "$REGION" \
    --query "[FunctionName, LastModified]" \
    --output text

  aws lambda update-function-code \
    --function-name ada-simulation \
    --s3-bucket "$SAM_BUCKET" --s3-key "$S3_KEY" \
    --region "$REGION" \
    --query "[FunctionName, LastModified]" \
    --output text
else
  echo "    WARNING: SAM bucket not found — skipping direct Lambda update (SAM deploy already handled it)"
fi

echo "==> Preparing static files for S3..."

# Use python3 if python is not available (WSL)
PYTHON=$(command -v python || command -v python3)

# v1: inject API_BASE only (no version label — preserve existing look)
$PYTHON -c "
api_url = '$API_URL'
content = open('templates/index.html', encoding='utf-8').read()
content = content.replace(\"window.ADA_API_BASE || ''\", \"'\" + api_url + \"'\")
open('_deploy_index.html', 'w', encoding='utf-8').write(content)
"

# v2: inject API URL (version label is baked into index.html directly)
$PYTHON -c "
api_url = '$API_URL'
content = open('templates/index.html', encoding='utf-8').read()
content = content.replace(\"window.ADA_API_BASE || ''\", \"'\" + api_url + \"'\")
open('_deploy_index_v2.html', 'w', encoding='utf-8').write(content)
"

echo "==> Uploading static files to s3://$V1_BUCKET (v1)..."
aws s3 cp _deploy_index.html \
  "s3://$V1_BUCKET/index.html" \
  --content-type "text/html; charset=utf-8" \
  --cache-control "no-cache"

aws s3 cp static/ada_logo.jpg \
  "s3://$V1_BUCKET/ada_logo.jpg" \
  --content-type "image/jpeg"

echo "==> Uploading static files to s3://$WEB_BUCKET_V2 (v2)..."
aws s3 cp _deploy_index_v2.html \
  "s3://$WEB_BUCKET_V2/index.html" \
  --content-type "text/html; charset=utf-8" \
  --cache-control "no-cache"

aws s3 cp static/ada_logo.jpg \
  "s3://$WEB_BUCKET_V2/ada_logo.jpg" \
  --content-type "image/jpeg"

if [[ -f "addresses_pool.json" ]]; then
  echo "==> Uploading addresses_pool.json to S3..."
  aws s3 cp addresses_pool.json \
    "s3://$V1_BUCKET/addresses_pool.json" \
    --content-type "application/json" \
    --cache-control "public, max-age=86400"
  aws s3 cp addresses_pool.json \
    "s3://$WEB_BUCKET_V2/addresses_pool.json" \
    --content-type "application/json" \
    --cache-control "public, max-age=86400"
else
  echo "    (addresses_pool.json not found locally — skipping)"
fi

rm -f _deploy_index.html _deploy_index_v2.html

echo "==> Invalidating CloudFront cache (v1)..."
aws cloudfront create-invalidation --distribution-id "$V1_CF_DIST" --paths "/*"

echo "==> Invalidating CloudFront cache (v2)..."
CF_DIST_V2=$(aws cloudfront list-distributions \
  --query "DistributionList.Items[?Origins.Items[0].DomainName==\`${WEB_BUCKET_V2}.s3-website-${REGION}.amazonaws.com\`].Id" \
  --output text)
if [[ -n "$CF_DIST_V2" ]]; then
  aws cloudfront create-invalidation --distribution-id "$CF_DIST_V2" --paths "/*"
  echo "    Invalidation created for $CF_DIST_V2"
else
  echo "    (v2 CloudFront not found — skipping)"
fi

CF_URL_V2=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='CloudFrontUrlV2'].OutputValue" \
  --output text 2>/dev/null || echo "")

echo ""
echo "=========================================="
echo "  Deploy complete!"
echo "  CloudFront v1 : $V1_CF_URL"
echo "  CloudFront v2 : $CF_URL_V2"
echo "  API           : $API_URL"
echo "=========================================="
