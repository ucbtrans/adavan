#!/usr/bin/env python3
"""
find_city_center.py — Find the commercial/traffic center of a city using OSM data.

Usage:
    python find_city_center.py "Albany, CA"
    python find_city_center.py "Berkeley, CA" --grid 30 --top 0.05

Definition of "city center": the location with the highest concentration of
shops, restaurants, offices, and parking — not the geographic centroid.

Method:
    1. Nominatim  → bounding box for the city
    2. Overpass   → all POIs (amenity, shop, office, parking) within bbox
    3. Grid       → divide bbox into NxN cells, count POIs per cell
    4. Top cells  → take the top X% densest cells, return their weighted centroid
"""

import argparse
import json
import sys
import time
import urllib.request
import urllib.parse


NOMINATIM_URL  = "https://nominatim.openstreetmap.org/search"
OVERPASS_URLS  = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.openstreetmap.ru/api/interpreter",
]

# OSM tags that indicate commercial / traffic activity
OVERPASS_QUERY_TEMPLATE = """
[out:json][timeout:120];
(
  node["amenity"~"restaurant|cafe|bar|fast_food|bank|pharmacy|parking|fuel|cinema|theatre|marketplace|nightclub|pub|food_court|marketplace"]({bbox});
  node["shop"]({bbox});
  node["office"]({bbox});
  node["parking"]({bbox});
  way["amenity"="parking"]({bbox});
  way["shop"]({bbox});
);
out center;
"""


def nominatim_bbox(city_name):
    """Return (south, west, north, east) bounding box for a city."""
    params = urllib.parse.urlencode({
        "q": city_name,
        "format": "json",
        "limit": 1,
    })
    req = urllib.request.Request(
        f"{NOMINATIM_URL}?{params}",
        headers={"User-Agent": "ada-driving-assistant/find_city_center"}
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        results = json.loads(r.read())
    if not results:
        sys.exit(f"ERROR: Nominatim found no results for '{city_name}'")
    hit = results[0]
    bb = hit["boundingbox"]  # [south, north, west, east]
    south, north, west, east = float(bb[0]), float(bb[1]), float(bb[2]), float(bb[3])
    print(f"City bbox: S={south:.5f} N={north:.5f} W={west:.5f} E={east:.5f}")
    return south, west, north, east


def overpass_pois(south, west, north, east):
    """Return list of (lat, lon) for all matching POIs in the bbox."""
    bbox_str = f"{south},{west},{north},{east}"
    query = OVERPASS_QUERY_TEMPLATE.format(bbox=bbox_str)
    data = urllib.parse.urlencode({"data": query}).encode()
    last_err = None
    for url in OVERPASS_URLS:
        print(f"Querying Overpass API: {url.split('/')[2]} ...")
        try:
            req = urllib.request.Request(url, data=data,
                                         headers={"User-Agent": "ada-driving-assistant/find_city_center"})
            with urllib.request.urlopen(req, timeout=150) as r:
                result = json.loads(r.read())
            pois = []
            for el in result.get("elements", []):
                if el["type"] == "node":
                    pois.append((el["lat"], el["lon"]))
                elif el["type"] == "way" and "center" in el:
                    pois.append((el["center"]["lat"], el["center"]["lon"]))
            return pois
        except Exception as e:
            print(f"  failed: {e} — trying next server...")
            last_err = e
            time.sleep(5)
    sys.exit(f"ERROR: All Overpass servers failed. Last error: {last_err}")


def find_dense_center(pois, south, west, north, east, grid_size, top_fraction):
    """
    Divide bbox into grid_size x grid_size cells.
    Count POIs per cell. Take the top top_fraction of cells by count.
    Return the weighted centroid (lat, lon) of those top cells.
    """
    lat_step = (north - south) / grid_size
    lon_step = (east  - west)  / grid_size

    # count[row][col] = (count, sum_lat, sum_lon)
    counts = [[0] * grid_size for _ in range(grid_size)]
    sum_lat = [[0.0] * grid_size for _ in range(grid_size)]
    sum_lon = [[0.0] * grid_size for _ in range(grid_size)]

    for lat, lon in pois:
        row = min(int((lat - south) / lat_step), grid_size - 1)
        col = min(int((lon - west)  / lon_step), grid_size - 1)
        counts[row][col] += 1
        sum_lat[row][col] += lat
        sum_lon[row][col] += lon

    # flatten cells and sort by count
    cells = []
    for r in range(grid_size):
        for c in range(grid_size):
            if counts[r][c] > 0:
                cells.append((counts[r][c], sum_lat[r][c], sum_lon[r][c]))
    cells.sort(reverse=True)

    if not cells:
        sys.exit("ERROR: No POIs found in city. Try a larger city or check city name.")

    # take top X% of cells (at least 1)
    top_n = max(1, int(len(cells) * top_fraction))
    top_cells = cells[:top_n]

    total_weight = sum(c[0] for c in top_cells)
    center_lat = sum(c[1] for c in top_cells) / sum(c[0] for c in top_cells)
    center_lon = sum(c[2] for c in top_cells) / sum(c[0] for c in top_cells)

    return center_lat, center_lon, total_weight, len(pois), top_n


def main():
    parser = argparse.ArgumentParser(description="Find commercial center of a city using OSM POI density.")
    parser.add_argument("city", help='City name, e.g. "Albany, CA"')
    parser.add_argument("--grid", type=int, default=20,
                        help="Grid resolution (NxN cells). Default: 20")
    parser.add_argument("--top", type=float, default=0.05,
                        help="Top fraction of dense cells to use (0.0-1.0). Default: 0.05")
    args = parser.parse_args()

    print(f"\nFinding commercial center of: {args.city}")
    print(f"Grid: {args.grid}x{args.grid}, top {args.top*100:.0f}% of cells\n")

    south, west, north, east = nominatim_bbox(args.city)
    time.sleep(1)  # Nominatim rate limit

    pois = overpass_pois(south, west, north, east)
    print(f"POIs found: {len(pois)}")

    if len(pois) < 10:
        print("WARNING: Very few POIs found. Results may not be meaningful.")

    lat, lon, weight, total, top_n = find_dense_center(
        pois, south, west, north, east, args.grid, args.top
    )

    print(f"\nTop {top_n} densest cells (of {args.grid*args.grid} total), "
          f"covering {weight}/{total} POIs\n")
    print(f"  City center (lat, lon): {lat:.6f}, {lon:.6f}")
    print(f"  Google Maps: https://www.google.com/maps?q={lat:.6f},{lon:.6f}")
    print(f"  OSM:         https://www.openstreetmap.org/?mlat={lat:.6f}&mlon={lon:.6f}&zoom=16")


if __name__ == "__main__":
    main()
