#!/usr/bin/env python
"""
position_to_s3.py -- publish the van's latest GPS fix to S3.

This is the desk-side counterpart of adavan/position_to_aws.py, which runs on
the van: read the fix log written by the GPS receiver, take the last valid row,
convert the raw NMEA coordinates to decimal degrees, name the street, and write
van_position.json to S3. ada-real-van-ingest polls that object and the fleet map
draws the van from it.

Two deliberate differences from position_to_aws.py:

  * The street name comes from city_streets.json via a nearest-segment search
    rather than from osmnx. It needs no network call and no geo stack, and it
    returns exactly the street names the web app already knows, so the UI's
    street/city lookups agree with the feed.
  * The hemisphere is read from the CSV's direction columns instead of assuming
    west. Same result in Berkeley, but it will not silently mirror the van if it
    is ever driven elsewhere.

Usage:
    python position_to_s3.py                      # publish once a minute
    python position_to_s3.py --once               # single shot
    python position_to_s3.py --once --dry-run     # build payload, skip S3
    python position_to_s3.py --csv other.csv --key van_position_sim.json

Note the default key is the one production reads. Use --dry-run, or a different
--key, when you do not want the live fleet map to follow this feed.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import threading
import time
from datetime import datetime, timezone

# Shared with the web app so the reported street matches what the UI knows.
from location import _load_streets, _point_to_segment_dist_m, haversine_m

DEFAULT_CSV    = "van_gps.csv"
DEFAULT_BUCKET = "ada-van-service-data"
DEFAULT_KEY    = "van_position.json"
DEFAULT_REGION = "us-west-1"
LOCAL_JSON     = "van_position.json"

_NULLS = {"", "null", "none", "nan"}


# ── Reading the fix log ──────────────────────────────────────────────────────

def to_decimal_degrees(raw: str, hemisphere: str = "") -> float:
    """
    Raw NMEA DDMM.MMMMM (or DDDMM.MMMMM) -> decimal degrees.

    Same arithmetic as position_to_aws.convert_to_decimal_degrees, with the sign
    taken from the hemisphere rather than hard-coded.
    """
    value = abs(float(raw))
    degrees = int(value // 100)
    minutes = value % 100
    dd = degrees + minutes / 60.0
    return -dd if hemisphere.strip().upper() in {"S", "W"} else dd


def read_last_fix(csv_path: str) -> dict | None:
    """
    Last row of the fix log with usable coordinates, as decimal degrees.

    The GPS logs "Null" whenever a fix is missing, and the simulator may be
    mid-append, so rows that do not parse are skipped rather than fatal.
    """
    last = None
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            lat_raw = (row.get("Latitude") or "").strip()
            lon_raw = (row.get("Longitude") or "").strip()
            if lat_raw.lower() in _NULLS or lon_raw.lower() in _NULLS:
                continue
            try:
                lat = to_decimal_degrees(lat_raw, row.get("Latitude_direction", ""))
                lon = to_decimal_degrees(lon_raw, row.get("Longitude_direction", ""))
            except (TypeError, ValueError):
                continue
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                continue
            last = {"time": (row.get("Camera_time") or "").strip(),
                    "lat": lat, "lon": lon}
    return last


# ── Street naming ────────────────────────────────────────────────────────────

def street_at(lat: float, lon: float, max_m: float = 60.0) -> str:
    """
    Nearest named street to a point, or "" when nothing is close enough.

    Candidate streets are filtered by a coarse degree box first; without it this
    walks all ~33 k vertices on every call.
    """
    # ~1 deg latitude is 111 km; pad generously and let the exact test decide.
    pad = max(max_m, 60.0) / 111_000.0 * 2
    best_name, best_dist = "", float("inf")

    for st in _load_streets().get("streets", []):
        name = (st.get("name") or "").strip()
        if not name or name.startswith("Unnamed_"):
            continue
        for seg in st.get("segments", []):
            prev = None
            for p in seg:
                if prev is not None:
                    if (min(prev["lat"], p["lat"]) - pad <= lat <= max(prev["lat"], p["lat"]) + pad
                            and min(prev["lon"], p["lon"]) - pad <= lon <= max(prev["lon"], p["lon"]) + pad):
                        d = _point_to_segment_dist_m(lat, lon, prev["lat"], prev["lon"],
                                                     p["lat"], p["lon"])
                        if d < best_dist:
                            best_name, best_dist = name, d
                prev = p
    return best_name if best_dist <= max_m else ""


# ── Publishing ───────────────────────────────────────────────────────────────

def build_payload(fix: dict) -> dict:
    """The exact shape /api/fleet/real-van and real_van_ingest.py expect."""
    return {
        "Time":      fix["time"] or datetime.now(timezone.utc)
                     .isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "Latitude":  fix["lat"],
        "Longitude": fix["lon"],
        "Street":    street_at(fix["lat"], fix["lon"]),
    }


def publish_once(csv_path: str = DEFAULT_CSV,
                 bucket: str = DEFAULT_BUCKET,
                 key: str = DEFAULT_KEY,
                 region: str = DEFAULT_REGION,
                 dry_run: bool = False,
                 quiet: bool = False,
                 local_json: str = LOCAL_JSON) -> dict | None:
    """Read, convert, write van_position.json locally, and upload it."""
    try:
        fix = read_last_fix(csv_path)
    except FileNotFoundError:
        if not quiet:
            print(f"position_to_s3: no fix log at {csv_path} yet")
        return None
    if not fix:
        if not quiet:
            print(f"position_to_s3: no valid rows in {csv_path} yet")
        return None

    payload = build_payload(fix)
    with open(local_json, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=4)

    if dry_run:
        if not quiet:
            print(f"position_to_s3: [dry-run] {payload['Latitude']:.6f},"
                  f"{payload['Longitude']:.6f} {payload['Street'] or '(no street)'}"
                  f" @ {payload['Time']}")
        return payload

    try:
        import boto3
        boto3.client("s3", region_name=region).upload_file(local_json, bucket, key)
    except Exception as exc:
        print(f"position_to_s3: AWS error: {exc}")
        return payload

    if not quiet:
        print(f"position_to_s3: -> s3://{bucket}/{key} "
              f"{payload['Latitude']:.6f},{payload['Longitude']:.6f} "
              f"{payload['Street'] or '(no street)'} @ {payload['Time']}")
    return payload


def publish_loop(csv_path: str = DEFAULT_CSV,
                 bucket: str = DEFAULT_BUCKET,
                 key: str = DEFAULT_KEY,
                 region: str = DEFAULT_REGION,
                 interval: float = 60.0,
                 dry_run: bool = False,
                 quiet: bool = False,
                 stop: threading.Event | None = None) -> None:
    """
    Publish every `interval` seconds until interrupted.

    `stop` lets the caller end the wait early. gps_sim passes its shutdown flag,
    so a Ctrl-C does not have to wait out an interval that may be a minute long.
    """
    stop = stop or threading.Event()
    while not stop.is_set():
        try:
            payload = publish_once(csv_path, bucket, key, region, dry_run, quiet=False)
            if quiet and payload:
                # Called from gps_sim, whose own line is being rewritten in place.
                print(f"\nposition_to_s3: uploaded {payload['Latitude']:.6f},"
                      f"{payload['Longitude']:.6f} {payload['Street']}")
        except Exception as exc:
            print(f"position_to_s3: {type(exc).__name__}: {exc}")
        # Sliced rather than one long wait, so a stop set while we are idle here
        # is noticed promptly -- see the note on gps_sim.wait_or_stop.
        deadline = time.monotonic() + interval
        while not stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            stop.wait(min(0.2, remaining))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--csv", default=DEFAULT_CSV, help="fix log written by gps_sim.py")
    p.add_argument("--bucket", default=DEFAULT_BUCKET)
    p.add_argument("--key", default=DEFAULT_KEY)
    p.add_argument("--region", default=DEFAULT_REGION)
    p.add_argument("--interval", type=float, default=60.0, help="seconds between uploads")
    p.add_argument("--once", action="store_true", help="publish once and exit")
    p.add_argument("--dry-run", action="store_true", help="skip the S3 upload")
    return p


def main(argv=None) -> int:
    opt = build_parser().parse_args(argv)
    if opt.once:
        got = publish_once(opt.csv, opt.bucket, opt.key, opt.region, opt.dry_run)
        return 0 if got else 1
    print(f"position_to_s3: {opt.csv} -> s3://{opt.bucket}/{opt.key} "
          f"every {opt.interval}s (Ctrl-C to stop)")
    try:
        publish_loop(opt.csv, opt.bucket, opt.key, opt.region, opt.interval, opt.dry_run)
    except KeyboardInterrupt:
        print("\nposition_to_s3: stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
