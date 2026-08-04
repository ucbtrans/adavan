"""
ADA Driving Assistant — Flask web application.
"""

import json
import math
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import parse_qs

import boto3
import requests
from boto3.dynamodb.conditions import Key
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, send_from_directory

import sessions as sess
import schedule as sched_mod
from assistant import answer_question
from events import (clear_event, event_lat_lon, get_archive_events_for_date,
                    get_events_by_city, get_events_by_street, get_events_near, put_event)
from location import (find_nearby_objects, find_objects_on_street,
                      find_street_suggestions, find_streets_mentioned,
                      geocode_address, object_center, random_location)
from parking import get_parking_context

_PARKING_KEYWORDS = {"park", "parking", "curb", "spot"}

def _is_parking_question(q: str) -> bool:
    q_lower = q.lower()
    return any(kw in q_lower for kw in _PARKING_KEYWORDS)

load_dotenv()

app = Flask(__name__)

S3_BUCKET = os.environ.get("S3_BUCKET", "ada-driving-assistant")

# ── DynamoDB events cache (per city, refresh every 5 minutes) ────────────────

_events_cache: dict = {}   # city → (loaded_at, [events])
_CACHE_TTL = 300           # seconds

_SUPPORTED_CITIES = [
    "Berkeley", "Albany", "ElCerrito", "Richmond", "Emeryville", "Oakland",
]


def get_events_for_city(city: str) -> list[dict]:
    now = time.time()
    entry = _events_cache.get(city)
    if entry and now - entry[0] < _CACHE_TTL:
        return entry[1]
    try:
        evts = get_events_by_city(city, datetime.now(timezone.utc))
        _events_cache[city] = (now, evts)
        return evts
    except Exception as exc:
        app.logger.warning("Could not load events for %s from DynamoDB: %s", city, exc)
        return entry[1] if entry else []


def get_all_events() -> list[dict]:
    """Return active events across all supported cities (for route-based queries)."""
    result = []
    for city in _SUPPORTED_CITIES:
        result.extend(get_events_for_city(city))
    return result


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/ada_logo.jpg")
def serve_logo():
    return send_from_directory(
        os.path.dirname(os.path.abspath(__file__)),
        "ada_logo.jpg", mimetype="image/jpeg"
    )


@app.route("/van_real.png")
def serve_real_van_icon():
    return send_from_directory(
        os.path.dirname(os.path.abspath(__file__)),
        "van_real.png", mimetype="image/png"
    )


# -- Location -----------------------------------------------------------------

@app.route("/api/location/random")
def api_random_location():
    try:
        loc = random_location()
        return jsonify(loc)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/location/geocode", methods=["POST"])
def api_geocode():
    address = (request.json or {}).get("address", "").strip()
    if not address:
        return jsonify({"error": "address is required"}), 400
    result = geocode_address(address)
    if result:
        return jsonify(result)
    return jsonify({"error": f"Could not geocode: {address}"}), 404


# -- Sessions -----------------------------------------------------------------

@app.route("/api/session/list")
def api_session_list():
    return jsonify(sess.list_sessions())


@app.route("/api/session/new", methods=["POST"])
def api_session_new():
    data    = request.json or {}
    address = data.get("address", "").strip()
    lat     = data.get("lat")
    lon     = data.get("lon")
    bearing = int(data.get("bearing", 0))

    if not address or lat is None or lon is None:
        return jsonify({"error": "address, lat, and lon are required"}), 400

    from location import bearing_to_direction
    bearing_dir = bearing_to_direction(bearing)
    street      = data.get("street", "")
    destination  = data.get("destination", "")
    dest_lat     = data.get("dest_lat")
    dest_lon     = data.get("dest_lon")
    route_coords  = data.get("route_coords")    # [[lon,lat],...] from OSRM
    route_streets = data.get("route_streets")   # [str,...] street names from OSRM steps

    # Deduplicate: reuse an existing session with the same address + bearing
    existing = sess.find_session(address, bearing)
    if existing:
        sess.touch_session(existing["id"])
        return jsonify(sess.get_session(existing["id"]))

    session = sess.create_session(address, float(lat), float(lon),
                                  bearing, bearing_dir, street,
                                  destination,
                                  float(dest_lat) if dest_lat is not None else None,
                                  float(dest_lon) if dest_lon is not None else None,
                                  route_coords,
                                  route_streets)
    return jsonify(session)


@app.route("/api/session/<session_id>")
def api_session_get(session_id):
    session = sess.get_session(session_id)
    if not session:
        return jsonify({"error": "Session not found"}), 404
    return jsonify(session)


@app.route("/api/session/<session_id>/resume", methods=["POST"])
def api_session_resume(session_id):
    """Resume a previous session, updating its last_active_at timestamp."""
    session = sess.get_session(session_id)
    if not session:
        return jsonify({"error": "Session not found"}), 404
    sess.touch_session(session_id)
    session = sess.get_session(session_id)   # reload with updated timestamp
    return jsonify(session)


