"""
Fleet schedule management — DynamoDB CRUD + OSRM transit estimation.

One DynamoDB item per van per day (config_key = "schedule#VAN_NN#YYYY-MM-DD").
Each item stores 10 rides with pre-computed OSRM durations so the browser can
interpolate van positions from wall-clock time without hitting routing APIs.
"""

import json, math, os, random, time, logging
from datetime import datetime, timezone
from decimal import Decimal
import boto3
import requests

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
BASE_LAT      = 37.8897
BASE_LON      = -122.3024
BASE_ADDRESS  = "535 Pierce Street, Albany, CA"
NUM_VANS      = 10
RIDES_PER_VAN = 10
DAY_START_MIN = 5 * 60          # 5:00 AM local (PT) in minutes since midnight
MAX_JITTER_MIN = 60             # max window between rides
OSRM_URL      = "http://router.project-osrm.org/route/v1/driving"
OSRM_SLEEP    = 0.12            # seconds between OSRM calls (rate-limit)

# ── DynamoDB helpers ──────────────────────────────────────────────────────────
_tbl = None

def _table():
    global _tbl
    if _tbl is None:
        ddb = boto3.resource("dynamodb")
        _tbl = ddb.Table(os.environ.get("FLEET_TABLE", "ada-fleet-config"))
    return _tbl

def van_id(n: int) -> str:      # n is 1-based
    return f"VAN_{n:02d}"

def _sched_key(v_id: str, date_str: str) -> str:
    return f"schedule#{v_id}#{date_str}"

# ── OSRM ──────────────────────────────────────────────────────────────────────
def _haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.asin(math.sqrt(a))

def _fallback_sec(lat1, lon1, lat2, lon2):
    km = _haversine_km(lat1, lon1, lat2, lon2)
    return km / 32.0 * 3600   # ~20 mph / 32 km/h urban estimate

def osrm_duration_sec(from_lat, from_lon, to_lat, to_lon) -> float:
    """Return OSRM driving duration in seconds, with straight-line fallback."""
    try:
        url = (f"{OSRM_URL}/{from_lon:.6f},{from_lat:.6f};"
               f"{to_lon:.6f},{to_lat:.6f}?overview=false")
        r = requests.get(url, timeout=8)
        data = r.json()
        if data.get("code") == "Ok":
            return float(data["routes"][0]["duration"])
    except Exception as exc:
        logger.warning("OSRM error: %s — using straight-line fallback", exc)
    return _fallback_sec(from_lat, from_lon, to_lat, to_lon)

# ── Geocoding ──────────────────────────────────────────────────────────────────
def geocode(address: str):
    """Return (lat, lon) via Nominatim, or (None, None) on failure."""
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": address, "format": "json", "limit": 1},
            headers={"User-Agent": "ADA-Driving-Assistant/3.0"},
            timeout=8,
        )
        hits = r.json()
        if hits:
            return float(hits[0]["lat"]), float(hits[0]["lon"])
    except Exception as exc:
        logger.warning("Geocode failed for %r: %s", address, exc)
    return None, None

