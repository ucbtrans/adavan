"""
ADA Driving Assistant — Flask web application.
"""

import json
import os
import time
from datetime import datetime, timezone

import boto3
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, send_from_directory

import sessions as sess
from assistant import answer_question
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

S3_BUCKET   = os.environ.get("S3_BUCKET",   "ada-driving-assistant")
OBJECTS_KEY = os.environ.get("OBJECTS_KEY", "CA/Berkeley/city_objects.json")

# ── S3 objects cache (refresh every 5 minutes) ───────────────────────────────

_cache: dict = {"objects": [], "loaded_at": 0.0}
_CACHE_TTL   = 300   # seconds


def get_objects() -> list[dict]:
    now = time.time()
    if now - _cache["loaded_at"] > _CACHE_TTL:
        try:
            s3   = boto3.client("s3")
            resp = s3.get_object(Bucket=S3_BUCKET, Key=OBJECTS_KEY)
            _cache["objects"]   = json.loads(resp["Body"].read())
            _cache["loaded_at"] = now
        except Exception as exc:
            app.logger.warning("Could not load objects from S3: %s", exc)
    return _cache["objects"]


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

    location = {
        "address":           session["address"],
        "lat":               session["lat"],
        "lon":               session["lon"],
        "bearing":           session["bearing"],
        "bearing_direction": session["bearing_direction"],
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

    objects = get_objects()

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

    answer, usage = answer_question(question, location, nearby, history)

    sess.add_message(session_id, "user",      question)
    sess.add_message(session_id, "assistant", answer)

    # Attach computed center coords to each source so the map can place markers
    sources_with_coords = []
    for obj in nearby:
        olat, olon = object_center(obj)
        sources_with_coords.append({**obj, "_lat": olat, "_lon": olon})

    return jsonify({"answer": answer, "nearby_count": len(nearby),
                    "sources": sources_with_coords, "usage": usage})


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
    from datetime import datetime, timedelta, timezone
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
                dict(p.split("=", 1) if "=" in p else (p, "")
                     for p in qs.split("&") if p) or None
            ),
            "headers":               event.get("headers", {}),
            "body":                  event.get("body"),
            "isBase64Encoded":       event.get("isBase64Encoded", False),
        }

    def handler(event, context):
        return awsgi.response(
            app, _normalise_event(event), context,
            base64_content_types={"image/jpeg"},
        )

except ImportError:
    pass  # aws-wsgi not installed in local dev; Flask runs directly

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5001)