# -- Q&A ----------------------------------------------------------------------

@app.route("/api/ask", methods=["POST"])
def api_ask():
    data       = request.json or {}
    session_id = data.get("session_id", "").strip()
    question   = data.get("question", "").strip()

    if not session_id or not question:
        return jsonify({"error": "session_id and question are required"}), 400

    session = sess.get_session(session_id)
    if not session:
        return jsonify({"error": "Session not found"}), 404

    current_lat     = data.get("current_lat")
    current_lon     = data.get("current_lon")
    current_bearing = data.get("current_bearing")
    current_dist_m  = data.get("current_dist_m")

    if current_lat is not None and current_lon is not None:
        from location import bearing_to_direction as _btd
        loc_lat     = float(current_lat)
        loc_lon     = float(current_lon)
        loc_bearing = int(current_bearing) if current_bearing is not None else session["bearing"]
        loc_bearing_dir = _btd(loc_bearing)
    else:
        loc_lat         = session["lat"]
        loc_lon         = session["lon"]
        loc_bearing     = session["bearing"]
        loc_bearing_dir = session["bearing_direction"]

    location = {
        "address":           session["address"],
        "lat":               loc_lat,
        "lon":               loc_lon,
        "bearing":           loc_bearing,
        "bearing_direction": loc_bearing_dir,
        "destination":       session.get("destination", ""),
        "checked_streets":   [],   # filled in after off-route search
    }

    # Parking simulation — only computed when the question is about parking
    if _is_parking_question(question):
        parking = get_parking_context(
            question,
            dest_lat=session.get("dest_lat"),
            dest_lon=session.get("dest_lon"),
            fallback_lat=session["lat"],
            fallback_lon=session["lon"],
        )
        if parking:
            location["parking"] = parking

    objects = get_all_events()

    route_streets_list: list | None = None
    route_json = session.get("route_coords_json", "")
    if route_json:
        try:
            import json as _json
            from location import find_objects_along_route
            route_streets_raw = session.get("route_streets_json", "")
            route_streets_list = _json.loads(route_streets_raw) if route_streets_raw else None
            nearby = find_objects_along_route(
                _json.loads(route_json), objects, route_streets=route_streets_list
            )
        except Exception as exc:
            app.logger.warning("Route corridor filter failed: %s", exc)
            nearby = find_nearby_objects(location["lat"], location["lon"], objects)
    else:
        nearby = find_nearby_objects(location["lat"], location["lon"], objects)

    # Adjust distances from current position (not route origin) when driver has moved
    if current_dist_m is not None and float(current_dist_m) > 0:
        dist_offset = float(current_dist_m)
        adjusted = []
        for obj in nearby:
            raw = obj.get("_distance_m")
            if raw is None:
                adjusted.append(obj)
                continue
            adjusted_dist = raw - dist_offset
            if adjusted_dist < -50:   # object is >50 m behind current position — skip
                continue
            adjusted.append({**obj, "_distance_m": round(max(0, adjusted_dist))})
        nearby = adjusted

    # Merge objects from any off-route streets the user explicitly asked about
    mentioned = find_streets_mentioned(question, route_streets_list)
    if not mentioned:
        suggestions = find_street_suggestions(question)
        if suggestions:
            location["street_suggestions"] = suggestions
    if mentioned:
        location["checked_streets"] = mentioned   # tell the AI which streets we looked up
        route_set_lower = {s.lower() for s in route_streets_list} if route_streets_list else set()
        nearby_keys: set = set()
        for obj in nearby:
            olat, olon = object_center(obj)
            if olat is not None:
                nearby_keys.add((obj.get("type"), round(olat, 5), round(olon, 5)))
        for street in mentioned:
            is_on_route = street.lower() in route_set_lower
            for obj in find_objects_on_street(street, objects):
                olat, olon = object_center(obj)
                key = (obj.get("type"), round(olat or 0, 5), round(olon or 0, 5))
                if key not in nearby_keys:
                    # Only tag as off-route if the street isn't part of the planned route
                    tagged = {**obj} if is_on_route else {**obj, "_off_route": True}
                    nearby.append(tagged)
                    nearby_keys.add(key)

    history       = sess.get_history(session_id, session=session)

    try:
        answer, usage = answer_question(question, location, nearby, history)
    except Exception as exc:
        app.logger.error("Anthropic API error: %s", exc)
        return jsonify({"error": f"AI service error: {exc}"}), 503

    sess.add_message(session_id, "user",      question)
    sess.add_message(session_id, "assistant", answer)

    # Attach computed center coords to each source so the map can place markers
    sources_with_coords = []
    for obj in nearby:
        olat, olon = object_center(obj)
        sources_with_coords.append({**obj, "_lat": olat, "_lon": olon})

    return jsonify({"answer": answer, "nearby_count": len(nearby),
                    "sources": sources_with_coords, "usage": usage})


# -- Events (van fleet API) ---------------------------------------------------

def _haversine_m(lat1, lon1, lat2, lon2):
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


