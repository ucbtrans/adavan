"""
Curb parking availability simulation for ADA Driving Assistant.

Occupancy formula (non-colored curbs):
  base       = 100 - 20 * distance_from_center_miles
  adjustment = street-type × time-of-day (see _block_occupancy)
  occupancy  = clip(round(base + adjustment), 0, 100)
  chance     = 100 - occupancy

Motorway / motorway_link: occupancy = 100 (no parking allowed).

Berkeley center: Office of Undergraduate Admission, 103 Sproul Hall #5800.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from location import haversine_m, _streets_path, _load_streets

# ── Berkeley center ───────────────────────────────────────────────────────────
_CENTER_LAT = 37.8718   # Sproul Hall, UC Berkeley
_CENTER_LON = -122.2598

_METERS_PER_MILE  = 1609.34
_DEFAULT_RADIUS_M = 300   # roughly 2 city blocks
_BLOCK_M          = 150   # approximate length of one city block

# ── Blue curb (disabled parking) ─────────────────────────────────────────────
_BLUE_CURB_OCCUPANCY = 50   # flat, all streets and times

_BLUE_CURB_KEYWORDS = {
    "blue curb", "blue-curb",
    "disabled parking", "disability parking",
    "handicap parking", "handicapped parking",
    "accessible parking", "ada parking",
    "placard", "wheelchair parking",
}

def _is_blue_curb_question(question: str) -> bool:
    q = question.lower()
    return any(kw in q for kw in _BLUE_CURB_KEYWORDS)


_NUM_WORDS = {
    'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
    '1':   1, '2':   2, '3':   3,   '4':   4,  '5':   5,
}

# Abbreviation → full form (for normalising user input before street matching)
_ABBREVS = {
    r'\bave\.?\b':  'avenue',
    r'\bst\.?\b':   'street',
    r'\bblvd\.?\b': 'boulevard',
    r'\bdr\.?\b':   'drive',
    r'\brd\.?\b':   'road',
    r'\bln\.?\b':   'lane',
    r'\bct\.?\b':   'court',
    r'\bpl\.?\b':   'place',
    r'\bter\.?\b':  'terrace',
    r'\bcir\.?\b':  'circle',
    r'\bhwy\.?\b':  'highway',
}

def _expand_abbrevs(text: str) -> str:
    """Expand common street abbreviations so they match stored street names."""
    t = text.lower()
    for pattern, full in _ABBREVS.items():
        t = re.sub(pattern, full, t)
    return t


# ── Time of day ───────────────────────────────────────────────────────────────

def _is_daytime() -> bool:
    """Return True if current Pacific Time is between 9 am and 9 pm."""
    try:
        from zoneinfo import ZoneInfo
        pacific = ZoneInfo("America/Los_Angeles")
        now = datetime.now(timezone.utc).astimezone(pacific)
    except Exception:
        # Fallback: UTC-7 (PDT) — close enough for simulation purposes
        from datetime import timedelta
        now = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=-7)))
    return 9 <= now.hour < 21


# ── Street type ───────────────────────────────────────────────────────────────

def _street_type(highway: str) -> str:
    """
    Classify an OSM highway tag into 'motorway', 'residential', or 'business'.
    Unknown / undefined tags are treated as 'business'.
    """
    if highway in ("motorway", "motorway_link"):
        return "motorway"
    if highway == "residential":
        return "residential"
    return "business"   # tertiary, secondary, primary, unclassified, or unknown


# ── Core formula ──────────────────────────────────────────────────────────────

def _block_occupancy(p1: dict, p2: dict,
                     highway: str = "",
                     daytime: bool = True) -> int:
    """
    Simulated curb parking occupancy % for one block.

    Motorway / motorway_link → 100 % flat (no parking allowed).
    Business (day)  +10 %, business (night)  -10 %.
    Residential (day) -10 %, residential (night) +10 %.
    Result clipped to [0, 100].
    """
    stype = _street_type(highway)

    if stype == "motorway":
        return 100

    mid_lat = (p1["lat"] + p2["lat"]) / 2
    mid_lon = (p1["lon"] + p2["lon"]) / 2
    dist_miles = haversine_m(mid_lat, mid_lon, _CENTER_LAT, _CENTER_LON) / _METERS_PER_MILE
    base = 100 - 20 * dist_miles

    if stype == "residential":
        adjustment = -10 if daytime else +10
    else:                          # business or unknown
        adjustment = +10 if daytime else -10

    return max(0, min(100, round(base + adjustment)))


# ── Intersection finder ───────────────────────────────────────────────────────

def _find_intersection(name1: str, name2: str) -> tuple[float, float] | None:
    """
    Find where two named streets cross using city_streets.json.
    Returns (lat, lon) midpoint of the closest point pair, or None if > 80 m apart.
    """
    try:
        data = _load_streets()
    except Exception:
        return None

    def get_points(name: str) -> list[dict]:
        pts: list[dict] = []
        for s in data.get("streets", []):
            if s.get("name", "").lower() == name.lower():
                for seg in s.get("segments", []):
                    pts.extend(seg)
        return pts

    pts1 = get_points(name1)
    pts2 = get_points(name2)
    if not pts1 or not pts2:
        return None

    best_dist = float("inf")
    best: tuple[float, float] | None = None
    for p1 in pts1:
        for p2 in pts2:
            d = haversine_m(p1["lat"], p1["lon"], p2["lat"], p2["lon"])
            if d < best_dist:
                best_dist = d
                best = ((p1["lat"] + p2["lat"]) / 2,
                        (p1["lon"] + p2["lon"]) / 2)

    return best if best_dist <= 80 else None


# ── Question parsing ──────────────────────────────────────────────────────────

_STREET_SUFFIXES = {
    "avenue", "street", "boulevard", "drive", "road", "lane", "court",
    "place", "terrace", "circle", "highway", "way", "trail", "alley",
}


def _street_stem(name: str) -> str:
    """Return the street name lower-cased, with the trailing type word removed.

    "Shattuck Avenue" → "shattuck"
    "Telegraph Avenue" → "telegraph"
    "University Avenue" → "university"
    Names without a recognised suffix are returned lower-cased unchanged.
    """
    words = name.lower().split()
    if len(words) > 1 and words[-1] in _STREET_SUFFIXES:
        return " ".join(words[:-1])
    return name.lower()


def _name_matches_question(name: str, q_lower: str, q_expanded: str) -> bool:
    """True if the full name OR its stem appears as a word in the question."""
    full = name.lower()
    if full in q_expanded or full in q_lower:
        return True
    stem = _street_stem(name)
    if stem != full:
        return bool(re.search(r"\b" + re.escape(stem) + r"\b", q_lower))
    return False


def _parse_blocks(question: str) -> int:
    """Return the number of blocks mentioned in the question (default 2)."""
    q = question.lower()
    for word, n in _NUM_WORDS.items():
        if (word + " block") in q:
            return n
    return 2


def extract_intersection_anchor(question: str) -> tuple[float, float, float] | None:
    """
    Detect a street intersection in the question.

    Returns (lat, lon, radius_m) or None.
    Handles patterns like:
      "within two blocks from Solano Ave and Tacoma Ave"
      "near the intersection of Shattuck and University"
      "around Telegraph and Bancroft"
    Bare names (e.g. "Shattuck") are matched against stored names that start
    with that stem (e.g. "Shattuck Avenue").
    """
    try:
        data = _load_streets()
    except Exception:
        return None

    known = [s["name"] for s in data.get("streets", [])
             if not s["name"].startswith("Unnamed_")]

    q_lower    = question.lower()
    q_expanded = _expand_abbrevs(question)

    mentioned = [name for name in known
                 if _name_matches_question(name, q_lower, q_expanded)]
    if len(mentioned) < 2:
        return None

    radius_m = max(_parse_blocks(question) * _BLOCK_M, _DEFAULT_RADIUS_M)

    for i, s1 in enumerate(mentioned):
        stem1 = _street_stem(s1)
        for s2 in mentioned[i + 1:]:
            stem2 = _street_stem(s2)
            for q_check in (q_lower, q_expanded):
                # Try full names and stems for the "X and Y" pattern
                candidates = [
                    (re.escape(s1.lower()), re.escape(s2.lower())),
                    (re.escape(stem1),      re.escape(stem2)),
                ]
                for a, b in candidates:
                    if (re.search(a + r"[\s,]+and[\s,]+" + b, q_check) or
                            re.search(b + r"[\s,]+and[\s,]+" + a, q_check)):
                        coords = _find_intersection(s1, s2)
                        if coords:
                            return coords[0], coords[1], radius_m

    return None


# ── Parking search ────────────────────────────────────────────────────────────

def parking_near(lat: float, lon: float,
                 radius_m: float = _DEFAULT_RADIUS_M) -> dict | None:
    """
    Find the best curb parking chance among blocks within radius_m of (lat, lon).

    Returns:
        {
            "best_chance":  int,
            "best_street":  str,
            "daytime":      bool,
            "blocks":       [{"street": str, "highway": str, "street_type": str,
                               "occupancy": int, "chance": int}, ...]
        }
        or None if no blocks found within radius.
    """
    try:
        data = _load_streets()
    except Exception:
        return None

    daytime = _is_daytime()
    seen: set[tuple] = set()
    blocks: list[dict] = []

    for street in data.get("streets", []):
        name    = street.get("name", "")
        highway = street.get("highway", "")
        if name.startswith("Unnamed_"):
            continue
        for seg in street.get("segments", []):
            if len(seg) < 2:
                continue
            p1, p2 = seg[0], seg[-1]
            mid_lat = (p1["lat"] + p2["lat"]) / 2
            mid_lon = (p1["lon"] + p2["lon"]) / 2
            if haversine_m(mid_lat, mid_lon, lat, lon) > radius_m:
                continue
            key = (name, round(p1["lat"], 5), round(p1["lon"], 5))
            if key in seen:
                continue
            seen.add(key)
            occ   = _block_occupancy(p1, p2, highway=highway, daytime=daytime)
            stype = _street_type(highway)
            blocks.append({
                "street":      name,
                "highway":     highway,
                "street_type": stype,
                "occupancy":   occ,
                "chance":      100 - occ,
            })

    if not blocks:
        return None

    # Exclude motorway blocks from best-chance consideration
    parkable = [b for b in blocks if b["street_type"] != "motorway"]
    if not parkable:
        return None

    parkable.sort(key=lambda b: -b["chance"])
    return {
        "best_chance": parkable[0]["chance"],
        "best_street": parkable[0]["street"],
        "daytime":     daytime,
        "blocks":      parkable,
    }


# ── Single-street parking search ─────────────────────────────────────────────

def parking_on_street(street_name: str) -> dict | None:
    """
    Find parking availability for every block on a single named street.

    Returns the same dict shape as parking_near() — with an extra
    'ask_cross_street': True flag so the AI knows to prompt the user
    for a cross street.  Returns None if the street is not found or is
    a motorway.
    """
    try:
        data = _load_streets()
    except Exception:
        return None

    target = next(
        (s for s in data.get("streets", [])
         if s.get("name", "").lower() == street_name.lower()),
        None,
    )
    if not target:
        return None

    daytime = _is_daytime()
    name    = target["name"]
    highway = target.get("highway", "")
    stype   = _street_type(highway)

    if stype == "motorway":
        return None   # no parking on motorways / ramps

    blocks: list[dict] = []
    seen: set = set()
    for seg in target.get("segments", []):
        if len(seg) < 2:
            continue
        p1, p2 = seg[0], seg[-1]
        key = (round(p1["lat"], 5), round(p1["lon"], 5))
        if key in seen:
            continue
        seen.add(key)
        occ = _block_occupancy(p1, p2, highway=highway, daytime=daytime)
        blocks.append({
            "street":      name,
            "highway":     highway,
            "street_type": stype,
            "occupancy":   occ,
            "chance":      100 - occ,
        })

    if not blocks:
        return None

    blocks.sort(key=lambda b: -b["chance"])
    return {
        "best_chance":      blocks[0]["chance"],
        "best_street":      name,
        "daytime":          daytime,
        "blocks":           blocks,
        "anchor_label":     name,
        "ask_cross_street": True,
    }


# ── Main entry point ──────────────────────────────────────────────────────────

def get_parking_context(question: str,
                        dest_lat: float | None = None,
                        dest_lon: float | None = None,
                        fallback_lat: float | None = None,
                        fallback_lon: float | None = None) -> dict | None:
    """
    Resolve parking anchor and return parking context for the AI.

    Priority:
      1. Intersection mentioned in the question (≥ 2 streets)
      2. Single named street (no cross street given) → best block + ask for cross street
      3. Session destination
      4. Driver's current location

    If the question explicitly asks about blue curb / disabled parking,
    the result includes a "blue_curb" key with occupancy and chance.
    """
    anchor = extract_intersection_anchor(question)
    if anchor:
        lat, lon, radius_m = anchor
        result = parking_near(lat, lon, radius_m)
        if result:
            result["anchor_label"] = (
                f"intersection mentioned in your question "
                f"(within {round(radius_m / _BLOCK_M)} block(s))"
            )
            _maybe_add_blue_curb(question, result)
            return result

    # Single named street without a cross street
    try:
        streets_data = _load_streets()
        known = [s["name"] for s in streets_data.get("streets", [])
                 if not s["name"].startswith("Unnamed_")]
        q_lower    = question.lower()
        q_expanded = _expand_abbrevs(question)
        # Full-name match only — avoids stem over-matching
        # (e.g. "Shattuck Place" would otherwise match "shattuck avenue" via stem)
        single = [n for n in known
                  if n.lower() in q_lower or n.lower() in q_expanded]
        if len(single) == 1:
            result = parking_on_street(single[0])
            if result:
                _maybe_add_blue_curb(question, result)
                return result
    except Exception:
        pass

    if dest_lat is not None and dest_lon is not None:
        result = parking_near(float(dest_lat), float(dest_lon))
        if result:
            result["anchor_label"] = "your destination"
            _maybe_add_blue_curb(question, result)
            return result

    if fallback_lat is not None and fallback_lon is not None:
        result = parking_near(float(fallback_lat), float(fallback_lon))
        if result:
            result["anchor_label"] = "your current location"
            _maybe_add_blue_curb(question, result)
            return result

    # Blue curb only (user asked about blue curb but no regular parking anchor)
    if _is_blue_curb_question(question):
        return {
            "anchor_label": "your area",
            "best_chance":  None,
            "best_street":  None,
            "daytime":      _is_daytime(),
            "blocks":       [],
            "blue_curb":    {"occupancy": _BLUE_CURB_OCCUPANCY,
                             "chance":    100 - _BLUE_CURB_OCCUPANCY},
        }

    return None


def _maybe_add_blue_curb(question: str, result: dict) -> None:
    """Add blue_curb entry to result if the question asks about it."""
    if _is_blue_curb_question(question):
        result["blue_curb"] = {
            "occupancy": _BLUE_CURB_OCCUPANCY,
            "chance":    100 - _BLUE_CURB_OCCUPANCY,
        }
