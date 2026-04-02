#!/usr/bin/env bash
# deploy_html.sh — Fast HTML-only deploy (skips SAM build and Lambda upload)
#
# Use this when only templates/index.html or addresses_pool.json changed.
# Takes ~15 seconds vs ~3 minutes for full deploy.sh.
#
# Usage:
#   bash deploy_html.sh

set -euo pipefail

REGION="${AWS_DEFAULT_REGION:-us-west-2}"
V1_BUCKET="ada-driving-assistant-web-173479170210"
V1_CF_DIST="E3UTZPP7B7X64F"
V2_CF_DIST="E3KE0JK3Y50REK"

# Use python3 if python is not available (WSL)
PYTHON=$(command -v python || command -v python3)

echo "==> Fetching API URL from CloudFormation..."
API_URL=$(aws cloudformation describe-stacks \
  --stack-name ada-driving-assistant \
  --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" \
  --output text)

WEB_BUCKET_V2=$(aws cloudformation describe-stacks \
  --stack-name ada-driving-assistant \
  --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='WebBucketNameV2'].OutputValue" \
  --output text)

echo "    API URL       : $API_URL"
echo "    Web bucket v2 : $WEB_BUCKET_V2"

echo "==> Preparing HTML files..."

# v1: inject API URL only
$PYTHON -c "
api_url = '$API_URL'
content = open('templates/index.html', encoding='utf-8').read()
content = content.replace(\"window.ADA_API_BASE || ''\", \"'\" + api_url + \"'\")
open('_deploy_index.html', 'w', encoding='utf-8').write(content)
"

# v2: inject API URL + v2.1 label
$PYTHON -c "
api_url = '$API_URL'
content = open('templates/index.html', encoding='utf-8').read()
content = content.replace(\"window.ADA_API_BASE || ''\", \"'\" + api_url + \"'\")
content = content.replace('<title>ADA Driving Assistant</title>', '<title>ADA Driving Assistant v2.1</title>')
content = content.replace('<h1>ADA <span>Driving Assistant</span></h1>', '<h1>ADA <span>Driving Assistant</span> <span style=\"font-size:0.7rem;color:var(--muted);font-weight:400;\">v2.1</span></h1>')
open('_deploy_index_v2.html', 'w', encoding='utf-8').write(content)
"

echo "==> Uploading to S3..."
aws s3 cp _deploy_index.html \
  "s3://$V1_BUCKET/index.html" \
  --content-type "text/html; charset=utf-8" \
  --cache-control "no-cache"

aws s3 cp _deploy_index_v2.html \
  "s3://$WEB_BUCKET_V2/index.html" \
  --content-type "text/html; charset=utf-8" \
  --cache-control "no-cache"

if [[ -f "addresses_pool.json" ]]; then
  echo "==> Uploading addresses_pool.json..."
  aws s3 cp addresses_pool.json \
    "s3://$V1_BUCKET/addresses_pool.json" \
    --content-type "application/json" \
    --cache-control "public, max-age=86400"
  aws s3 cp addresses_pool.json \
    "s3://$WEB_BUCKET_V2/addresses_pool.json" \
    --content-type "application/json" \
    --cache-control "public, max-age=86400"
fi

if [[ -f "traffic_signals.json" ]]; then
  echo "==> Uploading traffic_signals.json..."
  aws s3 cp traffic_signals.json \
    "s3://$V1_BUCKET/traffic_signals.json" \
    --content-type "application/json" \
    --cache-control "public, max-age=86400"
  aws s3 cp traffic_signals.json \
    "s3://$WEB_BUCKET_V2/traffic_signals.json" \
    --content-type "application/json" \
    --cache-control "public, max-age=86400"
fi

if [[ -f "stop_signs.json" ]]; then
  echo "==> Uploading stop_signs.json..."
  aws s3 cp stop_signs.json \
    "s3://$V1_BUCKET/stop_signs.json" \
    --content-type "application/json" \
    --cache-control "public, max-age=86400"
  aws s3 cp stop_signs.json \
    "s3://$WEB_BUCKET_V2/stop_signs.json" \
    --content-type "application/json" \
    --cache-control "public, max-age=86400"
fi

rm -f _deploy_index.html _deploy_index_v2.html

echo "==> Invalidating CloudFront cache..."
aws cloudfront create-invalidation --distribution-id "$V1_CF_DIST" --paths "/*" --output text --query "Invalidation.Id"
aws cloudfront create-invalidation --distribution-id "$V2_CF_DIST" --paths "/*" --output text --query "Invalidation.Id"

echo ""
echo "=========================================="
echo "  HTML deploy complete!"
echo "  v1 : https://d1v86oas7j7jis.cloudfront.net"
echo "  v2 : https://d67j8fw7h34cr.cloudfront.net"
echo "=========================================="