# ── Schedule generation ───────────────────────────────────────────────────────
def generate_van_schedule(v_id: str, date_str: str, pool: list, use_osrm: bool = False) -> dict:
    """
    Build a 10-ride schedule for one van.
    pool: list of dicts with {address, lat, lon}.
    use_osrm=True calls OSRM for accurate timing (slow, for daily Lambda).
    use_osrm=False uses straight-line fallback (fast, for on-demand API).
    """
    def _dur_sec(lat1, lon1, lat2, lon2):
        if use_osrm:
            sec = osrm_duration_sec(lat1, lon1, lat2, lon2)
            time.sleep(OSRM_SLEEP)
            return sec
        return _fallback_sec(lat1, lon1, lat2, lon2)

    sample = random.sample(pool, min(RIDES_PER_VAN * 2, len(pool)))
    rides  = []
    cur_lat, cur_lon = BASE_LAT, BASE_LON
    cur_time_min     = float(DAY_START_MIN)   # first pickup at 05:00

    for seq in range(1, RIDES_PER_VAN + 1):
        from_e = sample[(seq - 1) * 2 % len(sample)]
        to_e   = sample[((seq - 1) * 2 + 1) % len(sample)]
        from_lat, from_lon = from_e["lat"], from_e["lon"]
        to_lat,   to_lon   = to_e["lat"],   to_e["lon"]

        # Dead-head leg: current position → pickup
        dh_sec = _dur_sec(cur_lat, cur_lon, from_lat, from_lon)
        dh_min = dh_sec / 60.0

        if seq == 1:
            pickup_min  = cur_time_min          # exactly 5:00 AM
            depart_min  = max(0.0, pickup_min - dh_min)
        else:
            earliest    = cur_time_min + dh_min
            pickup_min  = earliest + random.uniform(0, MAX_JITTER_MIN)
            depart_min  = cur_time_min          # leave immediately after dropoff

        # Ride leg: pickup → dropoff
        ride_sec    = _dur_sec(from_lat, from_lon, to_lat, to_lon)
        ride_min    = ride_sec / 60.0
        dropoff_min = pickup_min + ride_min

        rides.append({
            "seq":          seq,
            "start_time":   _fmt(pickup_min),
            "from_address": from_e["address"],
            "to_address":   to_e["address"],
            "from_lat":     round(from_lat, 6),
            "from_lon":     round(from_lon, 6),
            "to_lat":       round(to_lat,   6),
            "to_lon":       round(to_lon,   6),
            "dh_from_lat":  round(cur_lat,  6),
            "dh_from_lon":  round(cur_lon,  6),
            "depart_min":   round(depart_min,  2),
            "pickup_min":   round(pickup_min,  2),
            "dropoff_min":  round(dropoff_min, 2),
            "dh_sec":       round(dh_sec),
            "ride_sec":     round(ride_sec),
        })

        cur_lat, cur_lon = to_lat, to_lon
        cur_time_min     = dropoff_min

    # Return-to-base leg
    rtb_sec = _dur_sec(cur_lat, cur_lon, BASE_LAT, BASE_LON)

    return {
        "van_id":       v_id,
        "date":         date_str,
        "rides":        rides,
        "rtb_from_lat": round(cur_lat, 6),
        "rtb_from_lon": round(cur_lon, 6),
        "rtb_sec":      round(rtb_sec),
    }

def _fmt(minutes: float) -> str:
    h = int(minutes) // 60   # no % 24 — hours >23 mean next-day (transit notation)
    m = int(minutes) % 60
    return f"{h:02d}:{m:02d}"

# ── Recompute timing after user edits ─────────────────────────────────────────
def recompute_schedule_timing(sched: dict) -> dict:
    """
    Re-run OSRM for each ride in the edited schedule and update
    depart_min / pickup_min / dropoff_min / dh_sec / ride_sec.
    start_time for ride 1 is fixed at 05:00; subsequent rides keep their
    user-set start_time if it's achievable, otherwise advance to earliest.
    """
    rides     = sched["rides"]
    cur_lat   = BASE_LAT
    cur_lon   = BASE_LON
    cur_time  = float(DAY_START_MIN)

    for i, r in enumerate(rides):
        from_lat, from_lon = r["from_lat"], r["from_lon"]
        to_lat,   to_lon   = r["to_lat"],   r["to_lon"]

        dh_sec = osrm_duration_sec(cur_lat, cur_lon, from_lat, from_lon)
        time.sleep(OSRM_SLEEP)
        dh_min = dh_sec / 60.0

        if i == 0:
            pickup_min  = float(DAY_START_MIN)
            depart_min  = max(0.0, pickup_min - dh_min)
        else:
            # Parse user start_time; enforce earliest feasible
            user_hhmm   = r.get("start_time", "")
            user_min    = _parse_hhmm(user_hhmm)
            earliest    = cur_time + dh_min
            pickup_min  = max(earliest, user_min) if user_min is not None else earliest
            depart_min  = cur_time

        ride_sec = osrm_duration_sec(from_lat, from_lon, to_lat, to_lon)
        time.sleep(OSRM_SLEEP)
        dropoff_min = pickup_min + ride_sec / 60.0

        r.update({
            "start_time":  _fmt(pickup_min),
            "dh_from_lat": round(cur_lat, 6),
            "dh_from_lon": round(cur_lon, 6),
            "depart_min":  round(depart_min,  2),
            "pickup_min":  round(pickup_min,  2),
            "dropoff_min": round(dropoff_min, 2),
            "dh_sec":      round(dh_sec),
            "ride_sec":    round(ride_sec),
        })

        cur_lat, cur_lon = to_lat, to_lon
        cur_time         = dropoff_min

    rtb_sec = osrm_duration_sec(cur_lat, cur_lon, BASE_LAT, BASE_LON)
    time.sleep(OSRM_SLEEP)

    sched.update({
        "rtb_from_lat": round(cur_lat, 6),
        "rtb_from_lon": round(cur_lon, 6),
        "rtb_sec":      round(rtb_sec),
    })
    return sched

