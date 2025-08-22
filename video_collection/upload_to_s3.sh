#!/bin/bash
# Variables
PROFILE="video_destination"
BUCKET_PREFIX="ada-video-storage"
# Check for file argument
if [[ -z "$1" ]]; then
  echo "Usage: $0 <file-to-upload>"
  exit 1
fi
FILE_TO_UPLOAD="$1"
# Check if file exists
if [[ ! -f "$FILE_TO_UPLOAD" ]]; then
  echo "Error: File '$FILE_TO_UPLOAD' not found."
  exit 1
fi
# Get AWS account number
ACCOUNT_ID=$(aws sts get-caller-identity \
  --query Account \
  --output text \
  --profile "$PROFILE")
# Check if we got an account ID
if [[ -z "$ACCOUNT_ID" ]]; then
  echo "Error: Could not retrieve AWS account number for profile
'$PROFILE'."
  exit 1
fi
# Construct bucket name
BUCKET_NAME="${BUCKET_PREFIX}-${ACCOUNT_ID}"
echo "Uploading $FILE_TO_UPLOAD to
s3://${BUCKET_NAME}/$FILE_TO_UPLOAD ..."
# Upload the file
aws s3 --profile "$PROFILE" cp "$FILE_TO_UPLOAD" "s3://${BUCKET_NAME}/$FILE_TO_UPLOAD"
# Check if upload succeeded
if [[ $? -eq 0 ]]; then
  echo "Upload completed successfully."
  rm -f -- "$FILE_TO_UPLOAD"
else
  echo "Upload failed."
  exit 1
fi