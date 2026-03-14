"""
Session management for ADA Driving Assistant.
Stores up to MAX_SESSIONS sessions in sessions.json.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone

MAX_SESSIONS = 12
_SESSIONS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sessions.json")


# ── Persistence ───────────────────────────────────────────────────────────────

def _load() -> list[dict]:
    if os.path.exists(_SESSIONS_FILE):
        try:
            with open(_SESSIONS_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return []


def _save(sessions: list[dict]) -> None:
    with open(_SESSIONS_FILE, "w") as f:
        json.dump(sessions, f, indent=2)


# ── Public API ────────────────────────────────────────────────────────────────

def create_session(address: str, lat: float, lon: float,
                   bearing: int, bearing_direction: str,
                   street: str = "") -> dict:
    """Create and persist a new session. Returns the session dict."""
    sessions = _load()

    session = {
        "id":                str(uuid.uuid4()),
        "started_at":        datetime.now(timezone.utc).isoformat(),
        "address":           address,
        "lat":               lat,
        "lon":               lon,
        "bearing":           bearing,
        "bearing_direction": bearing_direction,
        "street":            street,
        "messages":          [],
    }

    sessions.insert(0, session)         # newest first
    sessions = sessions[:MAX_SESSIONS]  # cap at 12
    _save(sessions)
    return session


def get_session(session_id: str) -> dict | None:
    for s in _load():
        if s["id"] == session_id:
            return s
    return None


def add_message(session_id: str, role: str, content: str) -> bool:
    """Append a message to a session. Returns True on success."""
    sessions = _load()
    for s in sessions:
        if s["id"] == session_id:
            s["messages"].append({
                "role":      role,
                "content":   content,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            _save(sessions)
            return True
    return False


def list_sessions() -> list[dict]:
    """Return all sessions (newest first), without message bodies for brevity."""
    sessions = _load()
    summaries = []
    for s in sessions:
        summaries.append({
            "id":                s["id"],
            "started_at":        s["started_at"],
            "address":           s["address"],
            "bearing":           s["bearing"],
            "bearing_direction": s["bearing_direction"],
            "street":            s.get("street", ""),
            "message_count":     len(s.get("messages", [])),
        })
    return summaries


def get_history(session_id: str) -> list[dict]:
    """Return the messages list for a session (role + content only)."""
    s = get_session(session_id)
    if not s:
        return []
    return [{"role": m["role"], "content": m["content"]}
            for m in s.get("messages", [])]
