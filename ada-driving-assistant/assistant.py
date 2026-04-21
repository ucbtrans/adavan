"""
ADA Driving Assistant — Claude API integration.

Two entry points:
  get_advisory()      — short advisory from detection/position data (dashboard)
  answer_question()   — multi-turn driving Q&A with session history
"""

from __future__ import annotations

import os
from dotenv import load_dotenv
from anthropic import Anthropic

load_dotenv()
client = Anthropic()

# ── Shared helpers ────────────────────────────────────────────────────────────

def _fmt_dist(metres: float) -> str:
    """Format a distance in US customary units (ft under 1000 ft, else miles)."""
    feet = metres * 3.28084
    if feet < 1000:
        return f"{round(feet)} ft"
    miles = metres / 1609.34
    if miles < 10:
        return f"{miles:.1f} mi"
    return f"{round(miles)} mi"


def _obj_summary(obj: dict) -> str:
    """One-line description of a traffic object."""
    t    = obj.get("type", "unknown").replace("_", " ")
    dist = obj.get("_distance_m")
    st   = obj.get("street", "")
    dist_str = f" ({_fmt_dist(dist)} ahead)" if dist is not None else ""
    street_str = f" on {st}" if st else ""

    extras = []
    if obj.get("blocking"):
        extras.append(f"blocking: {obj['blocking'].replace('_', ' ')}")
    if obj.get("num_cones"):
        extras.append(f"{obj['num_cones']} cones")
    if obj.get("cars"):
        cars = ", ".join(f"{c['color']} {c['type']}" for c in obj["cars"])
        extras.append(f"vehicles: {cars}")
    if obj.get("police_present"):
        extras.append("police on scene")
    if obj.get("car_type"):
        extras.append(f"{obj['car_color']} {obj['car_type']}")
    if obj.get("directions_blocked"):
        extras.append(f"blocking {obj['directions_blocked'].replace('_', ' ')}")
    if obj.get("emergency_lights"):
        extras.append("emergency lights on")
    lanes_fwd = obj.get("lanes_forward")
    lanes_bwd = obj.get("lanes_backward")
    if lanes_fwd is not None:
        extras.append(f"road: {lanes_fwd}+{lanes_bwd} lanes")

    detail = f" [{', '.join(extras)}]" if extras else ""
    return f"- {t}{street_str}{dist_str}{detail}"


# ── Dashboard advisory ────────────────────────────────────────────────────────

_ADVISORY_SYSTEM = """You are an AI co-pilot for an ADA van in Berkeley, CA work zones.
Produce a concise driving advisory (max 3 sentences) based on the current
position and detected nearby objects. Prioritise worker safety."""


def get_advisory(position: dict, detections: list[dict]) -> str:
    street = position.get("Street", "unknown street")
    lat    = position.get("Latitude",  "N/A")
    lon    = position.get("Longitude", "N/A")
    lines  = [f"Location: {street} (lat={lat}, lon={lon})."]
    if detections:
        lines.append(f"Detected ({len(detections)}):")
        for d in detections:
            dist  = d.get("distance_m")
            angle = d.get("angle_deg")
            lines.append(f"  - {d.get('label','?')}: "
                         f"{f'{dist:.1f}m' if dist else '?'}"
                         f"{f', {angle:.0f}deg' if angle else ''}")
    else:
        lines.append("No hazards detected.")

    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=_ADVISORY_SYSTEM,
        messages=[{"role": "user", "content": "\n".join(lines)}],
    )
    return msg.content[0].text.strip()


# ── Session Q&A ───────────────────────────────────────────────────────────────

_QA_SYSTEM = """You are ADA, an AI driving assistant for Berkeley, CA.
The user is a driver asking about road conditions along their planned route.

Guidelines:
- Answer ONLY based on the events listed in the context. Do not mention, infer,
  or invent anything about streets not listed.
- For on-route events: only discuss obstacles that are AHEAD of the driver
  (positive distance along the route). Ignore anything the driver has already passed.
- For off-route events (labeled "Off-route events"): these were fetched because the
  user explicitly asked about a specific street. Answer about them but clearly note
  they are off the planned route.
- If a street appears under "Streets checked but no current events found", tell the
  user that street was checked and is currently clear — no active events.
- If the context contains a "Possible spelling correction" note, tell the user the
  street name wasn't recognised and ask if they meant the suggested street. Do not
  make up any data about the unrecognised name.
- If the user asks about a street that appears in none of the sections and no
  spelling suggestion exists, say you have no data for that street.
- Be concise — the user is driving. Lead with the most important hazard first.
- Include street name, distance, and hazard type when available.
- If there are no hazards, say so clearly and briefly.
- Never make up events not listed in the context.
- Use natural, spoken language.
- For parking questions: if a "Curb parking" section appears in the context, use
  only those figures. Report the best chance to find a curb spot and name the street.
  Non-colored curb figures vary by street type and time of day.
- For blue curb questions: only report blue curb information if a "Blue curb" section
  appears in the context. Blue curbs are 50% occupied at all times. Do not mention
  blue curbs unless the user explicitly asked about them.
- If the context contains a "Cross street needed" note, report the best parking
  availability found on that street, then ask the user to name a cross street or
  nearby intersection so you can give more precise block-level information.
- "Streets checked but no current events found" refers to TRAFFIC events only,
  never to parking availability. Do not use it to answer parking questions."""


