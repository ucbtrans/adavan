#!/usr/bin/env bash
# deploy.sh — Build and deploy ADA Driving Assistant to AWS
#
# Prerequisites:
#   - AWS CLI configured (aws configure)
#   - AWS SAM CLI installed (https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)
#   - Docker running (SAM uses it to build in a Lambda-compatible environment)
#
# First run:  ./deploy.sh --guided   (prompts for parameters, saves samconfig.toml)
# Subsequent: ./deploy.sh

set -euo pipefail

STACK_NAME="ada-driving-assistant"
REGION="${AWS_DEFAULT_REGION:-us-west-2}"
SAM_CONFIG="samconfig.toml"

echo "==> Building Lambda package..."
sam build --use-container

echo "==> Deploying stack: $STACK_NAME"
if [[ "${1:-}" == "--guided" ]] || [[ ! -f "$SAM_CONFIG" ]]; then
  sam deploy \
    --stack-name "$STACK_NAME" \
    --region "$REGION" \
    --capabilities CAPABILITY_IAM \
    --guided
else
  sam deploy \
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

WEB_BUCKET=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='WebBucketName'].OutputValue" \
  --output text)

WEBSITE_URL=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='WebsiteUrl'].OutputValue" \
  --output text)

echo "    API URL     : $API_URL"
echo "    Web bucket  : $WEB_BUCKET"
echo "    Website URL : $WEBSITE_URL"

echo "==> Preparing static files for S3..."

# Inject API_BASE into a copy of index.html (write locally to avoid /tmp path issues on Windows)
python -c "
api_url = '$API_URL'
content = open('templates/index.html', encoding='utf-8').read()
content = content.replace(\"window.ADA_API_BASE || ''\", \"'\" + api_url + \"'\")
open('_deploy_index.html', 'w', encoding='utf-8').write(content)
"

echo "==> Uploading static files to s3://$WEB_BUCKET ..."
aws s3 cp _deploy_index.html \
  "s3://$WEB_BUCKET/index.html" \
  --content-type "text/html; charset=utf-8" \
  --cache-control "no-cache"

aws s3 cp static/ada_logo.jpg \
  "s3://$WEB_BUCKET/ada_logo.jpg" \
  --content-type "image/jpeg"

rm -f _deploy_index.html

echo "==> Invalidating CloudFront cache..."
CF_DIST=$(aws cloudfront list-distributions \
  --query "DistributionList.Items[?Origins.Items[0].DomainName==\`${WEB_BUCKET}.s3-website-${REGION}.amazonaws.com\`].Id" \
  --output text)
if [[ -n "$CF_DIST" ]]; then
  aws cloudfront create-invalidation --distribution-id "$CF_DIST" --paths "/*"
  echo "    Invalidation created for $CF_DIST"
else
  echo "    (no CloudFront distribution found — skipping)"
fi

CF_URL=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='CloudFrontUrl'].OutputValue" \
  --output text 2>/dev/null || echo "")

echo ""
echo "=========================================="
echo "  Deploy complete!"
echo "  CloudFront : $CF_URL"
echo "  API        : $API_URL"
echo "=========================================="
