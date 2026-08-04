"""
Real van position ingester.

Polls the external GPS feed and records a time-series of positions in DynamoDB
for the fleet map's live playback. Invoked every minute by EventBridge.

Two modes (state persisted in the positions table under ts="STATE"):
  ACTIVE     — the van is (or was recently) moving. Poll every few seconds so
               network-delayed reports are picked up promptly; record each new
               position. After STATIONED_AFTER seconds without movement, drop to
               STATIONED.
  STATIONED  — the van is idle / onboard computer off. Do a single check per
               invocation (~once a minute) and write a heartbeat at the last
               known spot; wake back to ACTIVE the moment the van moves.

The feed's "Time" field only advances when the van moves, so a change in Time is
treated as a new movement report.
"""
import json
import math
import os
import time
from datetime import datetime, timezone
from decimal import Decimal

import boto3
import requests

REAL_VAN_URL = os.environ.get(
    "REAL_VAN_URL",
    "https://ada-van-service-data.s3.us-west-1.amazonaws.com/van_position.json",
)
TABLE_NAME = os.environ.get("REAL_VAN_TABLE", "ada-real-van-positions")
VAN_ID = os.environ.get("REAL_VAN_ID", "real-1")

POLL_SECONDS = 5              # ACTIVE poll cadence
RUN_BUDGET_SECONDS = 50       # stop looping before the next 1-min trigger
STATIONED_AFTER = 5 * 60      # no movement for 5 min => stationed
MOVE_THRESHOLD_M = 15         # min distance to count as movement
RETENTION_SECONDS = 70 * 60   # TTL horizon (~70 min)

SIM_STEP_M = 650              # waypoint spacing (~1 min of city driving)
SIM_EMIT_SECONDS = 3          # emit one waypoint this often (accelerated)

_table = boto3.resource("dynamodb").Table(TABLE_NAME)


def _haversine_m(lat1, lon1, lat2, lon2):
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _fetch():
    r = requests.get(REAL_VAN_URL, timeout=5)
    r.raise_for_status()
    d = r.json()
    lat, lon = d.get("Latitude"), d.get("Longitude")
    if lat is None or lon is None:
        return None
    return {
        "lat": float(lat), "lon": float(lon),
        "street": d.get("Street", "") or "",
        "source_time": d.get("Time", "") or "",
    }


def _get_state():
    return _table.get_item(Key={"van_id": VAN_ID, "ts": "STATE"}).get("Item") or {}


def _save_state(state):
    item = {"van_id": VAN_ID, "ts": "STATE"}
    item.update(state)
    _table.put_item(Item=item)


def _persist(updates):
    """Re-read STATE and merge updates so control flags (tracking/sim) set by a
    user during this ~50s invocation aren't clobbered by our stale copy."""
    latest = _get_state()
    latest.update(updates)
    latest["van_id"] = VAN_ID
    latest["ts"] = "STATE"
    _table.put_item(Item=latest)


def _record(reading, moving, dist_m, simulated=False, route_m=None):
    ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    item = {
        "van_id": VAN_ID,
        "ts": ts,
        "lat": Decimal(str(reading["lat"])),
        "lon": Decimal(str(reading["lon"])),
        "street": reading["street"],
        "source_time": reading["source_time"],
        "moving": bool(moving),
        "dist_m": Decimal(str(round(dist_m, 1))),
        "simulated": bool(simulated),
        "ttl": int(time.time()) + RETENTION_SECONDS,
    }
    if route_m is not None:
        item["route_m"] = Decimal(str(round(route_m, 1)))
    _table.put_item(Item=item)


def _run_simulation(state):
    """Emit the precomputed loop waypoints in order (accelerated); loops forever."""
    try:
        wps = json.loads(state.get("sim_wp") or "[]")
    except Exception:
        wps = []
    if len(wps) < 2:
        return {"sim": "on", "recorded": 0, "note": "no waypoints"}
    idx = int(float(state.get("sim_index", 0) or 0)) % len(wps)
    recorded = 0
    deadline = time.time() + RUN_BUDGET_SECONDS
    while time.time() < deadline:
        lon, lat, route_m = wps[idx][0], wps[idx][1], wps[idx][2]
        _record({"lat": lat, "lon": lon, "street": "", "source_time": ""},
                moving=True, dist_m=SIM_STEP_M, simulated=True, route_m=route_m)
        recorded += 1
        idx = (idx + 1) % len(wps)
        time.sleep(SIM_EMIT_SECONDS)
    # Persist the loop position so the next invocation continues in order.
    _persist({"sim_index": Decimal(str(idx))})
    return {"sim": "on", "recorded": recorded}


def handler(event, context):
    state = _get_state()

    # Paused: do nothing (cheap no-op invocation — the cost-saving path).
    if state.get("tracking", "running") == "paused":
        return {"tracking": "paused", "recorded": 0}

    # Simulation mode: drive the saved loop path instead of the real feed.
    if state.get("sim", "off") == "on":
        return _run_simulation(state)

    mode = state.get("mode", "active")
    last_src = state.get("last_source_time", "")
    last_move = float(state.get("last_move_epoch", time.time()))
    last_lat = float(state["last_lat"]) if state.get("last_lat") is not None else None
    last_lon = float(state["last_lon"]) if state.get("last_lon") is not None else None
    last_street = state.get("last_street", "")

    counters = {"recorded": 0}

    def process(reading):
        """Record a reading if it's a new report; return True if it was movement."""
        nonlocal last_src, last_move, last_lat, last_lon, last_street
        if not reading["source_time"] or reading["source_time"] == last_src:
            return False
        dist = (_haversine_m(last_lat, last_lon, reading["lat"], reading["lon"])
                if last_lat is not None else 0.0)
        moving = last_lat is None or dist >= MOVE_THRESHOLD_M
        _record(reading, moving, dist)
        counters["recorded"] += 1
        last_src = reading["source_time"]
        last_lat, last_lon, last_street = reading["lat"], reading["lon"], reading["street"]
        if moving:
            last_move = time.time()
        return moving

    if mode == "stationed":
        # One check per minute: wake on movement, otherwise heartbeat.
        try:
            reading = _fetch()
        except Exception:
            reading = None
        if reading and reading["source_time"] and reading["source_time"] != last_src:
            process(reading)
            mode = "active"
        elif last_lat is not None:
            _record({"lat": last_lat, "lon": last_lon, "street": last_street,
                     "source_time": last_src}, moving=False, dist_m=0.0)
    else:
        # ACTIVE: poll every few seconds to catch network-delayed reports.
        deadline = time.time() + RUN_BUDGET_SECONDS
        while time.time() < deadline:
            try:
                reading = _fetch()
                if reading:
                    process(reading)
            except Exception:
                pass
            if time.time() - last_move >= STATIONED_AFTER:
                mode = "stationed"
                break
            time.sleep(POLL_SECONDS)

    updates = {
        "mode": mode,
        "last_source_time": last_src,
        "last_move_epoch": Decimal(str(int(last_move))),
    }
    if last_lat is not None:
        updates["last_lat"] = Decimal(str(last_lat))
        updates["last_lon"] = Decimal(str(last_lon))
        updates["last_street"] = last_street
    _persist(updates)
    return {"mode": mode, "recorded": counters["recorded"]}
