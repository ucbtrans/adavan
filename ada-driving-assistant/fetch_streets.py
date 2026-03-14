"""
Fetch all drivable streets for Berkeley, CA from the Overpass API
and build city_streets.json with full coordinates and lane counts.

Run once:  python fetch_streets.py
"""

import json
import math
import sys
import time
import xml.etree.ElementTree as ET
from collections import defaultdict

import requests

# ── Berkeley bounding box (S, W, N, E) ──────────────────────────────────────
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

OVERPASS_URL = "https://overpass-api.de/api/interpreter"


def fetch_osm(bbox):
    s, w, n, e = bbox
    query = f"""
[out:xml][timeout:90];
(
  way["highway"]({s},{w},{n},{e});
);
out body;
>;
out skel qt;
"""
    print("Querying Overpass API for Berkeley streets…")
    for attempt in range(3):
        try:
            r = requests.post(OVERPASS_URL, data={"data": query}, timeout=120)
            r.raise_for_status()
            return r.text
        except Exception as exc:
            print(f"  Attempt {attempt+1} failed: {exc}")
            if attempt < 2:
                time.sleep(5)
    raise RuntimeError("Overpass API unavailable after 3 attempts")


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
    xml_text = fetch_osm(BBOX)

    print("Parsing OSM data…")
    streets = build_streets(xml_text)
    print(f"Found {len(streets)} streets")

    out = {
        "city":    "Berkeley",
        "state":   "CA",
        "country": "US",
        "bbox":    {"south": BBOX[0], "west": BBOX[1], "north": BBOX[2], "east": BBOX[3]},
        "streets": streets,
    }

    outfile = "city_streets.json"
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
