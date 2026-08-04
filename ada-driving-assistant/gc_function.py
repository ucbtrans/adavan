"""
ADA GC Lambda — runs at 07:00 UTC (midnight Pacific Time).

Actions every night:
  1. Archive all live events (ada-events) → ada-events-archive keyed by PT date.
     Expired events are then deleted from the live table.
  2. GC ada-events-archive: eagerly delete items older than 7 days (TTL is backup).
  3. Generate fleet van schedules for today (PT date) if not already present.
  4. Write "generated" entries to ada-schedule-history for new schedules.
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone

import boto3

from events import archive_events_to_date, gc_archive_table
import schedule as sched_mod

logger = logging.getLogger()
logger.setLevel(logging.INFO)

S3_BUCKET = os.environ.get("S3_BUCKET", "")

CITIES = [
    ("Berkeley",   "CA/Berkeley"),
    ("Albany",     "CA/Albany"),
    ("ElCerrito",  "CA/ElCerrito"),
    ("Richmond",   "CA/Richmond"),
    ("Emeryville", "CA/Emeryville"),
    ("Oakland",    "CA/Oakland"),
]


def handler(event: dict, context) -> dict:
    now_utc = datetime.now(timezone.utc)
    pt_now  = now_utc - timedelta(hours=7)
    pt_date = pt_now.strftime("%Y-%m-%d")                            # today PT  (new day)
    yesterday_pt = (pt_now - timedelta(days=1)).strftime("%Y-%m-%d") # yesterday PT (just ended)

    # ── 1. Archive yesterday's live events ────────────────────────────────────
    # pt_midnight_utc is the boundary: events with inactive_at < this are expired
    pt_midnight_utc = datetime(now_utc.year, now_utc.month, now_utc.day,
                               7, 0, 0, tzinfo=timezone.utc)  # 07:00 UTC = midnight PT
    try:
        arch = archive_events_to_date(yesterday_pt, pt_midnight_utc)
        logger.info("Archive result for %s: %s", yesterday_pt, arch)
    except Exception as exc:
        logger.error("archive_events_to_date failed: %s", exc)
        arch = {"error": str(exc)}

    # ── 2. GC archive table (delete entries > 7 days old) ────────────────────
    try:
        gc_deleted = gc_archive_table(days=7)
        logger.info("Archive GC: deleted %d old entries", gc_deleted)
    except Exception as exc:
        logger.error("gc_archive_table failed: %s", exc)
        gc_deleted = 0

    # ── 3. Generate today's fleet schedules ───────────────────────────────────
    sched_result = _generate_fleet_schedules(pt_date)

    return {
        "statusCode":    200,
        "date_pt":       pt_date,
        "archive":       arch,
        "gc_deleted":    gc_deleted,
        "fleet_schedules": sched_result,
    }


def _generate_fleet_schedules(pt_date: str) -> dict:
    """Generate van schedules for pt_date if not already present, and save history."""
    if sched_mod.schedules_exist(pt_date):
        logger.info("Fleet schedules already exist for %s — skipping", pt_date)
        return {"skipped": True, "date": pt_date}

    # Load address pool from S3
    s3 = boto3.client("s3")
    try:
        key = "addresses_pool.json"
        tmp = "/tmp/addresses_pool.json"
        if not os.path.exists(tmp):
            s3.download_file(S3_BUCKET, key, tmp)
        with open(tmp) as f:
            raw_pool = json.load(f)
        pool = [{"address": p["address"], "lat": p["lat"], "lon": p["lon"]} for p in raw_pool]
    except Exception as exc:
        logger.error("Could not load address pool: %s", exc)
        return {"error": str(exc)}

    generated, errors = [], []
    for i in sched_mod.get_active_vans():
        v_id = sched_mod.van_id(i)
        try:
            s = sched_mod.generate_van_schedule(v_id, pt_date, pool, use_osrm=True)
            sched_mod.save_schedule(s)
            sched_mod.save_schedule_history(
                van_id=v_id, date_str=pt_date, event_type="generated",
                schedule=s, completed_rides=[], active_from_ride_seq=1,
            )
            generated.append(v_id)
            logger.info("Generated schedule+history for %s on %s", v_id, pt_date)
        except Exception as exc:
            logger.error("Schedule generation failed for %s: %s", v_id, exc)
            errors.append({"van_id": v_id, "error": str(exc)})

    return {"date": pt_date, "generated": generated, "errors": errors}
