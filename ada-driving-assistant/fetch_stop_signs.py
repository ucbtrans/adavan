#!/usr/bin/env python3
"""
fetch_stop_signs.py — Build stop_signs.json for all 6 supported cities.

Queries Overpass for highway=stop nodes in each city bounding box,
deduplicates using a 15 m spatial cluster, and writes a compact JSON
array of {lat, lon} objects.

Run once:
    python fetch_stop_signs.py
    python fetch_stop_signs.py --out stop_signs.json

Then upload to S3:
    aws s3 cp stop_signs.json s3://ada-driving-assistant-web-173479170210/stop_signs.json \
        --content-type application/json --cache-control "public, max-age=86400"
    aws s3 cp stop_signs.json s3://ada-driving-assistant-web-v2-173479170210/stop_signs.json \
        --content-type application/json --cache-control "public, max-age=86400"
"""

import argparse
import json
import math
import time

import requests

OVERPASS_SERVERS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.openstreetmap.fr/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]

# (city_name, S, W, N, E) — same bboxes used in fetch_traffic_signals.py
CITIES = [
    ("Berkeley",   37.8477, -122.3193, 37.9058, -122.2329),
    ("Albany",     37.8699, -122.3738, 37.8990, -122.2817),
    ("El Cerrito", 37.8975, -122.3233, 37.9383, -122.2811),
    ("Richmond",   37.8836, -122.4415, 38.0286, -122.2435),
    ("Emeryville", 37.8271, -122.3302, 37.8500, -122.2756),
    ("Oakland",    37.6301, -122.3559, 37.8854, -122.1144),
]

CLUSTER_M = 15  # merge stop signs within 15 m of each other


def haversine_m(lat1, lon1, lat2, lon2):
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def query_overpass(bbox_str: str, retries: int = 3) -> list[dict]:
    query = f'[out:json][timeout:30];node["highway"="stop"]({bbox_str});out body;'
    for attempt in range(retries):
        for server in OVERPASS_SERVERS:
            try:
                r = requests.post(server, data={"data": query}, timeout=35)
                if r.status_code == 200:
                    return r.json().get("elements", [])
                print(f"  HTTP {r.status_code} from {server}")
            except Exception as e:
                print(f"  Error from {server}: {e}")
        if attempt < retries - 1:
            wait = 15 * (attempt + 1)
            print(f"  Retrying in {wait}s...")
            time.sleep(wait)
    return []


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="stop_signs.json")
    args = parser.parse_args()

    signs: list[dict] = []  # deduplicated list

    for city, s, w, n, e in CITIES:
        bbox_str = f"{s},{w},{n},{e}"
        print(f"Fetching {city} ({bbox_str})...")
        nodes = query_overpass(bbox_str)
        added = 0
        for node in nodes:
            lat = round(node["lat"], 6)
            lon = round(node["lon"], 6)
            # Spatial dedup: skip if within CLUSTER_M of an existing sign
            if any(haversine_m(lat, lon, ex["lat"], ex["lon"]) < CLUSTER_M for ex in signs):
                continue
            signs.append({"lat": lat, "lon": lon})
            added += 1
        print(f"  {len(nodes)} raw nodes -> {added} added ({len(signs)} total)")
        time.sleep(2)  # be polite to Overpass

    with open(args.out, "w") as f:
        json.dump(signs, f, separators=(",", ":"))

    size_kb = len(json.dumps(signs, separators=(",", ":"))) // 1024
    print(f"\nDone. {len(signs)} stop signs saved to {args.out} ({size_kb} KB)")
    print("\nUpload to S3:")
    print(f"  aws s3 cp {args.out} s3://ada-driving-assistant-web-173479170210/{args.out} "
          f"--content-type application/json --cache-control 'public, max-age=86400'")
    print(f"  aws s3 cp {args.out} s3://ada-driving-assistant-web-v2-173479170210/{args.out} "
          f"--content-type application/json --cache-control 'public, max-age=86400'")


if __name__ == "__main__":
    main()