@app.route("/api/events", methods=["POST"])
def api_events_create():
    """Van reports a new event for its current block."""
    data = request.json or {}
    required = ("type", "lat", "lon", "street", "city", "active_at", "inactive_at")
    for field in required:
        if not data.get(field):
            return jsonify({"error": f"{field} is required"}), 400

    now_iso = datetime.now(timezone.utc).isoformat()
    active_at   = data["active_at"]
    inactive_at = data["inactive_at"]

    # active_at must not be more than 60 s in the past, inactive_at must be after active_at
    try:
        t_active   = datetime.fromisoformat(active_at.replace("Z", "+00:00"))
        t_inactive = datetime.fromisoformat(inactive_at.replace("Z", "+00:00"))
        t_now      = datetime.now(timezone.utc)
        if t_active < t_now.replace(second=0, microsecond=0) - timedelta(seconds=60):
            return jsonify({"error": "active_at cannot be in the past"}), 400
        if t_inactive <= t_active:
            return jsonify({"error": "inactive_at must be after active_at"}), 400
    except ValueError:
        return jsonify({"error": "invalid ISO timestamp"}), 400

    ev = {
        "event_id":   str(uuid.uuid4()),
        "type":       data["type"],
        "lat":        float(data["lat"]),
        "lon":        float(data["lon"]),
        "street":     data["street"],
        "city":       data["city"],
        "van_id":     data.get("van_id", ""),
        "active_at":  active_at,
        "inactive_at": inactive_at,
    }
    try:
        put_event(ev)
        # Invalidate city cache so next /api/ask sees the new event
        _events_cache.pop(data["city"], None)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    return jsonify({"event_id": ev["event_id"]})


@app.route("/api/events/block")
def api_events_block():
    """Return active events near a specific street block."""
    street    = request.args.get("street", "").strip()
    lat       = request.args.get("lat")
    lon       = request.args.get("lon")
    radius_m  = float(request.args.get("radius", 150))

    if not street or lat is None or lon is None:
        return jsonify({"error": "street, lat, and lon are required"}), 400

    try:
        lat = float(lat)
        lon = float(lon)
    except ValueError:
        return jsonify({"error": "lat and lon must be numeric"}), 400

    now = datetime.now(timezone.utc)
    try:
        evts = get_events_by_street(street, now)
        # Further filter by haversine distance
        evts = [e for e in evts if _haversine_m(lat, lon, e["lat"], e["lon"]) <= radius_m]
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    return jsonify({"events": evts})


@app.route("/api/events/<event_id>/clear", methods=["POST"])
def api_events_clear(event_id):
    """Van reports it has observed and cleared an event."""
    data   = request.json or {}
    street = data.get("street", "").strip()
    if not street:
        return jsonify({"error": "street is required"}), 400

    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        clear_event(street, event_id, now_iso)
        # Invalidate all city caches (we don't know which city this event belongs to)
        _events_cache.clear()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    return jsonify({"cleared": True})


@app.route("/api/events")
def api_events_list():
    """Return all active events across all cities (fleet map initial load)."""
    city = request.args.get("city")
    now  = datetime.now(timezone.utc)
    try:
        evts = get_events_by_city(city, now) if city else get_all_events()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify({"events": evts})


@app.route("/api/fleet/events")
def api_fleet_events():
    """Return active events from DynamoDB for the fleet map initial marker load."""
    now = datetime.now(timezone.utc)
    try:
        evts = get_all_events()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify({"events": evts})


# -- Consumption ---------------------------------------------------------------

_LAMBDA_FUNCTIONS = [
    {"name": "ada-api",        "memory_mb": 512},
    {"name": "ada-simulation", "memory_mb": 512},
]

_DDB_TABLE = "ada-sessions"


def _cw_metric_sum(cw, resource_name: str, metric: str,
                   start, end, period: int,
                   namespace: str = "AWS/Lambda",
                   dimension_name: str = "FunctionName") -> float | None:
    """Return the Sum of a CloudWatch metric over [start, end]."""
    try:
        resp = cw.get_metric_statistics(
            Namespace=namespace,
            MetricName=metric,
            Dimensions=[{"Name": dimension_name, "Value": resource_name}],
            StartTime=start,
            EndTime=end,
            Period=period,
            Statistics=["Sum"],
        )
        dps = resp.get("Datapoints", [])
        return round(sum(dp["Sum"] for dp in dps), 2) if dps else None
    except Exception:
        return None