def _build_context(location: dict, nearby: list[dict]) -> str:
    """Build the location+objects context block prepended to each conversation."""
    address   = location.get("address", "unknown")
    bearing   = location.get("bearing", 0)
    direction = location.get("bearing_direction", "N")
    lat       = location.get("lat", "?")
    lon       = location.get("lon", "?")

    destination = location.get("destination", "")
    lines = [
        f"Driver location: {address} (lat={lat}, lon={lon})",
        f"Heading: {direction} ({bearing} deg)",
        "",
    ]

    if destination:
        lines.append(f"Route: {address} → {destination}")
        scope_label = "along the route corridor"
    else:
        scope_label = "within 500m"

    route_events     = [o for o in nearby if not o.get("_off_route")]
    off_route_events = [o for o in nearby if     o.get("_off_route")]

    if route_events:
        lines.append(f"Active traffic events {scope_label} ({len(route_events)} total):")
        for obj in route_events:
            lines.append(_obj_summary(obj))
    else:
        lines.append(f"No active traffic events {scope_label}.")

    if off_route_events:
        lines.append("")
        lines.append(f"Off-route events (user asked about specific streets,"
                     f" {len(off_route_events)} total):")
        for obj in off_route_events:
            lines.append(_obj_summary(obj))

    # Streets that were looked up but had no active events
    checked       = location.get("checked_streets", [])
    found_streets = {o.get("street", "").lower() for o in off_route_events}
    empty_streets = [s for s in checked if s.lower() not in found_streets]
    if empty_streets:
        lines.append("")
        lines.append("Streets checked but no current events found: "
                     + ", ".join(empty_streets))

    # Fuzzy spelling suggestions for unrecognised street names
    suggestions = location.get("street_suggestions", {})
    if suggestions:
        lines.append("")
        for phrase, canonical in suggestions.items():
            lines.append(f"Possible spelling correction: '{phrase}' not found"
                         f" — did the user mean '{canonical}'?")

    # Curb parking simulation (non-colored curbs only)
    parking = location.get("parking")
    if parking:
        anchor    = parking.get("anchor_label", "requested area")
        time_lbl  = "daytime" if parking.get("daytime", True) else "nighttime"

        # Non-colored curb section (skip if no regular blocks found)
        if parking.get("blocks"):
            lines.append("")
            lines.append(f"Curb parking near {anchor}"
                         f" (non-colored curbs, simulated, {time_lbl}):")
            lines.append(f"  Best chance to find a spot: {parking['best_chance']}%"
                         f" on {parking['best_street']}")
            for b in parking["blocks"]:
                stype = b.get("street_type", "")
                if stype == "motorway":
                    lines.append(f"  - {b['street']}: no parking (motorway/ramp)")
                else:
                    lines.append(f"  - {b['street']} ({stype}): {b['chance']}% chance"
                                 f" ({b['occupancy']}% occupied)")

        if parking.get("ask_cross_street"):
            lines.append(
                f"Cross street needed: the user asked about parking on "
                f"{parking['best_street']} without specifying a cross street. "
                f"After reporting the best availability found above, ask the user "
                f"which cross street or block they are interested in."
            )

        # Blue curb section — only present when user explicitly asked
        blue = parking.get("blue_curb")
        if blue:
            lines.append("")
            lines.append(f"Blue curb (disabled parking) near {anchor} (simulated):")
            lines.append(f"  Occupancy: {blue['occupancy']}% — "
                         f"chance to find a blue curb spot: {blue['chance']}%")
            lines.append("  Blue curbs are reserved for vehicles with a disabled"
                         " placard or license plate only.")

    return "\n".join(lines)


def answer_question(question: str,
                    location: dict,
                    nearby_objects: list[dict],
                    history: list[dict]) -> tuple[str, dict]:
    """
    Answer a driving question using Claude with full session history.

    Args:
        question:       The user's latest question.
        location:       Dict with address, lat, lon, bearing, bearing_direction.
        nearby_objects: Active objects near the user from find_nearby_objects().
        history:        Prior messages [{role, content}, ...].

    Returns:
        (answer_text, usage_dict) where usage_dict has input_tokens / output_tokens.
    """
    context = _build_context(location, nearby_objects)

    # Inject location context as the first user turn if history is empty,
    # otherwise prepend it to the current question so it stays fresh.
    context_note = f"[Current conditions]\n{context}\n\n[Question]\n{question}"

    messages = list(history) + [{"role": "user", "content": context_note}]

    # Pass address as user_id so calls are labelled in the Anthropic Console
    address_tag = location.get("address", "unknown")[:512]
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=_QA_SYSTEM,
        messages=messages,
        metadata={"user_id": address_tag},
        timeout=25.0,
    )
    usage = {
        "input_tokens":  msg.usage.input_tokens,
        "output_tokens": msg.usage.output_tokens,
        "model":         "claude-haiku-4-5-20251001",
    }
    return msg.content[0].text.strip(), usage
