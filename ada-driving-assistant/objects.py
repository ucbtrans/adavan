"""
Object type definitions for the ADA Driving Assistant simulation.

Each object has common base attributes plus type-specific fields.
"""

from __future__ import annotations
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# ── Lifespan constants (seconds) ────────────────────────────────────────────

LIFESPANS = {
    "single_cone":        1 * 24 * 3600,   # 1 day
    "cone_group":         2 * 24 * 3600,   # 2 days
    "construction_zone":  7 * 24 * 3600,   # 1 week
    "car_accident":       3 * 3600,        # 3 hours
    "double_parked_car":  1 * 3600,        # 1 hour
    "broken_car":         2 * 3600,        # 2 hours
    "road_barrier":       3 * 24 * 3600,   # 3 days
    "dropped_object":     1 * 24 * 3600,   # 1 day
    "protest":            4 * 3600,        # 4 hours
    "police_blocking":    3 * 3600,        # 3 hours
}

OBJECT_TYPES = list(LIFESPANS.keys())

CAR_TYPES  = ["sedan", "SUV", "pickup truck", "van", "coupe", "hatchback", "minivan"]
CAR_COLORS = ["white", "black", "silver", "red", "blue", "gray", "green", "yellow", "orange"]

BLOCKING_OPTIONS = ["one_lane", "all_lanes_one_direction", "entire_street"]
POLICE_DIR_OPTIONS = ["one_direction", "both_directions"]


# ── Coordinate helpers ───────────────────────────────────────────────────────

def make_point(lat: float, lon: float) -> dict:
    return {"lat": lat, "lon": lon}


def make_rect_polygon(lat: float, lon: float,
                      half_w_m: float = 4.0,
                      half_l_m: float = 8.0) -> list[dict]:
    """Return a 4-point rectangle polygon centred on (lat, lon)."""
    dlat = half_l_m / 111_000
    dlon = half_w_m / (111_000 * _cos_lat(lat))
    return [
        make_point(lat - dlat, lon - dlon),
        make_point(lat - dlat, lon + dlon),
        make_point(lat + dlat, lon + dlon),
        make_point(lat + dlat, lon - dlon),
    ]


def _cos_lat(lat: float) -> float:
    import math
    return max(math.cos(math.radians(lat)), 1e-9)


# ── Object constructors ──────────────────────────────────────────────────────

def make_base(obj_type: str, active_at: datetime, inactive_at: datetime) -> dict:
    return {
        "id":          str(uuid.uuid4()),
        "type":        obj_type,
        "active_at":   active_at.isoformat(),
        "inactive_at": inactive_at.isoformat(),
        "source":      "simulated",
    }


def make_single_cone(lat: float, lon: float,
                     active_at: datetime, inactive_at: datetime) -> dict:
    obj = make_base("single_cone", active_at, inactive_at)
    obj["coordinates"] = make_point(lat, lon)
    return obj


def make_cone_group(lat: float, lon: float, num_cones: int,
                    active_at: datetime, inactive_at: datetime) -> dict:
    obj = make_base("cone_group", active_at, inactive_at)
    obj["polygon"]    = make_rect_polygon(lat, lon, half_w_m=num_cones * 1.5, half_l_m=5.0)
    obj["num_cones"]  = num_cones
    return obj


def make_construction_zone(lat: float, lon: float, blocking: str,
                            active_at: datetime, inactive_at: datetime) -> dict:
    obj = make_base("construction_zone", active_at, inactive_at)
    obj["polygon"]  = make_rect_polygon(lat, lon, half_w_m=6.0, half_l_m=30.0)
    obj["blocking"] = blocking
    return obj


def make_car_accident(lat: float, lon: float,
                      cars: list[dict], police_present: bool,
                      active_at: datetime, inactive_at: datetime) -> dict:
    obj = make_base("car_accident", active_at, inactive_at)
    obj["polygon"]        = make_rect_polygon(lat, lon, half_w_m=5.0, half_l_m=15.0)
    obj["cars"]           = cars
    obj["police_present"] = police_present
    return obj


def make_double_parked_car(lat: float, lon: float,
                            car_type: str, car_color: str,
                            active_at: datetime, inactive_at: datetime) -> dict:
    obj = make_base("double_parked_car", active_at, inactive_at)
    obj["polygon"]   = make_rect_polygon(lat, lon, half_w_m=1.5, half_l_m=3.0)
    obj["car_type"]  = car_type
    obj["car_color"] = car_color
    return obj


def make_broken_car(lat: float, lon: float,
                    car_type: str, car_color: str,
                    active_at: datetime, inactive_at: datetime) -> dict:
    obj = make_base("broken_car", active_at, inactive_at)
    obj["polygon"]              = make_rect_polygon(lat, lon, half_w_m=2.0, half_l_m=4.0)
    obj["car_type"]             = car_type
    obj["car_color"]            = car_color
    obj["emergency_lights"]     = True
    obj["blocking_direction"]   = "one_direction"
    return obj


def make_road_barrier(lat: float, lon: float,
                      active_at: datetime, inactive_at: datetime) -> dict:
    """Left/right ends placed 3 m either side of centre point."""
    dlon = 3.0 / (111_000 * _cos_lat(lat))
    obj = make_base("road_barrier", active_at, inactive_at)
    obj["left_coordinates"]  = make_point(lat, lon - dlon)
    obj["right_coordinates"] = make_point(lat, lon + dlon)
    return obj


def make_dropped_object(lat: float, lon: float,
                        active_at: datetime, inactive_at: datetime) -> dict:
    obj = make_base("dropped_object", active_at, inactive_at)
    obj["coordinates"] = make_point(lat, lon)
    return obj


def make_protest(lat: float, lon: float,
                 active_at: datetime, inactive_at: datetime) -> dict:
    obj = make_base("protest", active_at, inactive_at)
    obj["polygon"] = make_rect_polygon(lat, lon, half_w_m=10.0, half_l_m=40.0)
    return obj


def make_police_blocking(lat: float, lon: float,
                          directions: str,
                          active_at: datetime, inactive_at: datetime) -> dict:
    obj = make_base("police_blocking", active_at, inactive_at)
    obj["polygon"]             = make_rect_polygon(lat, lon, half_w_m=6.0, half_l_m=10.0)
    obj["directions_blocked"]  = directions
    return obj
