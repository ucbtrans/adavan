#!/usr/bin/env bash
# deploy_html.sh — Fast HTML-only deploy (skips SAM build and Lambda upload)
#
# Use this when only templates/index.html or addresses_pool.json changed.
# Takes ~15 seconds vs ~3 minutes for full deploy.sh.
#
# Usage:
#   bash deploy_html.sh              # prod: v1 + v2 buckets, label v2.3
#   bash deploy_html.sh --env dev    # dev:  dev bucket only, label v3.0

set -euo pipefail

ENV="prod"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --env) ENV="$2"; shift 2 ;;
    *) shift ;;
  esac
done

REGION="${AWS_DEFAULT_REGION:-us-west-2}"
V1_BUCKET="ada-driving-assistant-web-173479170210"
V1_CF_DIST="E3UTZPP7B7X64F"
V2_CF_DIST="E3KE0JK3Y50REK"
DEV_BUCKET="ada-driving-assistant-web-dev-173479170210"
DEV_CF_DIST="E2366H88WYS3HC"

# Use python3 if python is not available (WSL)
PYTHON=$(command -v python || command -v python3)

# Read the API URL from the stack being deployed. This used to always read the
# prod stack, which published a dev page wired to the prod API — and prod has
# none of the newer endpoints, so the Real van tab just 404'd.
if [[ "$ENV" == "dev" ]]; then
  STACK_NAME="ada-driving-assistant-dev"
else
  STACK_NAME="ada-driving-assistant"
fi

echo "==> Fetching API URL from CloudFormation ($STACK_NAME)..."
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

if [[ "$ENV" == "dev" ]]; then
  # Keep in step with deploy.sh, which labels dev builds the same way.
  VERSION_LABEL="v3.7"
  TARGET_BUCKETS=("$DEV_BUCKET")
  TARGET_CF_DISTS=("$DEV_CF_DIST")
  TARGET_URLS=("https://d1xjlbyixg6rri.cloudfront.net")
else
  VERSION_LABEL="v2.3"
  TARGET_BUCKETS=("$V1_BUCKET" "$WEB_BUCKET_V2")
  TARGET_CF_DISTS=("$V1_CF_DIST" "$V2_CF_DIST")
  TARGET_URLS=("https://d1v86oas7j7jis.cloudfront.net" "https://d67j8fw7h34cr.cloudfront.net")
fi

echo "==> Environment : $ENV (label: $VERSION_LABEL)"

echo "==> Preparing HTML file (injecting API URL + version label)..."

$PYTHON -c "
import re
api_url = '$API_URL'
version = '$VERSION_LABEL'
content = open('templates/index.html', encoding='utf-8').read()
content = content.replace(\"window.ADA_API_BASE || ''\", \"'\" + api_url + \"'\")
content = re.sub(r'v\d+\.\d+</span></h1>', version + '</span></h1>', content)
content = re.sub(r'ADA Driving Assistant v\d+\.\d+', 'ADA Driving Assistant ' + version, content)
open('_deploy_index.html', 'w', encoding='utf-8').write(content)
"

echo "==> Uploading to S3..."
for B in "${TARGET_BUCKETS[@]}"; do
  aws s3 cp _deploy_index.html \
    "s3://$B/index.html" \
    --content-type "text/html; charset=utf-8" \
    --cache-control "no-cache"
done

if [[ -f "addresses_pool.json" ]]; then
  echo "==> Uploading addresses_pool.json..."
  for B in "${TARGET_BUCKETS[@]}"; do
    aws s3 cp addresses_pool.json \
      "s3://$B/addresses_pool.json" \
      --content-type "application/json" \
      --cache-control "public, max-age=86400"
  done
fi

if [[ -f "traffic_signals.json" ]]; then
  echo "==> Uploading traffic_signals.json..."
  for B in "${TARGET_BUCKETS[@]}"; do
    aws s3 cp traffic_signals.json \
      "s3://$B/traffic_signals.json" \
      --content-type "application/json" \
      --cache-control "public, max-age=86400"
  done
fi

if [[ -f "stop_signs.json" ]]; then
  echo "==> Uploading stop_signs.json..."
  for B in "${TARGET_BUCKETS[@]}"; do
    aws s3 cp stop_signs.json \
      "s3://$B/stop_signs.json" \
      --content-type "application/json" \
      --cache-control "public, max-age=86400"
  done
fi

if [[ -f "van_icon2.png" ]]; then
  echo "==> Uploading van_top.png..."
  for B in "${TARGET_BUCKETS[@]}"; do
    aws s3 cp "van_icon2.png" \
      "s3://$B/van_top.png" \
      --content-type "image/png" \
      --cache-control "public, max-age=2592000"
  done
fi

rm -f _deploy_index.html

echo "==> Invalidating CloudFront cache..."
for D in "${TARGET_CF_DISTS[@]}"; do
  aws cloudfront create-invalidation --distribution-id "$D" --paths "/*" --output text --query "Invalidation.Id"
done

echo ""
echo "=========================================="
echo "  HTML deploy complete! [$ENV — $VERSION_LABEL]"
for U in "${TARGET_URLS[@]}"; do
  echo "  $U"
done
echo "=========================================="
