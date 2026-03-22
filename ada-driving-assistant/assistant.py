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
- Answer ONLY based on the events listed in the context. These have already been
  pre-filtered to the driver's route — do not mention, infer, or invent anything
  about streets not listed.
- Only discuss obstacles or conditions that are AHEAD of the driver (positive
  distance along the route). Ignore anything the driver has already passed.
- If the user explicitly asks about a specific street by name, check if it appears
  in the context. If not, say you have no data for that street on this route.
- Be concise — the user is driving. Lead with the most important hazard first.
- Include street name, distance, and hazard type when available.
- If there are no hazards, say so clearly and briefly.
- Never make up events not listed in the context.
- Use natural, spoken language."""


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

    if nearby:
        lines.append(f"Active traffic events {scope_label} ({len(nearby)} total):")
        for obj in nearby:
            lines.append(_obj_summary(obj))
    else:
        lines.append(f"No active traffic events {scope_label}.")

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
    )
    usage = {
        "input_tokens":  msg.usage.input_tokens,
        "output_tokens": msg.usage.output_tokens,
        "model":         "claude-haiku-4-5-20251001",
    }
    return msg.content[0].text.strip(), usage