def _parse_hhmm(s: str):
    try:
        h, m = s.split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return None

# ── DynamoDB CRUD ──────────────────────────────────────────────────────────────
def save_schedule(sched: dict):
    item = _dynamo_encode({
        "config_key":   _sched_key(sched["van_id"], sched["date"]),
        "van_id":       sched["van_id"],
        "date":         sched["date"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rides":        sched["rides"],
        "rtb_from_lat": sched["rtb_from_lat"],
        "rtb_from_lon": sched["rtb_from_lon"],
        "rtb_sec":      sched["rtb_sec"],
    })
    _table().put_item(Item=item)

def get_schedule(v_id: str, date_str: str):
    resp = _table().get_item(Key={"config_key": _sched_key(v_id, date_str)})
    item = resp.get("Item")
    return _dynamo_decode(item) if item else None

def get_all_schedules(date_str: str) -> list:
    nums = get_active_vans()
    return [s for s in (get_schedule(van_id(i), date_str) for i in nums) if s]

def schedules_exist(date_str: str) -> bool:
    nums = get_active_vans()
    return bool(nums) and get_schedule(van_id(nums[0]), date_str) is not None

def delete_schedule(v_id: str, date_str: str):
    _table().delete_item(Key={"config_key": _sched_key(v_id, date_str)})

# ── Active van list ───────────────────────────────────────────────────────────
ACTIVE_VANS_KEY = "active_vans"

def get_active_vans() -> list:
    """Return sorted list of active van numbers. Initialises to 1..NUM_VANS if not set."""
    resp = _table().get_item(Key={"config_key": ACTIVE_VANS_KEY})
    item = resp.get("Item")
    if item and "van_nums" in item:
        raw = item["van_nums"]
        nums = json.loads(raw) if isinstance(raw, str) else [int(x) for x in raw]
        return sorted(nums)
    return list(range(1, NUM_VANS + 1))

def set_active_vans(nums: list):
    _table().put_item(Item={
        "config_key": ACTIVE_VANS_KEY,
        "van_nums":   json.dumps(sorted(int(n) for n in nums)),
    })

# ── Decimal conversion ────────────────────────────────────────────────────────
def _dynamo_encode(obj):
    if isinstance(obj, float):
        return Decimal(str(round(obj, 6)))
    if isinstance(obj, int):
        return obj
    if isinstance(obj, dict):
        return {k: _dynamo_encode(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_dynamo_encode(v) for v in obj]
    return obj

def _dynamo_decode(obj):
    if isinstance(obj, Decimal):
        f = float(obj)
        return int(f) if f == int(f) else f
    if isinstance(obj, dict):
        return {k: _dynamo_decode(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_dynamo_decode(v) for v in obj]
    return obj
