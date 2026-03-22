"""
Location utilities: geocoding, reverse geocoding, nearby object search,
and random Berkeley location generation.
"""

from __future__ import annotations

import json
import math
import os
import random
from datetime import datetime, timezone
from math import atan2, cos, degrees, radians, sin

import requests

NOMINATIM_URL     = "https://nominatim.openstreetmap.org"
NOMINATIM_HEADERS = {"User-Agent": "ADA-Driving-Assistant/1.0 (ucbtrans)"}

_STREETS_FILE   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "city_streets.json")
_STREETS_TMP    = "/tmp/city_streets.json"
_S3_BUCKET      = os.environ.get("S3_BUCKET")
_STREETS_KEY    = os.environ.get("STREETS_KEY", "CA/Berkeley/city_streets.json")


def _streets_path() -> str:
    """Return path to city_streets.json, downloading from S3 to /tmp if needed."""
    if os.path.exists(_STREETS_FILE):
        return _STREETS_FILE
    if os.path.exists(_STREETS_TMP):
        return _STREETS_TMP
    if _S3_BUCKET:
        import boto3
        boto3.client("s3").download_file(_S3_BUCKET, _STREETS_KEY, _STREETS_TMP)
        return _STREETS_TMP
    raise FileNotFoundError(
        f"city_streets.json not found locally or in S3 bucket '{_S3_BUCKET}'"
    )


# ── Math helpers ─────────────────────────────────────────────────────────────

