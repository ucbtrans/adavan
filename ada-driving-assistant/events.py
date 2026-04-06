"""
events.py — DynamoDB CRUD layer for ADA traffic events.

Table: ada-events
  Partition key : street   (S)
  Sort key      : event_id (S)
  GSI city-index     : city     (HASH), event_id (RANGE)
  GSI geohash6-index : geohash6 (HASH), event_id (RANGE)
  TTL attribute : ttl (unix epoch seconds)

Each item retains ttl = unix(inactive_at) + 7*86400 so DynamoDB
auto-deletes the row one week after it expires, while queries still
see it during that grace window (filtered by inactive_at in Python).
"""

from __future__ import annotations

import math
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Attr, Key

# ── Pure-Python geohash (replaces python-geohash C extension) ────────────────
_GH_BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"

def _gh_encode(lat: float, lon: float, precision: int = 6) -> str:
    lat_lo, lat_hi = -90.0, 90.0
    lon_lo, lon_hi = -180.0, 180.0
    bits = [16, 8, 4, 2, 1]
    bits_total = precision * 5
    is_lon = True
    bit = 0
    ch  = 0
    result = []
    while len(result) < precision:
        if is_lon:
            mid = (lon_lo + lon_hi) / 2
            if lon >= mid:
                ch |= bits[bit]
                lon_lo = mid
            else:
                lon_hi = mid
        else:
            mid = (lat_lo + lat_hi) / 2
            if lat >= mid:
                ch |= bits[bit]
                lat_lo = mid
            else:
                lat_hi = mid
        is_lon = not is_lon
        if bit < 4:
            bit += 1
        else:
            result.append(_GH_BASE32[ch])
            bit = 0
            ch  = 0
    return "".join(result)

_GH_NEIGHBORS = {
    "right":  {"even": "bc01fg45telegramhijklmnopqrstuvwx", "odd": "p0r21436x8zb9dcf5h7kjnmqesgutwvy"},
    "left":   {"even": "238967debc01telegramhjklnopqrsvwyx", "odd": "14365h7k9dcfesgujnmqp0r2twvyx8zb"},
    "top":    {"even": "p0r21436x8zb9dcf5h7kjnmqesgutwvy", "odd": "bc01fg45telegramhijklmnopqrstuvwx"},
    "bottom": {"even": "14365h7k9dcfesgujnmqp0r2twvyx8zb", "odd": "238967debc01telegramhjklnopqrsvwyx"},
}

def _gh_neighbors(h: str) -> dict[str, str]:
    """Return the 8 neighboring geohash cells."""
    def _adj(h, direction):
        last = h[-1]
        typ  = "odd" if len(h) % 2 else "even"
        base = h[:-1]
        if last in "bcfguvyz" and direction in ("top", "right"):
            base = _adj(base, direction) if base else ""
        n_table = _GH_NEIGHBORS[direction][typ]
        b_table = _GH_BASE32
        idx = n_table.find(last) if last in n_table else b_table.find(last)
        return (base or "") + (b_table[idx] if idx >= 0 else last)

    # Simpler approach: compute neighbors via coordinate offsets
    lat, lon, dlat, dlon = _gh_decode_bbox(h)
    return {
        "n":  _gh_encode(lat + dlat, lon,        len(h)),
        "s":  _gh_encode(lat - dlat, lon,        len(h)),
        "e":  _gh_encode(lat,        lon + dlon, len(h)),
        "w":  _gh_encode(lat,        lon - dlon, len(h)),
        "ne": _gh_encode(lat + dlat, lon + dlon, len(h)),
        "nw": _gh_encode(lat + dlat, lon - dlon, len(h)),
        "se": _gh_encode(lat - dlat, lon + dlon, len(h)),
        "sw": _gh_encode(lat - dlat, lon - dlon, len(h)),
    }

def _gh_decode_bbox(h: str):
    """Return (center_lat, center_lon, half_lat, half_lon)."""
    lat_lo, lat_hi = -90.0, 90.0
    lon_lo, lon_hi = -180.0, 180.0
    is_lon = True
    for ch in h:
        idx = _GH_BASE32.find(ch)
        for bit in (16, 8, 4, 2, 1):
            if is_lon:
                mid = (lon_lo + lon_hi) / 2
                if idx & bit:
                    lon_lo = mid
                else:
                    lon_hi = mid
            else:
                mid = (lat_lo + lat_hi) / 2
                if idx & bit:
                    lat_lo = mid
                else:
                    lat_hi = mid
            is_lon = not is_lon
    return (lat_lo + lat_hi) / 2, (lon_lo + lon_hi) / 2, (lat_hi - lat_lo) / 2, (lon_hi - lon_lo) / 2

