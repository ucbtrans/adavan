"""
AWS Lambda function — ADA Driving Assistant daily object simulation.

Schedule: run once a day (EventBridge cron: 0 6 * * ? *)

Actions:
  1. Read current city_objects.json from S3.
  2. Purge expired objects.
  3. Generate N new events for today.
  4. Write the merged list back to S3.

Environment variables:
  S3_BUCKET    – name of the S3 bucket  (required)
  OBJECTS_KEY  – S3 key for the JSON file (default: city_objects.json)
  N_EVENTS     – number of new events to generate per run (default: 300)
"""

import json
import logging
import os
from datetime import datetime, timezone

import boto3

from simulator import generate_events, purge_expired

logger = logging.getLogger()
logger.setLevel(logging.INFO)

S3_BUCKET   = os.environ["S3_BUCKET"]
OBJECTS_KEY = os.environ.get("OBJECTS_KEY", "city_objects.json")
N_EVENTS    = int(os.environ.get("N_EVENTS", "300"))


def handler(event: dict, context) -> dict:
    """Lambda entry point."""
    s3  = boto3.client("s3")
    now = datetime.now(timezone.utc)

    # ── 1. Load existing objects ─────────────────────────────────────────────
    existing: list[dict] = []
    try:
        response = s3.get_object(Bucket=S3_BUCKET, Key=OBJECTS_KEY)
        existing = json.loads(response["Body"].read().decode("utf-8"))
        logger.info("Loaded %d existing objects from s3://%s/%s",
                    len(existing), S3_BUCKET, OBJECTS_KEY)
    except s3.exceptions.NoSuchKey:
        logger.info("No existing file found — starting fresh.")
    except Exception as exc:
        logger.error("Error reading from S3: %s", exc)
        raise

    # ── 2. Purge expired ─────────────────────────────────────────────────────
    active = purge_expired(existing, now)
    purged = len(existing) - len(active)
    logger.info("Purged %d expired objects. %d remain active.", purged, len(active))

    # ── 3. Generate new events for today ─────────────────────────────────────
    new_events = generate_events(N_EVENTS, day=now)
    logger.info("Generated %d new events.", len(new_events))

    # ── 4. Merge and save ────────────────────────────────────────────────────
    merged = active + new_events
    try:
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=OBJECTS_KEY,
            Body=json.dumps(merged, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
        logger.info("Saved %d total objects to s3://%s/%s",
                    len(merged), S3_BUCKET, OBJECTS_KEY)
    except Exception as exc:
        logger.error("Error writing to S3: %s", exc)
        raise

    return {
        "statusCode": 200,
        "body": {
            "timestamp":  now.isoformat(),
            "purged":     purged,
            "kept":       len(active),
            "generated":  len(new_events),
            "total":      len(merged),
        },
    }
