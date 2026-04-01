"""
AWS Lambda function -- ADA Driving Assistant daily object simulation.

Schedule: run once a day (EventBridge cron: 0 6 * * ? *)

Actions (per city):
  1. Download city_streets.json from S3.
  2. Compute N events = len(streets) // 3.
  3. Read current city_objects.json from S3.
  4. Purge expired objects.
  5. Generate N new events for today.
  6. Write the merged list back to S3.

Environment variables:
  S3_BUCKET  - name of the S3 data bucket (required)
"""

import json
import logging
import os
from datetime import datetime, timezone

import boto3

from simulator import generate_events, purge_expired

logger = logging.getLogger()
logger.setLevel(logging.INFO)

S3_BUCKET = os.environ["S3_BUCKET"]

# All supported cities: (display_name, S3_prefix)
CITIES = [
    ("Berkeley",   "CA/Berkeley"),
    ("Albany",     "CA/Albany"),
    ("ElCerrito",  "CA/ElCerrito"),
    ("Richmond",   "CA/Richmond"),
    ("Emeryville", "CA/Emeryville"),
    ("Oakland",    "CA/Oakland"),
]


def _load_streets_from_s3(s3, prefix: str) -> list[dict]:
    """Download city_streets.json for a city and return the streets list."""
    key      = f"{prefix}/city_streets.json"
    tmp_path = f"/tmp/streets_{prefix.replace('/', '_')}.json"
    if not os.path.exists(tmp_path):
        logger.info("Downloading s3://%s/%s", S3_BUCKET, key)
        s3.download_file(S3_BUCKET, key, tmp_path)
    with open(tmp_path) as f:
        data = json.load(f)
    return data["streets"]


def _process_city(s3, city: str, prefix: str, now: datetime) -> dict:
    """Run the full simulate cycle for one city. Returns a summary dict."""
    objects_key = f"{prefix}/city_objects.json"

    # 1. Load streets and compute event count
    streets  = _load_streets_from_s3(s3, prefix)
    n_events = max(1, len(streets) // 3)
    logger.info("%s: %d streets → %d events", city, len(streets), n_events)

    # 2. Load existing objects
    existing: list[dict] = []
    try:
        response = s3.get_object(Bucket=S3_BUCKET, Key=objects_key)
        existing = json.loads(response["Body"].read().decode("utf-8"))
        logger.info("%s: loaded %d existing objects", city, len(existing))
    except s3.exceptions.NoSuchKey:
        logger.info("%s: no existing objects file — starting fresh", city)
    except Exception as exc:
        logger.error("%s: error reading objects: %s", city, exc)
        raise

    # 3. Purge expired
    active = purge_expired(existing, now)
    purged = len(existing) - len(active)
    logger.info("%s: purged %d expired, %d remain", city, purged, len(active))

    # 4. Generate new events using this city's streets
    new_events = generate_events(n_events, day=now, streets=streets)
    logger.info("%s: generated %d new events", city, len(new_events))

    # 5. Merge and save
    merged = active + new_events
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=objects_key,
        Body=json.dumps(merged, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    logger.info("%s: saved %d total objects to s3://%s/%s",
                city, len(merged), S3_BUCKET, objects_key)

    return {
        "city":      city,
        "streets":   len(streets),
        "n_events":  n_events,
        "purged":    purged,
        "kept":      len(active),
        "generated": len(new_events),
        "total":     len(merged),
    }


def handler(event: dict, context) -> dict:
    """Lambda entry point — processes all cities."""
    s3  = boto3.client("s3")
    now = datetime.now(timezone.utc)

    results = []
    errors  = []

    for city, prefix in CITIES:
        try:
            summary = _process_city(s3, city, prefix, now)
            results.append(summary)
        except Exception as exc:
            logger.error("Failed to process %s: %s", city, exc)
            errors.append({"city": city, "error": str(exc)})

    total_generated = sum(r["generated"] for r in results)
    total_objects   = sum(r["total"]     for r in results)

    logger.info("All cities done. Generated %d events across %d cities. %d errors.",
                total_generated, len(results), len(errors))

    return {
        "statusCode": 200 if not errors else 207,
        "body": {
            "timestamp":       now.isoformat(),
            "cities_ok":       len(results),
            "cities_failed":   len(errors),
            "total_generated": total_generated,
            "total_objects":   total_objects,
            "details":         results,
            "errors":          errors,
        },
    }
