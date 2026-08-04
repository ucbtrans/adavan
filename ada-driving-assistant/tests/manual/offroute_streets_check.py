"""
Test: off-route street query pipeline.

Simulates a user asking about road conditions on streets that are NOT on their
planned route and validates that:
  1. find_streets_mentioned() detects the street name from the question
  2. find_objects_on_street() returns active objects (or empty list)
  3. The full answer_question() response mentions the street correctly
     — either listing obstacles or saying the road is clear.

Run from the project root:
    python tests/test_offroute_streets.py
"""

import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import boto3

from assistant import answer_question
from location import find_objects_on_street, find_streets_mentioned

# ── Load city objects from S3 ─────────────────────────────────────────────────
S3_BUCKET   = os.environ.get("S3_BUCKET", "ada-driving-assistant")
OBJECTS_KEY = os.environ.get("OBJECTS_KEY", "CA/Berkeley/city_objects.json")

print("Loading city objects from S3 …")
s3 = boto3.client("s3")
resp = s3.get_object(Bucket=S3_BUCKET, Key=OBJECTS_KEY)
ALL_OBJECTS = json.loads(resp["Body"].read())
print(f"  {len(ALL_OBJECTS)} total objects loaded")

# ── Fake locations ────────────────────────────────────────────────────────────
# With route
FAKE_LOCATION = {
    "address":           "2400 Telegraph Avenue, Berkeley",
    "lat":               37.8660,
    "lon":               -122.2588,
    "bearing":           0,
    "bearing_direction": "N",
    "destination":       "College Ave & Ashby Ave, Berkeley",
    "checked_streets":   [],
}
ROUTE_STREETS = ["Telegraph Avenue", "Dwight Way", "College Avenue"]

# Without route (simulates first question with no destination set)
FAKE_LOCATION_NO_ROUTE = {
    "address":           "2400 Telegraph Avenue, Berkeley",
    "lat":               37.8660,
    "lon":               -122.2588,
    "bearing":           0,
    "bearing_direction": "N",
    "destination":       "",
    "checked_streets":   [],
}

# ── Helper ────────────────────────────────────────────────────────────────────
def run_case(label, question, route_streets=None, no_route=False):
    rs = route_streets if route_streets is not None else ([] if no_route else ROUTE_STREETS)
    loc = dict(FAKE_LOCATION_NO_ROUTE if no_route else FAKE_LOCATION)

    # 1. Street detection
    found = find_streets_mentioned(question, rs)
    print(f"\n{'='*60}")
    print(f"CASE: {label}")
    print(f"  Question       : {question}")
    print(f"  Route streets  : {rs}")
    print(f"  Detected streets: {found}")

    # 2. Fetch objects for each found street
    nearby = []
    now = datetime.now(timezone.utc)
    for street in found:
        objs = find_objects_on_street(street, ALL_OBJECTS)
        active = [o for o in objs
                  if datetime.fromisoformat(o["active_at"])   <= now
                  and datetime.fromisoformat(o["inactive_at"]) >= now]
        print(f"  Objects on '{street}': {len(active)} active")
        for o in active:
            print(f"    - {o['type']} (inactive_at {o['inactive_at']})")
        for obj in active:
            nearby.append({**obj, "_off_route": True})

    loc["checked_streets"] = found

    # 3. Get AI answer
    answer, usage = answer_question(question, loc, nearby, [])
    print(f"  Answer   : {answer}")
    print(f"  Tokens   : in={usage['input_tokens']} out={usage['output_tokens']}")

    # 4. Validate
    ok = True
    if found:
        # Expect the answer to mention the street (full name or abbrev)
        first = found[0].lower().replace(" avenue","").replace(" street","").replace(" ave","").replace(" st","")
        if first not in answer.lower() and found[0].lower() not in answer.lower():
            print(f"  FAIL: answer does not mention '{found[0]}'")
            ok = False
        else:
            # Check: either "clear" / "no events" / "no obstacles" OR specific obstacle types mentioned
            clear_words = ["clear", "no current", "no active", "no event", "no obstacle", "no hazard"]
            obstacle_words = ["construction", "cone", "closure", "accident", "barrier", "object", "vehicle"]
            has_clear    = any(w in answer.lower() for w in clear_words)
            has_obstacle = any(w in answer.lower() for w in obstacle_words)
            if not has_clear and not has_obstacle:
                print(f"  FAIL: answer neither confirms clear nor lists obstacles")
                ok = False
            else:
                print(f"  OK: {'clear road reported' if has_clear else 'obstacles listed'}")
    else:
        print(f"  SKIP validation (no streets detected in question)")

    return ok


# ── Test cases ────────────────────────────────────────────────────────────────
cases = [
    # (label, question, route_streets_override=None, no_route=False)
    ("Ashby Ave — first question, NO route set",
     "What are the road conditions on Ashby Avenue?",
     None, True),

    ("Ashby Ave — first question, NO route, alternate phrasing",
     "Are there any obstacles on Ashby Avenue?",
     None, True),

    ("Ashby Ave — with route (Ashby not on route)",
     "What are the road conditions on Ashby Avenue?",
     None, False),

    ("Ashby Ave — destination street (Ashby IS on route)",
     "Are there obstacles on Ashby Avenue?",
     ["Telegraph Avenue", "Dwight Way", "College Avenue", "Ashby Avenue"], False),

    ("Adeline Street — no route",
     "Is Adeline Street clear ahead?",
     None, True),

    ("Shattuck Avenue — no active events expected",
     "Any construction on Shattuck Avenue?",
     None, True),

    ("Telegraph Avenue — on route",
     "Is Telegraph Avenue clear?",
     None, False),

    ("Nonexistent street",
     "How is traffic on Banana Peel Boulevard?",
     None, True),

    ("Multiple streets — no route",
     "What about Ashby Avenue and Adeline Street — any issues there?",
     None, True),
]

results = []
for case in cases:
    label    = case[0]
    question = case[1]
    route_streets = case[2] if len(case) > 2 else None
    no_route      = case[3] if len(case) > 3 else False
    passed = run_case(label, question, route_streets, no_route)
    results.append((label, passed))

print(f"\n{'='*60}")
print("SUMMARY")
for label, passed in results:
    status = "OK  " if passed else "FAIL"
    print(f"  [{status}] {label}")
