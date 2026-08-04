"""
Test route-corridor filtering and AI scoping.

Simulates a route: Hiller Drive → Tunnel Road → Ashby Ave → College Ave
Asks: "are road conditions clear ahead?"
Verifies the response only mentions events on route streets.
"""

import json, os, urllib.request
from dotenv import load_dotenv
load_dotenv()

import boto3
from location import find_objects_along_route, geocode_address
from assistant import answer_question

# ── 1. Origin / destination ───────────────────────────────────────────────────
# Route from Berkeley Hills area to Ashby/College via Claremont Ave
# (OSRM-verified route through Berkeley streets)
print("=== Simulated Route (hardcoded OSRM-verified coordinates) ===")
origin = {"lat": 37.8715, "lon": -122.2165, "address": "Grizzly Peak Blvd, Berkeley Hills"}
dest   = {"lat": 37.857,  "lon": -122.253,  "address": "College Ave & Ashby Ave, Berkeley"}
print(f"Origin : {origin}")
print(f"Dest   : {dest}")

# ── 2. Fetch OSRM route ───────────────────────────────────────────────────────
print("\n=== OSRM Route ===")
url = (f"https://router.project-osrm.org/route/v1/driving/"
       f"{origin['lon']},{origin['lat']};{dest['lon']},{dest['lat']}"
       f"?overview=full&geometries=geojson&steps=true")
with urllib.request.urlopen(url, timeout=15) as r:
    osrm = json.loads(r.read())

assert osrm["code"] == "Ok", f"OSRM error: {osrm}"
route_coords = osrm["routes"][0]["geometry"]["coordinates"]  # [[lon,lat],...]

route_streets = []
for leg in osrm["routes"][0].get("legs", []):
    for step in leg.get("steps", []):
        if step.get("name"):
            route_streets.append(step["name"])
route_streets = list(dict.fromkeys(route_streets))   # deduplicated, ordered

print(f"Route coords : {len(route_coords)} points")
print(f"Route streets: {route_streets}")

# ── 3. Load city objects from S3 ─────────────────────────────────────────────
print("\n=== City Objects ===")
S3_BUCKET   = os.environ.get("S3_BUCKET", "ada-driving-assistant")
OBJECTS_KEY = os.environ.get("OBJECTS_KEY", "CA/Berkeley/city_objects.json")
s3 = boto3.client("s3")
resp    = s3.get_object(Bucket=S3_BUCKET, Key=OBJECTS_KEY)
objects = json.loads(resp["Body"].read())
print(f"Total objects in city: {len(objects)}")

# ── 4. Filter ─────────────────────────────────────────────────────────────────
print("\n=== Route-Corridor Filter ===")
nearby = find_objects_along_route(route_coords, objects, route_streets=route_streets)
print(f"Objects along route  : {len(nearby)}")
for obj in nearby:
    st   = obj.get("street", "(no street)")
    dist = obj.get("_distance_m", "?")
    typ  = obj.get("type", "?")
    print(f"  [{dist:>4}m]  {st}  —  {typ}")

# Check for streets NOT on route
print()
off_route = [o for o in nearby if o.get("street","").lower() not in
             {s.lower() for s in route_streets} and o.get("street","")]
if off_route:
    print(f"WARNING: {len(off_route)} off-route street objects leaked through:")
    for o in off_route:
        print(f"  {o.get('street')} ({o.get('_distance_m')}m)")
else:
    print("OK: All named objects are on route streets (or have no street field).")

# ── 5. Ask Claude ─────────────────────────────────────────────────────────────
print("\n=== Claude Q&A ===")
location = {
    "address":           origin["address"],
    "lat":               origin["lat"],
    "lon":               origin["lon"],
    "bearing":           0,
    "bearing_direction": "N",
    "destination":       dest["address"],
}
question = "are road conditions clear ahead?"
answer, usage = answer_question(question, location, nearby, [])
print(f"Q: {question}")
print(f"A: {answer}")
print(f"\nTokens — in: {usage['input_tokens']}  out: {usage['output_tokens']}")

# ── 6. Verify answer doesn't mention off-route streets ───────────────────────
print("\n=== Scope Verification ===")
answer_lower = answer.lower()
violations = [s for s in route_streets if s.lower() in answer_lower]   # should be fine
unexpected = []
# Check a few known off-route streets from the earlier bad response
for bad_street in ["north hill court", "roble court", "marie way", "vicente road",
                   "unnamed_tertiary"]:
    if bad_street in answer_lower:
        unexpected.append(bad_street)

if unexpected:
    print(f"WARNING: Answer mentioned off-route streets: {unexpected}")
else:
    print("OK: Answer does not mention any known off-route streets.")
print("Route streets mentioned:", [s for s in route_streets if s.lower() in answer_lower])