@app.route("/api/consumption")
def api_consumption():
    """Return Lambda CloudWatch metrics and model info for the consumption page."""
    import boto3

    session_start_str = request.args.get("session_start", "")
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION", "us-west-2")

    now = datetime.now(timezone.utc)
    try:
        session_start = datetime.fromisoformat(
            session_start_str.replace("Z", "+00:00")
        ) if session_start_str else now - timedelta(hours=1)
    except Exception:
        session_start = now - timedelta(hours=1)

    # (label, start_time)
    periods = [
        ("session", session_start),
        ("24h",     now - timedelta(hours=24)),
        ("7d",      now - timedelta(days=7)),
        ("30d",     now - timedelta(days=30)),
    ]

    try:
        cw = boto3.client("cloudwatch", region_name=region)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    result = {}
    for func in _LAMBDA_FUNCTIONS:
        fn  = func["name"]
        mem = func["memory_mb"]
        result[fn] = {"memory_mb": mem}

        for period_name, start_time in periods:
            span = max(60.0, (now - start_time).total_seconds())
            # Choose CloudWatch granularity (must be multiple of 60)
            if span <= 3_600:
                cw_period = 60
            elif span <= 86_400:
                cw_period = 300
            elif span <= 604_800:
                cw_period = 3_600
            else:
                cw_period = 86_400

            result[fn][period_name] = {
                m: _cw_metric_sum(cw, fn, m, start_time, now, cw_period)
                for m in ("Invocations", "Duration", "Errors")
            }

    # DynamoDB metrics
    result[_DDB_TABLE] = {"type": "dynamodb"}
    for period_name, start_time in periods:
        span = max(60.0, (now - start_time).total_seconds())
        if span <= 3_600:
            cw_period = 60
        elif span <= 86_400:
            cw_period = 300
        elif span <= 604_800:
            cw_period = 3_600
        else:
            cw_period = 86_400

        result[_DDB_TABLE][period_name] = {
            m: _cw_metric_sum(cw, _DDB_TABLE, m, start_time, now, cw_period,
                              namespace="AWS/DynamoDB",
                              dimension_name="TableName")
            for m in ("ConsumedReadCapacityUnits", "ConsumedWriteCapacityUnits")
        }

    return jsonify(result)


# -- Fleet ---------------------------------------------------------------

FLEET_TABLE     = os.environ.get("FLEET_TABLE", "ada-fleet-config")
FLEET_CONFIG_KEY = "fleet_vans"

_fleet_ddb_table = None

def _get_fleet_table():
    global _fleet_ddb_table
    if _fleet_ddb_table is None:
        _fleet_ddb_table = boto3.resource("dynamodb").Table(FLEET_TABLE)
    return _fleet_ddb_table


@app.route("/api/fleet/vans")
def api_fleet_get():
    """GET /api/fleet/vans — return active van numbers list."""
    try:
        nums = sched_mod.get_active_vans()
        return jsonify({"vans": nums})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# Live position feed for the real (physical) van. Server-side proxy so the
# browser isn't blocked by the source bucket's missing CORS headers.
REAL_VAN_URL = os.environ.get(
    "REAL_VAN_URL",
    "https://ada-van-service-data.s3.us-west-1.amazonaws.com/van_position.json",
)


@app.route("/api/fleet/real-van")
def api_fleet_real_van():
    """GET /api/fleet/real-van — current GPS position of the real van."""
    try:
        resp = requests.get(REAL_VAN_URL, timeout=5)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        return jsonify({"error": f"upstream fetch failed: {exc}"}), 502
    lat = data.get("Latitude")
    lon = data.get("Longitude")
    if lat is None or lon is None:
        return jsonify({"error": "position unavailable"}), 404
    return jsonify({
        "lat":    lat,
        "lon":    lon,
        "street": data.get("Street", ""),
        "time":   data.get("Time", ""),
    })


REAL_VAN_TABLE = os.environ.get("REAL_VAN_TABLE", "ada-real-van-positions")