EVENTS_TABLE = os.environ.get("EVENTS_TABLE", "ada-events")

_ddb    = None   # lazy singleton
_table  = None


def _get_table():
    global _ddb, _table
    if _table is None:
        _ddb   = boto3.resource("dynamodb")
        _table = _ddb.Table(EVENTS_TABLE)
    return _table


# ── Type conversion ──────────────────────────────────────────────────────────

def _to_decimal(v) -> Decimal:
    return Decimal(str(v))


def _floats_to_decimal(obj):
    """Recursively convert all float values in a dict/list to Decimal."""
    if isinstance(obj, float):
        return _to_decimal(obj)
    if isinstance(obj, dict):
        return {k: _floats_to_decimal(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_floats_to_decimal(i) for i in obj]
    return obj


def event_lat_lon(ev: dict) -> tuple[float, float]:
    """
    Extract (lat, lon) from any event structure.
    Events store coordinates in one of three shapes:
      • ev["lat"] / ev["lon"]                     (already normalised, e.g. from vans)
      • ev["coordinates"]["lat|lon"]              (single_cone, dropped_object)
      • ev["polygon"] list of {lat,lon}           (most types)
      • ev["left_coordinates"] + ["right_coordinates"] (road_barrier)
    """
    # Already normalised
    if ev.get("lat") and ev.get("lon"):
        return float(ev["lat"]), float(ev["lon"])
    # Single-point
    c = ev.get("coordinates")
    if c:
        return float(c["lat"]), float(c["lon"])
    # Polygon centroid
    pts = ev.get("polygon")
    if pts:
        return (sum(float(p["lat"]) for p in pts) / len(pts),
                sum(float(p["lon"]) for p in pts) / len(pts))
    # Road barrier
    lc = ev.get("left_coordinates")
    rc = ev.get("right_coordinates")
    if lc and rc:
        return (float(lc["lat"]) + float(rc["lat"])) / 2, (float(lc["lon"]) + float(rc["lon"])) / 2
    return 0.0, 0.0


def event_to_dynamo(ev: dict) -> dict:
    """
    Normalise and store an event in DynamoDB format:
    - Map "id" → "event_id" (sort key)
    - Extract lat/lon from nested coordinates and store at top level
    - Add geohash6, geohash7 (spatial index), ttl (auto-delete after 7 days past expiry)
    Returns a new dict (does not mutate ev).
    """
    item = {}
    for k, v in ev.items():
        # Map legacy "id" field to "event_id" (DynamoDB sort key)
        key = "event_id" if k == "id" else k
        item[key] = _floats_to_decimal(v)

    # Ensure event_id exists (generate one if missing)
    if "event_id" not in item:
        import uuid as _uuid
        item["event_id"] = str(_uuid.uuid4())

    # Always store lat/lon at top level for easy querying
    lat, lon = event_lat_lon(ev)
    item["lat"] = _to_decimal(lat)
    item["lon"] = _to_decimal(lon)

    item["geohash6"] = _gh_encode(lat, lon, precision=6)
    item["geohash7"] = _gh_encode(lat, lon, precision=7)

    inactive_at = ev.get("inactive_at", "")
    try:
        ts = datetime.fromisoformat(inactive_at.replace("Z", "+00:00")).timestamp()
        item["ttl"] = int(ts) + 7 * 86400   # keep 7 days past expiry
    except Exception:
        item["ttl"] = int(datetime.now(timezone.utc).timestamp()) + 14 * 86400

    return item


def _decimals_to_float(obj):
    """Recursively convert all Decimal values in a dict/list to float."""
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: _decimals_to_float(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decimals_to_float(i) for i in obj]
    return obj


def dynamo_to_event(item: dict) -> dict:
    """Convert a DynamoDB item back to a plain event dict (Decimal → float)."""
    ev = {}
    for k, v in item.items():
        if k in ("ttl", "geohash6", "geohash7"):
            continue   # internal fields
        ev[k] = _decimals_to_float(v)
    return ev


# ── Write ────────────────────────────────────────────────────────────────────

def put_event(ev: dict) -> None:
    """Write a single event to DynamoDB. Adds geohash and TTL fields."""
    item = event_to_dynamo(ev)
    _get_table().put_item(Item=item)


# ── Read ─────────────────────────────────────────────────────────────────────

def _is_active(item: dict, now: datetime) -> bool:
    """Return True if the event's window covers now."""
    try:
        active_at   = datetime.fromisoformat(str(item["active_at"]).replace("Z", "+00:00"))
        inactive_at = datetime.fromisoformat(str(item["inactive_at"]).replace("Z", "+00:00"))
        return active_at <= now < inactive_at
    except Exception:
        return False


def get_events_by_city(city: str, now: datetime) -> list[dict]:
    """Query city-index for all active events in a city."""
    table  = _get_table()
    result = []
    kwargs = {
        "IndexName": "city-index",
        "KeyConditionExpression": Key("city").eq(city),
    }
    while True:
        resp = table.query(**kwargs)
        for item in resp.get("Items", []):
            if _is_active(item, now):
                result.append(dynamo_to_event(item))
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            break
        kwargs["ExclusiveStartKey"] = lek
    return result


def get_events_by_street(street: str, now: datetime) -> list[dict]:
    """Query by partition key (street) and return active events."""
    table  = _get_table()
    result = []
    kwargs = {
        "KeyConditionExpression": Key("street").eq(street),
    }
    while True:
        resp = table.query(**kwargs)
        for item in resp.get("Items", []):
            if _is_active(item, now):
                result.append(dynamo_to_event(item))
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            break
        kwargs["ExclusiveStartKey"] = lek
    return result


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def get_events_near(lat: float, lon: float, radius_m: float, now: datetime) -> list[dict]:
    """
    Query geohash6-index for all 9 neighboring cells and filter by haversine.
    geohash precision 6 ≈ 1.2 km — more than enough to cover radius_m ≤ ~500 m.
    """
    table    = _get_table()
    center   = _gh_encode(lat, lon, precision=6)
    cells    = [center] + list(_gh_neighbors(center).values())
    seen     = set()
    result   = []

    for cell in cells:
        kwargs = {
            "IndexName": "geohash6-index",
            "KeyConditionExpression": Key("geohash6").eq(cell),
        }
        while True:
            resp = table.query(**kwargs)
            for item in resp.get("Items", []):
                eid = item.get("event_id")
                if eid in seen:
                    continue
                seen.add(eid)
                ev_lat = float(item.get("lat", 0))
                ev_lon = float(item.get("lon", 0))
                if _haversine_m(lat, lon, ev_lat, ev_lon) <= radius_m and _is_active(item, now):
                    result.append(dynamo_to_event(item))
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                break
            kwargs["ExclusiveStartKey"] = lek

    return result


# ── Mutation ─────────────────────────────────────────────────────────────────

def clear_event(street: str, event_id: str, now_iso: str) -> None:
    """
    Mark an event as cleared by setting inactive_at to now.
    Also update ttl so DynamoDB can garbage-collect after 7 days.
    """
    try:
        ts  = datetime.fromisoformat(now_iso.replace("Z", "+00:00")).timestamp()
        ttl = int(ts) + 7 * 86400
    except Exception:
        ttl = int(datetime.now(timezone.utc).timestamp()) + 7 * 86400

    _get_table().update_item(
        Key={"street": street, "event_id": event_id},
        UpdateExpression="SET inactive_at = :ia, #t = :ttl",
        ExpressionAttributeNames={"#t": "ttl"},
        ExpressionAttributeValues={":ia": now_iso, ":ttl": ttl},
    )


# ── Garbage collection ───────────────────────────────────────────────────────

def delete_stale_events(days: int = 7) -> int:
    """
    Scan for events whose inactive_at is older than `days` days and batch-delete them.
    Returns count of deleted items.
    Note: DynamoDB TTL also handles this eventually; this call provides an eager purge.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    table  = _get_table()
    deleted = 0

    kwargs: dict[str, Any] = {
        "FilterExpression": Attr("inactive_at").lt(cutoff),
        "ProjectionExpression": "street, event_id",
    }

    while True:
        resp  = table.scan(**kwargs)
        items = resp.get("Items", [])
        if items:
            with table.batch_writer() as batch:
                for item in items:
                    batch.delete_item(Key={"street": item["street"], "event_id": item["event_id"]})
            deleted += len(items)
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            break
        kwargs["ExclusiveStartKey"] = lek

    return deleted
