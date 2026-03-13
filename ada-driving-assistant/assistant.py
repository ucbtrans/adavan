"""
ADA Driving Assistant
Uses Claude API to generate real-time driving advisories from detection and GPS data.
"""

import os
import json
from dotenv import load_dotenv
from anthropic import Anthropic

load_dotenv()
client = Anthropic()  # uses ANTHROPIC_API_KEY env var

SYSTEM_PROMPT = """You are an AI co-pilot assistant for an autonomous ADA van operating in work zones.
Your job is to produce concise, clear driving advisories for the human driver based on:
- Current street/GPS position
- Objects detected ahead (traffic cones, construction workers)
- Their approximate distances and positions

Rules:
- Keep advisories under 3 sentences.
- Always mention detected hazards and recommended action.
- If nothing significant is detected, give a brief all-clear.
- Use plain language, no jargon.
- Prioritize worker safety above all else."""


def build_prompt(position: dict, detections: list[dict]) -> str:
    """
    Build the user message from position and detection data.

    Args:
        position: dict with keys Time, Latitude, Longitude, Street
        detections: list of dicts with keys label, distance_m, angle_deg
    """
    street = position.get("Street", "unknown street")
    lat = position.get("Latitude", "N/A")
    lon = position.get("Longitude", "N/A")

    lines = [f"Current location: {street} (lat={lat}, lon={lon})."]

    if detections:
        lines.append(f"Objects detected ({len(detections)} total):")
        for d in detections:
            label = d.get("label", "unknown")
            dist = d.get("distance_m")
            angle = d.get("angle_deg")
            dist_str = f"{dist:.1f}m away" if dist is not None else "distance unknown"
            angle_str = f", {angle:.0f}° off center" if angle is not None else ""
            lines.append(f"  - {label}: {dist_str}{angle_str}")
    else:
        lines.append("No hazards detected ahead.")

    return "\n".join(lines)


def get_advisory(position: dict, detections: list[dict]) -> str:
    """
    Call Claude to generate a driving advisory.

    Returns:
        Advisory string from Claude.
    """
    user_message = build_prompt(position, detections)

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    return message.content[0].text.strip()


def get_advisory_from_files(position_path: str, detections_path: str | None = None) -> str:
    """
    Load data from files and return advisory.

    Args:
        position_path: Path to van_position.json
        detections_path: Optional path to detections JSON file
    """
    with open(position_path) as f:
        position = json.load(f)

    detections = []
    if detections_path and os.path.exists(detections_path):
        with open(detections_path) as f:
            detections = json.load(f)

    return get_advisory(position, detections)


if __name__ == "__main__":
    # Demo with sample data
    sample_position = {
        "Time": "2026-03-13T10:00:00",
        "Latitude": 37.8716,
        "Longitude": -122.2727,
        "Street": "Telegraph Ave",
    }
    sample_detections = [
        {"label": "traffic cone", "distance_m": 12.3, "angle_deg": -5.0},
        {"label": "traffic cone", "distance_m": 14.1, "angle_deg": 8.0},
        {"label": "construction worker", "distance_m": 20.0, "angle_deg": 15.0},
    ]

    print("Generating advisory...")
    advisory = get_advisory(sample_position, sample_detections)
    print(f"\nAdvisory:\n{advisory}")