@app.route("/api/fleet/real-van/history")
def api_fleet_real_van_history():
    """GET /api/fleet/real-van/history?van_id=&mins= — recorded position track."""
    van_id = request.args.get("van_id", "real-1")
    try:
        mins = max(1, min(180, int(request.args.get("mins", 60))))
    except ValueError:
        mins = 60
    from_ts = (datetime.now(timezone.utc) - timedelta(minutes=mins)
               ).isoformat().replace("+00:00", "Z")
    try:
        table = boto3.resource("dynamodb").Table(REAL_VAN_TABLE)
        resp = table.query(
            KeyConditionExpression=Key("van_id").eq(van_id)
            & Key("ts").between(from_ts, "99999999"),
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    points = []
    for it in resp.get("Items", []):
        if it.get("ts") == "STATE":
            continue
        points.append({
            "ts":      it["ts"],
            "lat":     float(it["lat"]),
            "lon":     float(it["lon"]),
            "street":  it.get("street", ""),
            "moving":  bool(it.get("moving", False)),
            "dist_m":  float(it.get("dist_m", 0)),
            "route_m": (float(it["route_m"]) if "route_m" in it else None),
        })
    return jsonify({"van_id": van_id, "points": points})


# ── Real van tracking / simulation control ────────────────────────────────────
# Fleet base — matches FLEET_BASE_LAT/LON in the frontend.
REAL_VAN_BASE_LAT = 37.8897
REAL_VAN_BASE_LON = -122.3024
SIM_STEP_M = 650   # ~1 min of city driving between precomputed waypoints


def _real_van_table():
    return boto3.resource("dynamodb").Table(REAL_VAN_TABLE)


def _get_real_van_state(van_id):
    return _real_van_table().get_item(
        Key={"van_id": van_id, "ts": "STATE"}).get("Item") or {}


def _osrm_routes(lat1, lon1, lat2, lon2, alternatives=False):
    """Return a list of routes, each a list of [lon, lat] coords, via OSRM."""
    alt = "true" if alternatives else "false"
    url = (f"https://router.project-osrm.org/route/v1/driving/"
           f"{lon1},{lat1};{lon2},{lat2}"
           f"?overview=full&geometries=geojson&alternatives={alt}")
    r = requests.get(url, timeout=8)
    r.raise_for_status()
    data = r.json()
    return [rt["geometry"]["coordinates"] for rt in data.get("routes", [])]


def _hav_m(lat1, lon1, lat2, lon2):
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _coords_len_m(coords):
    return sum(_hav_m(coords[i][1], coords[i][0], coords[i + 1][1], coords[i + 1][0])
               for i in range(len(coords) - 1))


def _precompute_waypoints(coords, step_m):
    """Sample [lon, lat, route_m] every ~step_m along the polyline, in order."""
    if len(coords) < 2:
        return []
    wps = [[coords[0][0], coords[0][1], 0.0]]
    walked = 0.0
    next_mark = step_m
    for i in range(len(coords) - 1):
        lo1, la1 = coords[i]
        lo2, la2 = coords[i + 1]
        seg = _hav_m(la1, lo1, la2, lo2)
        while seg > 0 and next_mark <= walked + seg:
            t = (next_mark - walked) / seg
            wps.append([lo1 + t * (lo2 - lo1), la1 + t * (la2 - la1), round(next_mark, 1)])
            next_mark += step_m
        walked += seg
    wps.append([coords[-1][0], coords[-1][1], round(walked, 1)])
    return wps


def _overlap_fraction(route, ref):
    """Fraction of `route` points that sit ~on top of `ref` (rounded ~30m grid)."""
    ref_grid = {(round(la, 3), round(lo, 3)) for lo, la in ref}  # ~110m cells
    if not route:
        return 1.0
    hits = sum(1 for lo, la in route if (round(la, 3), round(lo, 3)) in ref_grid)
    return hits / len(route)


def _build_loop_path(cur_lat, cur_lon):
    """current -> base, then base -> current via the most different route."""
    out = _osrm_routes(cur_lat, cur_lon, REAL_VAN_BASE_LAT, REAL_VAN_BASE_LON)
    outbound = out[0] if out else [[cur_lon, cur_lat], [REAL_VAN_BASE_LON, REAL_VAN_BASE_LAT]]
    backs = _osrm_routes(REAL_VAN_BASE_LAT, REAL_VAN_BASE_LON, cur_lat, cur_lon,
                         alternatives=True)
    if not backs:
        backs = [[[REAL_VAN_BASE_LON, REAL_VAN_BASE_LAT], [cur_lon, cur_lat]]]
    # Prefer the return route that least overlaps the outbound leg.
    return_leg = min(backs, key=lambda rt: _overlap_fraction(rt, outbound))
    loop = outbound + return_leg
    return loop, _coords_len_m(loop)


@app.route("/api/fleet/real-van/status", methods=["GET"])
def api_fleet_real_van_status():
    """GET current tracking/simulation flags for a real van."""
    van_id = request.args.get("van_id", "real-1")
    st = _get_real_van_state(van_id)
    return jsonify({
        "van_id":   van_id,
        "tracking": st.get("tracking", "running"),
        "sim":      st.get("sim", "off"),
    })


@app.route("/api/fleet/real-van/status", methods=["POST"])
def api_fleet_real_van_status_set():
    """Update tracking/simulation flags. action: pause|resume|sim_start|sim_stop."""
    body = request.json or {}
    van_id = body.get("van_id", "real-1")
    action = body.get("action", "")
    table = _real_van_table()
    st = _get_real_van_state(van_id)
    updates = {}

    if action == "pause":
        # Pausing also stops any running simulation.
        updates = {"tracking": "paused", "sim": "off"}
    elif action == "resume":
        updates = {"tracking": "running"}
        # Instant move: capture the current live position right away.
        try:
            resp = requests.get(REAL_VAN_URL, timeout=5)
            d = resp.json()
            lat, lon = d.get("Latitude"), d.get("Longitude")
            if lat is not None and lon is not None:
                now = datetime.now(timezone.utc)
                table.put_item(Item={
                    "van_id": van_id,
                    "ts": now.isoformat().replace("+00:00", "Z"),
                    "lat": Decimal(str(lat)), "lon": Decimal(str(lon)),
                    "street": d.get("Street", "") or "",
                    "source_time": d.get("Time", "") or "",
                    "moving": False, "dist_m": Decimal("0"),
                    "ttl": int(time.time()) + 70 * 60,
                })
        except Exception:
            pass
    elif action == "sim_start":
        # Starting a simulation forces tracking on.
        cur_lat = float(st.get("last_lat", REAL_VAN_BASE_LAT))
        cur_lon = float(st.get("last_lon", REAL_VAN_BASE_LON))
        try:
            loop, total = _build_loop_path(cur_lat, cur_lon)
            waypoints = _precompute_waypoints(loop, SIM_STEP_M)
        except Exception as exc:
            return jsonify({"error": f"loop path build failed: {exc}"}), 502
        updates = {
            "sim": "on", "tracking": "running", "sim_index": Decimal("0"),
            "sim_path": json.dumps(loop, separators=(",", ":")),
            "sim_total_m": Decimal(str(round(total, 1))),
            "sim_wp": json.dumps(waypoints, separators=(",", ":")),
        }
    elif action == "sim_stop":
        updates = {"sim": "off"}
    else:
        return jsonify({"error": f"unknown action: {action}"}), 400

    # Merge onto the existing STATE item (preserve mode/last_* fields).
    item = dict(st)
    item["van_id"] = van_id
    item["ts"] = "STATE"
    item.update(updates)
    table.put_item(Item=item)
    return jsonify({
        "van_id":   van_id,
        "tracking": item.get("tracking", "running"),
        "sim":      item.get("sim", "off"),
    })


@app.route("/api/fleet/real-van/sim-path")
def api_fleet_real_van_sim_path():
    """GET the current simulation loop polyline (for drawing on the map)."""
    van_id = request.args.get("van_id", "real-1")
    st = _get_real_van_state(van_id)
    try:
        path = json.loads(st.get("sim_path") or "[]")
    except Exception:
        path = []
    return jsonify({"van_id": van_id, "path": path,
                    "total_m": float(st.get("sim_total_m", 0) or 0)})


@app.route("/api/fleet/vans", methods=["POST"])
def api_fleet_save():
    """Save fleet van configuration to DynamoDB."""
    data = request.json
    if not isinstance(data, list):
        return jsonify({"error": "expected a JSON array"}), 400
    try:
        _get_fleet_table().put_item(Item={
            "config_key":   FLEET_CONFIG_KEY,
            "config_value": json.dumps(data, separators=(",", ":")),
        })
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ── Fleet schedule endpoints ──────────────────────────────────────────────────

@app.route("/api/fleet/schedule")
def api_fleet_schedule_get():
    """GET /api/fleet/schedule?date=YYYY-MM-DD — return all van schedules, auto-generating if missing."""
    date_str = request.args.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    try:
        schedules = sched_mod.get_all_schedules(date_str)
        if not schedules:
            pool = _load_address_pool()
            if pool:
                for i in sched_mod.get_active_vans():
                    v_id = sched_mod.van_id(i)
                    try:
                        s = sched_mod.generate_van_schedule(v_id, date_str, pool)
                        sched_mod.save_schedule(s)
                    except Exception:
                        pass
                schedules = sched_mod.get_all_schedules(date_str)
        return jsonify({"date": date_str, "schedules": schedules})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/fleet/schedule/<van_id>", methods=["PUT"])
def api_fleet_schedule_save(van_id: str):
    """PUT /api/fleet/schedule/VAN_01 — save (and recompute) a van schedule."""
    body = request.json or {}
    date_str = body.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    rides    = body.get("rides", [])
    recompute = body.get("recompute", False)

    # Geocode any rides that lack lat/lon
    for r in rides:
        if not r.get("from_lat") and r.get("from_address"):
            lat, lon = sched_mod.geocode(r["from_address"])
            if lat: r["from_lat"], r["from_lon"] = lat, lon
        if not r.get("to_lat") and r.get("to_address"):
            lat, lon = sched_mod.geocode(r["to_address"])
            if lat: r["to_lat"], r["to_lon"] = lat, lon

    # Merge into existing or create fresh shell
    existing = sched_mod.get_schedule(van_id, date_str) or {
        "van_id": van_id, "date": date_str,
        "rtb_from_lat": sched_mod.BASE_LAT, "rtb_from_lon": sched_mod.BASE_LON,
        "rtb_sec": 0,
    }
    existing["rides"] = rides

    if recompute:
        try:
            existing = sched_mod.recompute_schedule_timing(existing)
        except Exception as exc:
            return jsonify({"error": f"recompute failed: {exc}"}), 500

    try:
        sched_mod.save_schedule(existing)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    # Save to schedule history (source field distinguishes csv_upload from manual edits)
    source = body.get("source", "edit")
    event_type = "csv_upload" if source == "csv" else "edited"
    try:
        now_pt     = datetime.now(timezone.utc) - timedelta(hours=7)
        now_pt_min = now_pt.hour * 60 + now_pt.minute
        completed  = sched_mod.compute_completed_rides(existing, now_pt_min)
        first_pending = next(
            (r["seq"] for r in existing.get("rides", []) if r["seq"] not in completed), None
        )
        sched_mod.save_schedule_history(
            van_id=van_id, date_str=date_str, event_type=event_type,
            schedule=existing, completed_rides=completed,
            active_from_ride_seq=first_pending,
        )
    except Exception:
        pass

    return jsonify({"ok": True, "schedule": existing})


@app.route("/api/fleet/schedule/generate", methods=["POST"])
def api_fleet_schedule_generate():
    """POST /api/fleet/schedule/generate — generate random schedules for a date."""
    body     = request.json or {}
    date_str = body.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    force    = body.get("force", False)

    if not force and sched_mod.schedules_exist(date_str):
        return jsonify({"ok": True, "skipped": True, "reason": "schedules already exist"})

    # Load address pool from S3 or local file
    pool = _load_address_pool()
    if not pool:
        return jsonify({"error": "address pool unavailable"}), 500

    generated, errors = [], []
    for i in sched_mod.get_active_vans():
        v_id = sched_mod.van_id(i)
        try:
            s = sched_mod.generate_van_schedule(v_id, date_str, pool)
            sched_mod.save_schedule(s)
            generated.append(v_id)
        except Exception as exc:
            errors.append({"van_id": v_id, "error": str(exc)})

    return jsonify({"ok": True, "date": date_str,
                    "generated": generated, "errors": errors})


@app.route("/api/fleet/van", methods=["POST"])
def api_fleet_van_create():
    """POST /api/fleet/van — pick a number, generate schedule, find next ride position."""
    body     = request.json or {}
    date_str = body.get("date", _pt_date_str())

    active_nums = sched_mod.get_active_vans()
    max_num     = max(active_nums) if active_nums else 0
    used        = set(active_nums)
    gaps        = [n for n in range(1, max_num + 1) if n not in used]
    new_num     = gaps[0] if gaps else max_num + 1
    v_id        = sched_mod.van_id(new_num)

    pool = _load_address_pool()
    if not pool:
        return jsonify({"error": "address pool unavailable"}), 500

    try:
        sched = sched_mod.generate_van_schedule(v_id, date_str, pool)
        sched_mod.save_schedule(sched)
    except Exception as exc:
        return jsonify({"error": f"schedule generation failed: {exc}"}), 500

    active_nums.append(new_num)
    sched_mod.set_active_vans(active_nums)

    now_pt = datetime.now(timezone.utc) - timedelta(hours=7)
    now_pt_min = now_pt.hour * 60 + now_pt.minute
    rides      = sched.get("rides", [])
    next_ride  = next((r for r in rides if r.get("pickup_min", 9999) >= now_pt_min), None)

    if next_ride:
        start_lat = next_ride["from_lat"]
        start_lon = next_ride["from_lon"]
        msg = f"{v_id} added, positioned at Ride {next_ride['seq']} starting {next_ride['start_time']}"
    else:
        start_lat = sched_mod.BASE_LAT
        start_lon = sched_mod.BASE_LON
        msg = f"{v_id} added at base — no rides remaining for today"

    # Save to schedule history
    try:
        sched_mod.save_schedule_history(
            van_id=v_id, date_str=date_str, event_type="created",
            schedule=sched, completed_rides=[],
            active_from_ride_seq=next_ride["seq"] if next_ride else None,
        )
    except Exception:
        pass

    return jsonify({
        "ok":            True,
        "van_id":        v_id,
        "van_num":       new_num,
        "schedule":      sched,
        "start_lat":     start_lat,
        "start_lon":     start_lon,
        "next_ride_seq": next_ride["seq"] if next_ride else None,
        "message":       msg,
    })


@app.route("/api/fleet/van/<van_id>", methods=["DELETE"])
def api_fleet_van_delete(van_id: str):
    """DELETE /api/fleet/van/VAN_03?date=YYYY-MM-DD — remove van + delete its schedule."""
    date_str = request.args.get("date", _pt_date_str())
    try:
        van_num = int(van_id.replace("VAN_", ""))
    except ValueError:
        return jsonify({"error": f"invalid van_id: {van_id}"}), 400

    # Save history entry before deleting: record completed rides + deletion timestamp
    try:
        existing = sched_mod.get_schedule(van_id, date_str)
        if existing:
            now_pt     = datetime.now(timezone.utc) - timedelta(hours=7)
            now_pt_min = now_pt.hour * 60 + now_pt.minute
            completed  = sched_mod.compute_completed_rides(existing, now_pt_min)
            now_iso    = datetime.now(timezone.utc).isoformat()
            sched_mod.save_schedule_history(
                van_id=van_id, date_str=date_str, event_type="deleted",
                schedule=existing, completed_rides=completed,
                active_from_ride_seq=None, deletion_timestamp=now_iso,
            )
    except Exception:
        pass

    try:
        sched_mod.delete_schedule(van_id, date_str)
    except Exception as exc:
        return jsonify({"error": f"delete schedule failed: {exc}"}), 500

    active_nums = sched_mod.get_active_vans()
    if van_num in active_nums:
        active_nums.remove(van_num)
        sched_mod.set_active_vans(active_nums)

    return jsonify({"ok": True, "van_id": van_id})


def _pt_date_str() -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=7)).strftime("%Y-%m-%d")


