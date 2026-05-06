"""
AWS Lambda function -- ADA Driving Assistant daily simulation + schedule generation.

Schedule: runs daily at 12:00 UTC (≈ 4-5 am Pacific Time)

Actions:
  1. Per city: download city_streets.json from S3, generate traffic events → DynamoDB.
  2. Generate fleet schedules for today if not already present.
  3. Garbage-collect stale events (> 7 days past inactive_at).

Environment variables:
  S3_BUCKET     - name of the S3 data bucket (required)
  EVENTS_TABLE  - DynamoDB events table name (default: ada-events)
  FLEET_TABLE   - DynamoDB fleet config table (default: ada-fleet-config)
"""

import json
import logging
import os
from datetime import datetime, timezone, timedelta

import boto3

from events import put_event, delete_stale_events
from simulator import generate_events
import schedule as sched_mod

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
    """Generate new events for one city and write them to DynamoDB."""
    # 1. Load streets and compute event count
    streets  = _load_streets_from_s3(s3, prefix)
    n_events = min(75, max(1, len(streets) // 10))
    logger.info("%s: %d streets → %d events", city, len(streets), n_events)

    # 2. Generate new events for this city's streets
    new_events = generate_events(n_events, day=now, streets=streets)
    logger.info("%s: generated %d new events", city, len(new_events))

    # 3. Write each event to DynamoDB (each has a unique event_id sort key — no read needed)
    for ev in new_events:
        # Ensure city field is set for the city-index GSI
        ev["city"] = city
        put_event(ev)

    return {
        "city":      city,
        "streets":   len(streets),
        "n_events":  n_events,
        "generated": len(new_events),
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

    logger.info("All cities done. Generated %d events across %d cities. %d errors.",
                total_generated, len(results), len(errors))

    # Garbage-collect events expired more than 7 days ago
    try:
        deleted = delete_stale_events(days=7)
        logger.info("Garbage collection: deleted %d stale events", deleted)
    except Exception as exc:
        logger.error("Garbage collection failed: %s", exc)
        deleted = 0

    # ── Fleet schedule generation ─────────────────────────────────────────────
    # Generate today's schedules (PT date = UTC - 7 h) if not already present.
    sched_result = _generate_fleet_schedules(s3, now)

    return {
        "statusCode": 200 if not errors else 207,
        "body": {
            "timestamp":       now.isoformat(),
            "cities_ok":       len(results),
            "cities_failed":   len(errors),
            "total_generated": total_generated,
            "stale_deleted":   deleted,
            "fleet_schedules": sched_result,
            "details":         results,
            "errors":          errors,
        },
    }


def _generate_fleet_schedules(s3, now: datetime) -> dict:
    """Generate van schedules for today's PT date if not already present."""
    pt_date = (now - timedelta(hours=7)).strftime("%Y-%m-%d")
    if sched_mod.schedules_exist(pt_date):
        logger.info("Fleet schedules already exist for %s — skipping", pt_date)
        return {"skipped": True, "date": pt_date}

    # Load address pool from S3
    try:
        key = "addresses_pool.json"
        tmp = "/tmp/addresses_pool.json"
        if not os.path.exists(tmp):
            s3.download_file(os.environ["S3_BUCKET"], key, tmp)
        with open(tmp) as f:
            raw_pool = json.load(f)
        pool = [{"address": p["address"], "lat": p["lat"], "lon": p["lon"]} for p in raw_pool]
    except Exception as exc:
        logger.error("Could not load address pool: %s", exc)
        return {"error": str(exc)}

    generated, errors = [], []
    for i in range(1, sched_mod.NUM_VANS + 1):
        v_id = sched_mod.van_id(i)
        try:
            s = sched_mod.generate_van_schedule(v_id, pt_date, pool)
            sched_mod.save_schedule(s)
            generated.append(v_id)
            logger.info("Generated schedule for %s on %s", v_id, pt_date)
        except Exception as exc:
            logger.error("Schedule generation failed for %s: %s", v_id, exc)
            errors.append({"van_id": v_id, "error": str(exc)})

    return {"date": pt_date, "generated": generated, "errors": errors}
