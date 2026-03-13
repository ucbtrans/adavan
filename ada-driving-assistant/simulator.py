"""
Simulation engine for ADA Driving Assistant.

Generates N random traffic/road events distributed across city streets,
with randomised locations, activation timestamps (8am–8pm), and lifespans
varied ±10% from the type baseline.
"""

from __future__ import annotations

import json
import math
import os
import random
from datetime import datetime, timedelta, timezone

from objects import (
    LIFESPANS, OBJECT_TYPES,
    CAR_TYPES, CAR_COLORS, BLOCKING_OPTIONS, POLICE_DIR_OPTIONS,
    make_single_cone, make_cone_group, make_construction_zone,
    make_car_accident, make_double_parked_car, make_broken_car,
    make_road_barrier, make_dropped_object, make_protest,
    make_police_blocking,
)

# ── Street loading ───────────────────────────────────────────────────────────

_STREETS_FILE = os.path.join(os.path.dirname(__file__), "city_streets.json")

def load_streets() -> list[dict]:
    with open(_STREETS_FILE) as f:
        data = json.load(f)
    return data["streets"]


def random_point_on_street(street: dict) -> tuple[float, float]:
    """
    Pick a random position along a street by interpolating between
    two consecutive waypoints.
    """
    wps = street["waypoints"]
    if len(wps) == 1:
        return wps[0]["lat"], wps[0]["lon"]

    # Choose a random segment
    i = random.randint(0, len(wps) - 2)
    p1, p2 = wps[i], wps[i + 1]
    t = random.random()
    lat = p1["lat"] + t * (p2["lat"] - p1["lat"])
    lon = p1["lon"] + t * (p2["lon"] - p1["lon"])
    return lat, lon


# ── Timestamp helpers ────────────────────────────────────────────────────────

def random_activation(day: datetime) -> datetime:
    """Random time between 08:00 and 20:00 on the given day (UTC)."""
    start = day.replace(hour=8,  minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    end   = day.replace(hour=20, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    seconds = random.randint(0, int((end - start).total_seconds()))
    return start + timedelta(seconds=seconds)


def varied_lifespan(obj_type: str) -> timedelta:
    """Base lifespan ± uniform 10%."""
    base = LIFESPANS[obj_type]
    variation = base * 0.10
    seconds = base + random.uniform(-variation, variation)
    return timedelta(seconds=seconds)


# ── Per-type factories ───────────────────────────────────────────────────────

def _random_car() -> dict:
    return {"type": random.choice(CAR_TYPES), "color": random.choice(CAR_COLORS)}


def _build_event(obj_type: str, lat: float, lon: float,
                 active_at: datetime, inactive_at: datetime) -> dict:
    if obj_type == "single_cone":
        return make_single_cone(lat, lon, active_at, inactive_at)

    elif obj_type == "cone_group":
        num = random.randint(2, 10)
        return make_cone_group(lat, lon, num, active_at, inactive_at)

    elif obj_type == "construction_zone":
        blocking = random.choice(BLOCKING_OPTIONS)
        return make_construction_zone(lat, lon, blocking, active_at, inactive_at)

    elif obj_type == "car_accident":
        n_cars = random.randint(2, 4)
        cars = [_random_car() for _ in range(n_cars)]
        police = random.choice([True, False])
        return make_car_accident(lat, lon, cars, police, active_at, inactive_at)

    elif obj_type == "double_parked_car":
        car = _random_car()
        return make_double_parked_car(lat, lon, car["type"], car["color"], active_at, inactive_at)

    elif obj_type == "broken_car":
        car = _random_car()
        return make_broken_car(lat, lon, car["type"], car["color"], active_at, inactive_at)

    elif obj_type == "road_barrier":
        return make_road_barrier(lat, lon, active_at, inactive_at)

    elif obj_type == "dropped_object":
        return make_dropped_object(lat, lon, active_at, inactive_at)

    elif obj_type == "protest":
        return make_protest(lat, lon, active_at, inactive_at)

    elif obj_type == "police_blocking":
        directions = random.choice(POLICE_DIR_OPTIONS)
        return make_police_blocking(lat, lon, directions, active_at, inactive_at)

    else:
        raise ValueError(f"Unknown object type: {obj_type}")


# ── Public API ───────────────────────────────────────────────────────────────

def generate_events(n: int = 300, day: datetime | None = None) -> list[dict]:
    """
    Generate N random events for a single day.

    Args:
        n:   Number of events to generate.
        day: The target day (defaults to today UTC).

    Returns:
        List of event dicts ready for JSON serialisation.
    """
    if day is None:
        day = datetime.now(timezone.utc)

    streets = load_streets()
    events = []

    for _ in range(n):
        obj_type = random.choice(OBJECT_TYPES)
        street   = random.choice(streets)
        lat, lon = random_point_on_street(street)

        active_at   = random_activation(day)
        inactive_at = active_at + varied_lifespan(obj_type)

        event = _build_event(obj_type, lat, lon, active_at, inactive_at)
        event["street"] = street["name"]   # convenience field
        events.append(event)

    return events


def purge_expired(objects: list[dict], now: datetime | None = None) -> list[dict]:
    """Remove objects whose inactive_at is in the past."""
    if now is None:
        now = datetime.now(timezone.utc)
    return [
        obj for obj in objects
        if datetime.fromisoformat(obj["inactive_at"]) > now
    ]


# ── CLI helper ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Generate simulated traffic events")
    parser.add_argument("-n", type=int, default=300, help="Number of events to generate")
    parser.add_argument("--out", default="city_objects.json", help="Output JSON file")
    args = parser.parse_args()

    evts = generate_events(args.n)
    with open(args.out, "w") as f:
        json.dump(evts, f, indent=2)

    # Summary
    from collections import Counter
    counts = Counter(e["type"] for e in evts)
    print(f"Generated {len(evts)} events -> {args.out}")
    for t, c in sorted(counts.items()):
        print(f"  {t:<28} {c:>3}")