@app.route("/api/fleet/replay")
def api_fleet_replay():
    """GET /api/fleet/replay?date=YYYY-MM-DD&hour=H — return full replay payload for a past date."""
    date_str = request.args.get("date", "")
    try:
        hour = int(request.args.get("hour", 0))
    except ValueError:
        return jsonify({"error": "hour must be 0-23"}), 400

    if not date_str:
        return jsonify({"error": "date required"}), 400

    try:
        req_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"error": "date must be YYYY-MM-DD"}), 400

    pt_now  = datetime.now(timezone.utc) - timedelta(hours=7)
    pt_date = pt_now.date()
    delta   = (pt_date - req_date).days
    if delta < 0 or delta > 7:
        return jsonify({"error": "date must be within last 7 days"}), 400

    try:
        history_by_van = sched_mod.get_schedule_history_for_date(date_str)
        archive_events = get_archive_events_for_date(date_str)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    schedules = [{"van_id": v, "history": h} for v, h in history_by_van.items()]
    return jsonify({
        "date":      date_str,
        "hour":      hour,
        "schedules": schedules,
        "events":    archive_events,
    })


@app.route("/api/fleet/transit")
def api_fleet_transit():
    """GET /api/fleet/transit?from_lat&from_lon&to_lat&to_lon — OSRM transit time."""
    try:
        from_lat = float(request.args["from_lat"])
        from_lon = float(request.args["from_lon"])
        to_lat   = float(request.args["to_lat"])
        to_lon   = float(request.args["to_lon"])
    except (KeyError, ValueError):
        return jsonify({"error": "from_lat, from_lon, to_lat, to_lon required"}), 400
    try:
        sec = sched_mod.osrm_duration_sec(from_lat, from_lon, to_lat, to_lon)
        return jsonify({"duration_sec": round(sec), "duration_min": round(sec / 60, 1)})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/geocode")