def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance in metres between two lat/lon points."""
    R = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a  = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def bearing_to_direction(bearing: float) -> str:
    dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return dirs[round(bearing / 45) % 8]


def compute_bearing_from_segment(p1: dict, p2: dict) -> int:
    """Forward azimuth from p1 to p2, in degrees [0, 360)."""
    lat1, lon1 = radians(p1["lat"]), radians(p1["lon"])
    lat2, lon2 = radians(p2["lat"]), radians(p2["lon"])
    dlon = lon2 - lon1
    x = sin(dlon) * cos(lat2)
    y = cos(lat1) * sin(lat2) - sin(lat1) * cos(lat2) * cos(dlon)
    return round(degrees(atan2(x, y)) % 360)


def is_oneway(street: dict) -> bool:
    return street.get("lanes_backward", 1) == 0


# ── Geocoding ────────────────────────────────────────────────────────────────

def geocode_address(address: str) -> dict | None:
    """
    Geocode an address string.
    Appends ', Berkeley, CA' if city/state not mentioned.
    Returns {lat, lon, address} or None.
    """
    query = address
    if "berkeley" not in address.lower():
        query = f"{address}, Berkeley, CA"
    try:
        r = requests.get(
            f"{NOMINATIM_URL}/search",
            params={"q": query, "format": "json", "limit": 1, "addressdetails": 1},
            headers=NOMINATIM_HEADERS,
            timeout=10,
        )
        r.raise_for_status()
        results = r.json()
        if results:
            res = results[0]
            parts = res.get("display_name", address).split(",")
            short  = ", ".join(p.strip() for p in parts[:3])
            return {
                "lat":     float(res["lat"]),
                "lon":     float(res["lon"]),
                "address": short,
            }
    except Exception as exc:
        print(f"Geocoding error: {exc}")
    return None


def reverse_geocode(lat: float, lon: float) -> str:
    """Return a short human-readable address for lat/lon."""
    try:
        r = requests.get(
            f"{NOMINATIM_URL}/reverse",
            params={"lat": lat, "lon": lon, "format": "json"},
            headers=NOMINATIM_HEADERS,
            timeout=10,
        )
        r.raise_for_status()
        data  = r.json()
        parts = data.get("display_name", "").split(",")
        return ", ".join(p.strip() for p in parts[:3])
    except Exception:
        return f"{lat:.5f}, {lon:.5f}"


# ── Random location ──────────────────────────────────────────────────────────

def random_location() -> dict:
    """
    Pick a random drivable point on a named Berkeley street.
    Returns {lat, lon, bearing, bearing_direction, heading_auto,
             heading_options (two-way only), address, street}.
    """
    with open(_streets_path()) as f:
        data = json.load(f)

    named = [s for s in data["streets"] if not s["name"].startswith("Unnamed_")]

    # Keep picking until we find a street with at least one usable segment (≥2 points).
    for _ in range(200):
        street    = random.choice(named)
        segments  = street.get("segments") or [street.get("waypoints", [])]
        usable    = [seg for seg in segments if len(seg) >= 2]
        if usable:
            break
    else:
        raise RuntimeError("No usable street segments found in city_streets.json")

    seg = random.choice(usable)
    i   = random.randint(0, len(seg) - 2)
    p1, p2 = seg[i], seg[i + 1]
    t   = random.random()
    lat = p1["lat"] + t * (p2["lat"] - p1["lat"])
    lon = p1["lon"] + t * (p2["lon"] - p1["lon"])

    fwd_bearing = compute_bearing_from_segment(p1, p2)
    address     = reverse_geocode(lat, lon)

    result = {
        "lat":               round(lat, 6),
        "lon":               round(lon, 6),
        "bearing":           fwd_bearing,
        "bearing_direction": bearing_to_direction(fwd_bearing),
        "address":           address,
        "street":            street["name"],
    }

    if is_oneway(street):
        result["heading_auto"] = True
    else:
        rev_bearing = (fwd_bearing + 180) % 360
        result["heading_auto"]    = False
        result["heading_options"] = [
            {"bearing": fwd_bearing, "direction": bearing_to_direction(fwd_bearing)},
            {"bearing": rev_bearing, "direction": bearing_to_direction(rev_bearing)},
        ]

    return result


# ── Object geometry ───────────────────────────────────────────────────────────

def object_center(obj: dict) -> tuple[float | None, float | None]:
    """Return (lat, lon) centroid of any object type."""
    if "coordinates" in obj:
        return obj["coordinates"]["lat"], obj["coordinates"]["lon"]
    if "polygon" in obj:
        pts = obj["polygon"]
        return (sum(p["lat"] for p in pts) / len(pts),
                sum(p["lon"] for p in pts) / len(pts))
    if "left_coordinates" in obj:
        lc, rc = obj["left_coordinates"], obj["right_coordinates"]
        return (lc["lat"] + rc["lat"]) / 2, (lc["lon"] + rc["lon"]) / 2
    return None, None


# ── Route corridor search ────────────────────────────────────────────────────

def _point_to_segment_dist_m(plat: float, plon: float,
                              lat1: float, lon1: float,
                              lat2: float, lon2: float) -> float:
    """Minimum distance from point P to segment (lat1,lon1)→(lat2,lon2) in metres."""
    dx = lon2 - lon1
    dy = lat2 - lat1
    if dx == 0 and dy == 0:
        return haversine_m(plat, plon, lat1, lon1)
    t = ((plon - lon1) * dx + (plat - lat1) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return haversine_m(plat, plon, lat1 + t * dy, lon1 + t * dx)


def find_objects_along_route(route_coords: list,
                              objects: list[dict],
                              corridor_m: float = 40,
                              route_streets: list | None = None) -> list[dict]:
    """
    Return active objects that are on the route, sorted by distance from origin.

    _distance_m is set to the along-route distance from the origin to the
    closest point on the polyline — NOT the perpendicular corridor distance.
    This ensures objects are ordered and labelled from origin → destination.

    Inclusion logic:
      1. Object's street is in route_streets (if provided), OR object has no
         street field and is within corridor_m of the polyline.
      2. Closest point on polyline is within max(corridor_m, 200) m of object.

    Args:
        route_coords:   [[lon, lat], ...] as returned by OSRM.
        objects:        Full city objects list.
        corridor_m:     Tight corridor for unnamed objects (default 40 m).
        route_streets:  Street names from OSRM steps.
    """
    now        = datetime.now(timezone.utc)
    street_set = {s.lower() for s in route_streets} if route_streets else None
    max_dist   = max(corridor_m, 200)

    # Precompute cumulative along-route distances for each vertex
    cum_dist = [0.0]
    for i in range(len(route_coords) - 1):
        lon1, lat1 = route_coords[i]
        lon2, lat2 = route_coords[i + 1]
        cum_dist.append(cum_dist[-1] + haversine_m(lat1, lon1, lat2, lon2))

    result = []

    for obj in objects:
        try:
            if datetime.fromisoformat(obj["active_at"])   > now:
                continue
            if datetime.fromisoformat(obj["inactive_at"]) < now:
                continue
        except Exception:
            continue

        olat, olon = object_center(obj)
        if olat is None:
            continue

        # Find the closest segment; track both perpendicular and along-route distances
        min_perp  = float("inf")
        along_at_min = 0.0

        for i in range(len(route_coords) - 1):
            lon1, lat1 = route_coords[i]
            lon2, lat2 = route_coords[i + 1]

            dx = lon2 - lon1
            dy = lat2 - lat1
            if dx == 0 and dy == 0:
                t = 0.0
            else:
                t = ((olon - lon1) * dx + (olat - lat1) * dy) / (dx * dx + dy * dy)
                t = max(0.0, min(1.0, t))

            perp = haversine_m(olat, olon, lat1 + t * dy, lon1 + t * dx)
            if perp < min_perp:
                min_perp = perp
                seg_len  = haversine_m(lat1, lon1, lat2, lon2)
                along_at_min = cum_dist[i] + t * seg_len

        if min_perp > max_dist:
            continue

        obj_street = obj.get("street", "").lower()

        if street_set:
            on_route = (obj_street and obj_street in street_set) or \
                       (not obj_street and min_perp <= corridor_m)
        else:
            on_route = min_perp <= corridor_m

        if on_route:
            result.append({**obj, "_distance_m": round(along_at_min)})

    result.sort(key=lambda x: x["_distance_m"])
    return result


# ── Street search ─────────────────────────────────────────────────────────────

def find_streets_mentioned(question: str,
                            route_streets: list | None = None) -> list[str]:
    """
    Return canonical street names (from city_streets.json) that appear in the
    question.  Includes streets that are on the route so that explicit user
    questions always trigger a fresh lookup (deduplication happens upstream).
    Uses substring matching so "Ashby Avenue" matches stored name "Ashby Ave".
    """
    try:
        with open(_streets_path()) as f:
            data = json.load(f)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("find_streets_mentioned: could not load streets: %s", exc)
        return []

    q_lower = question.lower()
    seen: dict[str, str] = {}   # lower-name → canonical name

    for street in data.get("streets", []):
        name = street.get("name", "").strip()
        if not name or name.startswith("Unnamed_"):
            continue
        nl = name.lower()
        if nl in seen:
            continue
        if nl in q_lower:
            seen[nl] = name

    return list(seen.values())


def find_street_suggestions(question: str) -> dict[str, str]:
    """
    For street-like phrases in the question that don't match any known street,
    return {typed_phrase: closest_known_street} using fuzzy matching.
    Only returns suggestions for phrases that look like street names
    (contain a street-type suffix) but weren't found by exact matching.
    """
    import difflib
    import re

    _SUFFIXES = {
        'avenue', 'ave', 'street', 'st', 'drive', 'dr', 'boulevard', 'blvd',
        'way', 'road', 'rd', 'lane', 'ln', 'court', 'ct', 'place', 'pl',
        'terrace', 'ter', 'circle', 'cir', 'run', 'loop', 'path', 'trail', 'row',
    }

    try:
        with open(_streets_path()) as f:
            data = json.load(f)
    except Exception:
        return {}

    known       = [s["name"] for s in data.get("streets", [])
                   if not s["name"].startswith("Unnamed_")]
    known_lower = [k.lower() for k in known]
    q_lower     = question.lower()

    # Streets already matched exactly — no suggestion needed
    exact_matched = {k.lower() for k in known if k.lower() in q_lower}

    # Tokenise the question; for each suffix token, build 1-word and 2-word candidates
    words = re.findall(r"[\w']+", q_lower)
    candidates: set[str] = set()
    for i, word in enumerate(words):
        clean = word.rstrip('.')
        if clean in _SUFFIXES:
            if i >= 1:
                candidates.add(' '.join(words[i - 1:i + 1]))   # 1 word + suffix
            if i >= 2:
                candidates.add(' '.join(words[i - 2:i + 1]))   # 2 words + suffix

    # raw: phrase → canonical
    raw: dict[str, str] = {}
    for phrase in candidates:
        if phrase in exact_matched:
            continue
        # Skip if the candidate contains a known exact match as a substring
        if any(em in phrase for em in exact_matched):
            continue
        close = difflib.get_close_matches(phrase, known_lower, n=1, cutoff=0.72)
        if close:
            canonical = known[known_lower.index(close[0])]
            raw[phrase] = canonical

    # Deduplicate: for each canonical keep only the shortest candidate phrase
    best: dict[str, str] = {}  # canonical → best phrase
    for phrase, canonical in raw.items():
        if canonical not in best or len(phrase) < len(best[canonical]):
            best[canonical] = phrase

    return {phrase: canonical for canonical, phrase in best.items()}


def find_objects_on_street(street_name: str, objects: list[dict]) -> list[dict]:
    """Return currently-active objects whose street field matches street_name (case-insensitive)."""
    now = datetime.now(timezone.utc)
    name_lower = street_name.lower()
    result = []
    for obj in objects:
        try:
            if datetime.fromisoformat(obj["active_at"])   > now:
                continue
            if datetime.fromisoformat(obj["inactive_at"]) < now:
                continue
        except Exception:
            continue
        if obj.get("street", "").lower() == name_lower:
            result.append(obj)
    return result


# ── Nearby search ────────────────────────────────────────────────────────────

def find_nearby_objects(lat: float, lon: float,
                        objects: list[dict],
                        radius_m: float = 500) -> list[dict]:
    """
    Return currently-active objects within radius_m metres, sorted by distance.
    Adds a '_distance_m' field to each result.
    """
    now    = datetime.now(timezone.utc)
    nearby = []

    for obj in objects:
        try:
            if datetime.fromisoformat(obj["active_at"])   > now:
                continue
            if datetime.fromisoformat(obj["inactive_at"]) < now:
                continue
        except Exception:
            continue

        olat, olon = object_center(obj)
        if olat is None:
            continue

        dist = haversine_m(lat, lon, olat, olon)
        if dist <= radius_m:
            nearby.append({**obj, "_distance_m": round(dist)})

    nearby.sort(key=lambda x: x["_distance_m"])
    return nearby
