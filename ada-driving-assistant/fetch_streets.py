"""
Fetch all drivable streets for a city from the Overpass API
and build city_streets.json with full coordinates and lane counts.

Run once (Berkeley default):
    python fetch_streets.py

Other cities:
    python fetch_streets.py --city Albany --state CA --bbox 37.8699,-122.3738,37.8990,-122.2817
    python fetch_streets.py --city "El Cerrito" --state CA --bbox 37.8975,-122.3233,37.9383,-122.2811
    python fetch_streets.py --city Richmond --state CA --bbox 37.8836,-122.4415,38.0286,-122.2435
    python fetch_streets.py --city Emeryville --state CA --bbox 37.8271,-122.3302,37.8500,-122.2756
    python fetch_streets.py --city Oakland --state CA --bbox 37.6301,-122.3559,37.8854,-122.1144
"""

import argparse
import json
import math
import sys
import time
import xml.etree.ElementTree as ET
from collections import defaultdict

import requests

# ── Default: Berkeley bounding box (S, W, N, E) ─────────────────────────────
BBOX = (37.8477, -122.3193, 37.9058, -122.2329)

# Drivable highway types
DRIVABLE = {
    "motorway", "motorway_link",
    "trunk", "trunk_link",
    "primary", "primary_link",
    "secondary", "secondary_link",
    "tertiary", "tertiary_link",
    "unclassified", "residential", "living_street",
}

# Default lanes per direction when OSM tags are absent
DEFAULT_LANES = {
    "motorway": 3, "motorway_link": 1,
    "trunk": 2,    "trunk_link": 1,
    "primary": 2,  "primary_link": 1,
    "secondary": 1,"secondary_link": 1,
    "tertiary": 1, "tertiary_link": 1,
    "unclassified": 1, "residential": 1, "living_street": 1,
}

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.openstreetmap.ru/api/interpreter",
]


def fetch_osm(bbox, city="city"):
    s, w, n, e = bbox
    query = f"""
[out:xml][timeout:120];
(
  way["highway"]({s},{w},{n},{e});
);
out body;
>;
out skel qt;
"""
    print(f"Querying Overpass API for {city} streets…")
    for url in OVERPASS_URLS:
        for attempt in range(2):
            try:
                r = requests.post(url, data={"data": query}, timeout=150)
                r.raise_for_status()
                return r.text
            except Exception as exc:
                print(f"  {url.split('/')[2]} attempt {attempt+1} failed: {exc}")
                if attempt < 1:
                    time.sleep(5)
        print(f"  Trying next Overpass server…")
        time.sleep(5)
    raise RuntimeError("All Overpass servers unavailable")


def parse_lanes(tags, highway_type):
    """
    Return (lanes_forward, lanes_backward) as ints.
    Uses OSM tags if present, otherwise sensible defaults.
    """
    oneway = tags.get("oneway", "no") in ("yes", "1", "true", "-1")
    oneway_reverse = tags.get("oneway") == "-1"

    default = DEFAULT_LANES.get(highway_type, 1)

    # Explicit per-direction tags
    fwd = tags.get("lanes:forward")
    bwd = tags.get("lanes:backward")
    total = tags.get("lanes")

    if fwd and bwd:
        return int(fwd), int(bwd)

    if oneway:
        total_lanes = int(total) if total else default
        if oneway_reverse:
            return 0, total_lanes
        return total_lanes, 0

    if total:
        t = int(total)
        half = t // 2
        return max(half, 1), max(t - half, 1)

    return default, default


def build_streets(xml_text):
    root = ET.fromstring(xml_text)

    # Build node id → (lat, lon)
    nodes = {}
    for node in root.iter("node"):
        nid = node.get("id")
        lat = node.get("lat")
        lon = node.get("lon")
        if lat and lon:
            nodes[nid] = (float(lat), float(lon))

    # Group ways by name (merge segments of the same street)
    # street_name → list of way dicts
    street_ways = defaultdict(list)
    unnamed_count = 0

    for way in root.iter("way"):
        tags = {t.get("k"): t.get("v") for t in way.iter("tag")}
        highway = tags.get("highway", "")
        if highway not in DRIVABLE:
            continue

        name = tags.get("name") or tags.get("ref")
        if not name:
            unnamed_count += 1
            name = f"Unnamed_{highway}_{unnamed_count}"

        nd_refs = [nd.get("ref") for nd in way.iter("nd")]
        waypoints = [
            {"lat": nodes[r][0], "lon": nodes[r][1]}
            for r in nd_refs if r in nodes
        ]
        if len(waypoints) < 2:
            continue

        lanes_fwd, lanes_bwd = parse_lanes(tags, highway)

        street_ways[name].append({
            "waypoints": waypoints,
            "lanes_forward": lanes_fwd,
            "lanes_backward": lanes_bwd,
            "highway": highway,
        })

    # Build final street list
    streets = []
    for name, ways in sorted(street_ways.items()):
        # Use most common lane values across segments
        all_fwd = [w["lanes_forward"]  for w in ways]
        all_bwd = [w["lanes_backward"] for w in ways]
        lanes_fwd = max(set(all_fwd), key=all_fwd.count)
        lanes_bwd = max(set(all_bwd), key=all_bwd.count)
        highway   = ways[0]["highway"]

        # Flatten all waypoints (keep segments as sub-lists so routing is possible)
        segments = [w["waypoints"] for w in ways]

        streets.append({
            "name":            name,
            "highway":         highway,
            "lanes_forward":   lanes_fwd,
            "lanes_backward":  lanes_bwd,
            "segments":        segments,   # list of lists of {lat, lon}
        })

    return streets


def main():
    parser = argparse.ArgumentParser(description="Fetch drivable streets for a city from OSM.")
    parser.add_argument("--city",  default="Berkeley", help="City name (default: Berkeley)")
    parser.add_argument("--state", default="CA",       help="State abbreviation (default: CA)")
    parser.add_argument("--bbox",  default=None,
                        help="Bounding box as S,W,N,E (default: Berkeley bbox)")
    args = parser.parse_args()

    if args.bbox:
        parts = [float(x) for x in args.bbox.split(",")]
        bbox = tuple(parts)  # S, W, N, E
    else:
        bbox = BBOX

    xml_text = fetch_osm(bbox, city=args.city)

    print("Parsing OSM data…")
    streets = build_streets(xml_text)
    print(f"Found {len(streets)} streets")

    out = {
        "city":    args.city,
        "state":   args.state,
        "country": "US",
        "bbox":    {"south": bbox[0], "west": bbox[1], "north": bbox[2], "east": bbox[3]},
        "streets": streets,
    }

    # Output filename: city_streets.json for Berkeley (backwards compat),
    # city_streets_<City>.json for others
    city_slug = args.city.replace(" ", "")
    outfile = "city_streets.json" if args.city == "Berkeley" else f"city_streets_{city_slug}.json"
    with open(outfile, "w") as f:
        json.dump(out, f, separators=(",", ":"))   # compact — file is large

    size_kb = round(len(json.dumps(out)) / 1024)
    print(f"Saved {outfile}  ({size_kb} KB, {len(streets)} streets)")

    # Quick lane summary
    from collections import Counter
    hw_counts = Counter(s["highway"] for s in streets)
    print("\nStreet types:")
    for hw, c in hw_counts.most_common():
        print(f"  {hw:<20} {c}")


if __name__ == "__main__":
    main()