def api_geocode_proxy():
    """GET /api/geocode?q=address — Nominatim proxy (avoids browser CORS issues)."""
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"error": "q required"}), 400
    lat, lon = sched_mod.geocode(q)
    if lat is None:
        return jsonify({"error": "not found"}), 404
    return jsonify({"lat": lat, "lon": lon, "address": q})


# ── Address pool loader ───────────────────────────────────────────────────────
_address_pool_cache = None

def _load_address_pool() -> list:
    global _address_pool_cache
    if _address_pool_cache:
        return _address_pool_cache

    # Try S3 first (deployed), then local file
    bucket = os.environ.get("S3_BUCKET", "")
    if bucket:
        try:
            import boto3 as b3
            obj = b3.client("s3").get_object(Bucket=bucket, Key="addresses_pool.json")
            pool = json.loads(obj["Body"].read())
            _address_pool_cache = [{"address": p["address"], "lat": p["lat"], "lon": p["lon"]} for p in pool]
            return _address_pool_cache
        except Exception:
            pass

    # Local fallback
    local = os.path.join(os.path.dirname(__file__), "addresses_pool.json")
    if os.path.exists(local):
        with open(local, encoding="utf-8") as f:
            pool = json.load(f)
        _address_pool_cache = [{"address": p["address"], "lat": p["lat"], "lon": p["lon"]} for p in pool]
        return _address_pool_cache

    return []


