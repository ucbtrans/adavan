#!/usr/bin/env python3
"""
generate_addresses.py — Offline pre-generation of addresses_pool.json

Loads city_streets JSON files for all 6 cities, picks 1000 random drivable
points (weighted by named-street count), reverse-geocodes each via Nominatim
(1 req/sec policy), and saves addresses_pool.json for use by the frontend.

Run once (takes ~17 minutes for 1000 addresses):
    python generate_addresses.py
    python generate_addresses.py --count 1000 --out addresses_pool.json
"""

import argparse
import json
import os
import random
import time
from math import atan2, cos, degrees, radians, sin

import requests

NOMINATIM_URL     = "https://nominatim.openstreetmap.org"
NOMINATIM_HEADERS = {"User-Agent": "ADA-Driving-Assistant/1.0 (ucbtrans)"}
RATE_LIMIT_S      = 1.1   # Nominatim policy: max 1 req/sec; use 1.1 for safety

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CITY_FILES = [
    ("Berkeley",   "city_streets.json"),
    ("Albany",     "city_streets_Albany.json"),
    ("ElCerrito",  "city_streets_ElCerrito.json"),
    ("Richmond",   "city_streets_Richmond.json"),
    ("Emeryville", "city_streets_Emeryville.json"),
    ("Oakland",    "city_streets_Oakland.json"),
]


# ── Geometry helpers (mirrors location.py) ───────────────────────────────────

def bearing_to_direction(bearing: float) -> str:
    dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return dirs[round(bearing / 45) % 8]


def compute_bearing(p1: dict, p2: dict) -> int:
    lat1, lon1 = radians(p1["lat"]), radians(p1["lon"])
    lat2, lon2 = radians(p2["lat"]), radians(p2["lon"])
    dlon = lon2 - lon1
    x = sin(dlon) * cos(lat2)
    y = cos(lat1) * sin(lat2) - sin(lat1) * cos(lat2) * cos(dlon)
    return round(degrees(atan2(x, y)) % 360)


def is_oneway(street: dict) -> bool:
    return street.get("lanes_backward", 1) == 0


# ── Nominatim reverse geocode ─────────────────────────────────────────────────

def reverse_geocode(lat: float, lon: float) -> str:
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


# ── City loader ───────────────────────────────────────────────────────────────

def load_cities() -> list:
    """Return [(city_name, usable_streets_list), ...] for all cities."""
    result = []
    for city, fname in CITY_FILES:
        path = os.path.join(BASE_DIR, fname)
        if not os.path.exists(path):
            print(f"  WARNING: {fname} not found — skipping {city}")
            continue
        with open(path) as f:
            data = json.load(f)
        named = [s for s in data["streets"] if not s["name"].startswith("Unnamed_")]
        usable = [
            s for s in named
            if any(len(seg) >= 2
                   for seg in (s.get("segments") or [s.get("waypoints", [])]))
        ]
        result.append((city, usable))
        print(f"  {city:<12}: {len(usable):>4} usable named streets")
    return result


# ── Random point picker ───────────────────────────────────────────────────────

def pick_random_point(streets: list) -> dict:
    for _ in range(200):
        street   = random.choice(streets)
        segments = street.get("segments") or [street.get("waypoints", [])]
        usable   = [seg for seg in segments if len(seg) >= 2]
        if usable:
            break
    else:
        raise RuntimeError("No usable segments found")

    seg = random.choice(usable)
    i   = random.randint(0, len(seg) - 2)
    p1, p2 = seg[i], seg[i + 1]
    t   = random.random()
    lat = p1["lat"] + t * (p2["lat"] - p1["lat"])
    lon = p1["lon"] + t * (p2["lon"] - p1["lon"])
    bearing = compute_bearing(p1, p2)

    return {
        "lat":     round(lat, 6),
        "lon":     round(lon, 6),
        "bearing": bearing,
        "street":  street["name"],
        "oneway":  is_oneway(street),
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def generate(total: int, out_path: str) -> None:
    print("Loading city street files...")
    cities = load_cities()
    if not cities:
        raise SystemExit("No city files found.")

    # Even allocation across cities; last city absorbs rounding remainder
    base  = total // len(cities)
    extra = total % len(cities)
    allocations = []
    print("\nAddress allocation:")
    for i, (city, streets) in enumerate(cities):
        alloc = base + (1 if i < extra else 0)
        allocations.append((city, streets, alloc))
        print(f"  {city:<12}: {alloc:>4} addresses")

    print(f"\nGenerating {total} addresses (~{total} seconds, ~{total//60}m{total%60:02d}s)...\n")

    pool     = []
    done     = 0
    start_ts = time.time()

    for city, streets, alloc in allocations:
        city_done = 0
        while city_done < alloc:
            point = pick_random_point(streets)

            time.sleep(RATE_LIMIT_S)
            address = reverse_geocode(point["lat"], point["lon"])

            entry = {
                "address":           address,
                "lat":               point["lat"],
                "lon":               point["lon"],
                "bearing":           point["bearing"],
                "bearing_direction": bearing_to_direction(point["bearing"]),
                "street":            point["street"],
                "city":              city,
            }

            if point["oneway"]:
                entry["heading_auto"] = True
            else:
                rev = (point["bearing"] + 180) % 360
                entry["heading_auto"]    = False
                entry["heading_options"] = [
                    {"bearing": point["bearing"],
                     "direction": bearing_to_direction(point["bearing"])},
                    {"bearing": rev,
                     "direction": bearing_to_direction(rev)},
                ]

            pool.append(entry)
            done      += 1
            city_done += 1

            elapsed = time.time() - start_ts
            rate    = done / elapsed if elapsed > 0 else 0
            eta_s   = int((total - done) / rate) if rate > 0 else 0
            print(f"  [{done:4d}/{total}] {city:<12} {address[:55]:<55}  ETA {eta_s//60}m{eta_s%60:02d}s")

        # Checkpoint after each city
        with open(out_path, "w") as f:
            json.dump(pool, f, separators=(",", ":"))
        print(f"  -- Checkpoint: {city} done, {len(pool)} entries saved to {out_path} --\n")

    size_kb = os.path.getsize(out_path) // 1024
    print(f"Done. {len(pool)} entries saved to {out_path} ({size_kb} KB)")
    print(f"\nNext: upload to S3:")
    print(f"  aws s3 cp {out_path} s3://ada-driving-assistant-web-173479170210/addresses_pool.json --content-type application/json --cache-control 'public, max-age=86400'")
    print(f"  aws s3 cp {out_path} s3://ada-driving-assistant-web-v2-173479170210/addresses_pool.json --content-type application/json --cache-control 'public, max-age=86400'")


def main():
    parser = argparse.ArgumentParser(description="Pre-generate addresses_pool.json for ADA.")
    parser.add_argument("--count", type=int, default=1000,
                        help="Number of addresses to generate (default: 1000)")
    parser.add_argument("--out", default="addresses_pool.json",
                        help="Output file (default: addresses_pool.json)")
    args = parser.parse_args()
    generate(args.count, args.out)


if __name__ == "__main__":
    main()
