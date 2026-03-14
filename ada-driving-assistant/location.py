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

import requests

NOMINATIM_URL     = "https://nominatim.openstreetmap.org"
NOMINATIM_HEADERS = {"User-Agent": "ADA-Driving-Assistant/1.0 (ucbtrans)"}

_STREETS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "city_streets.json")


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
    Returns {lat, lon, bearing, bearing_direction, address, street}.
    """
    with open(_STREETS_FILE) as f:
        data = json.load(f)

    named = [s for s in data["streets"] if not s["name"].startswith("Unnamed_")]
    street = random.choice(named)

    segments  = street.get("segments") or [street.get("waypoints", [])]
    non_empty = [seg for seg in segments if len(seg) >= 2]
    seg       = random.choice(non_empty) if non_empty else segments[0]

    i  = random.randint(0, len(seg) - 2)
    p1, p2 = seg[i], seg[i + 1]
    t  = random.random()
    lat = p1["lat"] + t * (p2["lat"] - p1["lat"])
    lon = p1["lon"] + t * (p2["lon"] - p1["lon"])

    bearing = random.randint(0, 359)
    address = reverse_geocode(lat, lon)

    return {
        "lat":               round(lat, 6),
        "lon":               round(lon, 6),
        "bearing":           bearing,
        "bearing_direction": bearing_to_direction(bearing),
        "address":           address,
        "street":            street["name"],
    }


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