# ── Lambda entry point (aws-wsgi bridges Flask WSGI → API Gateway) ───────────
try:
    import awsgi

    def _normalise_event(event: dict) -> dict:
        """Convert HTTP API v2 event to REST API v1 format that aws-wsgi expects."""
        if "httpMethod" in event:
            return event  # already v1
        rc    = event.get("requestContext", {})
        http  = rc.get("http", {})
        stage = rc.get("stage", "")
        qs    = event.get("rawQueryString", "")
        # HTTP API v2 includes the stage in rawPath — strip it so Flask routes match
        path  = http.get("path", "/")
        if stage and stage != "$default" and path.startswith(f"/{stage}"):
            path = path[len(f"/{stage}"):] or "/"
        return {
            "httpMethod":            http.get("method", "GET"),
            "path":                  path,
            "queryStringParameters": (
                # parse_qs decodes + as space and handles %xx, then flatten to single values
                {k: v[0] for k, v in parse_qs(qs, keep_blank_values=True).items()} or None
            ),
            "headers":               event.get("headers", {}),
            "body":                  event.get("body"),
            "isBase64Encoded":       event.get("isBase64Encoded", False),
        }

    def handler(event, context):
        resp = awsgi.response(
            app, _normalise_event(event), context,
            base64_content_types={"image/jpeg"},
        )
        # HTTP API Gateway requires statusCode as int; aws-wsgi returns it as str
        if isinstance(resp.get("statusCode"), str):
            resp["statusCode"] = int(resp["statusCode"])
        return resp

except ImportError:
    pass  # aws-wsgi not installed in local dev; Flask runs directly

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5001)
