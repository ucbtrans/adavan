"""
Session management for ADA Driving Assistant.
Backend: DynamoDB when SESSIONS_TABLE env var is set; local JSON file otherwise.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone

MAX_SESSIONS   = 12
_TABLE_NAME    = os.environ.get("SESSIONS_TABLE")
_SESSIONS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sessions.json")
_TTL_DAYS      = 30


# ── Backend selector ──────────────────────────────────────────────────────────

def _use_dynamo() -> bool:
    return bool(_TABLE_NAME)


def _table():
    import boto3
    return boto3.resource("dynamodb").Table(_TABLE_NAME)


# ── File backend (local dev) ──────────────────────────────────────────────────

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


# ── DynamoDB helpers ──────────────────────────────────────────────────────────

def _ttl_timestamp() -> int:
    from datetime import timedelta
    return int((datetime.now(timezone.utc) + timedelta(days=_TTL_DAYS)).timestamp())


def _dynamo_to_session(item: dict) -> dict:
    """Convert DynamoDB item (Decimal types etc.) to plain Python dict."""
    import decimal
    def _fix(v):
        if isinstance(v, decimal.Decimal):
            return int(v) if v == int(v) else float(v)
        if isinstance(v, list):
            return [_fix(i) for i in v]
        if isinstance(v, dict):
            return {k: _fix(val) for k, val in v.items()}
        return v
    return {k: _fix(v) for k, v in item.items()}


# ── Public API ────────────────────────────────────────────────────────────────

def create_session(address: str, lat: float, lon: float,
                   bearing: int, bearing_direction: str,
                   street: str = "",
                   destination: str = "",
                   dest_lat: float | None = None,
                   dest_lon: float | None = None,
                   route_coords: list | None = None,
                   route_streets: list | None = None) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    session = {
        "id":                str(uuid.uuid4()),
        "started_at":        now,
        "last_active_at":    now,
        "address":           address,
        "lat":               lat,
        "lon":               lon,
        "bearing":           bearing,
        "bearing_direction": bearing_direction,
        "street":            street,
        "destination":       destination,
        "dest_lat":          dest_lat,
        "dest_lon":          dest_lon,
        "route_coords_json":  json.dumps(route_coords)  if route_coords  else "",
        "route_streets_json": json.dumps(route_streets) if route_streets else "",
        "messages":          [],
    }

    if _use_dynamo():
        from decimal import Decimal
        item = dict(session, ttl=_ttl_timestamp())
        item["lat"] = Decimal(str(lat))
        item["lon"] = Decimal(str(lon))
        if dest_lat is not None:
            item["dest_lat"] = Decimal(str(dest_lat))
        if dest_lon is not None:
            item["dest_lon"] = Decimal(str(dest_lon))
        _table().put_item(Item=item)
        # Enforce MAX_SESSIONS: delete oldest if over limit
        _enforce_max_sessions_dynamo()
    else:
        sessions = _load()
        sessions.insert(0, session)
        sessions = sessions[:MAX_SESSIONS]
        _save(sessions)

    return session


def get_session(session_id: str) -> dict | None:
    if _use_dynamo():
        resp = _table().get_item(Key={"id": session_id})
        item = resp.get("Item")
        return _dynamo_to_session(item) if item else None
    else:
        for s in _load():
            if s["id"] == session_id:
                return s
        return None


def add_message(session_id: str, role: str, content: str) -> bool:
    now = datetime.now(timezone.utc).isoformat()
    if _use_dynamo():
        try:
            _table().update_item(
                Key={"id": session_id},
                UpdateExpression=(
                    "SET messages = list_append(messages, :msg), "
                    "last_active_at = :ts"
                ),
                ExpressionAttributeValues={
                    ":msg": [{"role": role, "content": content, "timestamp": now}],
                    ":ts":  now,
                },
            )
            return True
        except Exception:
            return False
    else:
        sessions = _load()
        for s in sessions:
            if s["id"] == session_id:
                s["messages"].append({"role": role, "content": content, "timestamp": now})
                s["last_active_at"] = now
                _save(sessions)
                return True
        return False


def touch_session(session_id: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    if _use_dynamo():
        try:
            _table().update_item(
                Key={"id": session_id},
                UpdateExpression="SET last_active_at = :ts",
                ExpressionAttributeValues={":ts": now},
            )
        except Exception:
            pass
    else:
        sessions = _load()
        for s in sessions:
            if s["id"] == session_id:
                s["last_active_at"] = now
                _save(sessions)
                return


def list_sessions() -> list[dict]:
    if _use_dynamo():
        resp  = _table().scan()
        items = [_dynamo_to_session(i) for i in resp.get("Items", [])]
        items.sort(key=lambda x: x.get("last_active_at", x["started_at"]), reverse=True)
        sessions = items[:MAX_SESSIONS]
    else:
        sessions = _load()

    return [
        {
            "id":                s["id"],
            "started_at":        s["started_at"],
            "last_active_at":    s.get("last_active_at", s["started_at"]),
            "address":           s["address"],
            "bearing":           s["bearing"],
            "bearing_direction": s["bearing_direction"],
            "street":            s.get("street", ""),
            "destination":       s.get("destination", ""),
            "dest_lat":          s.get("dest_lat"),
            "dest_lon":          s.get("dest_lon"),
            "message_count":     len(s.get("messages", [])),
        }
        for s in sessions
    ]


def find_session(address: str, bearing: int) -> dict | None:
    """Return the most-recent session matching address + bearing, or None."""
    if _use_dynamo():
        resp  = _table().scan()
        items = [_dynamo_to_session(i) for i in resp.get("Items", [])]
        items.sort(key=lambda x: x.get("last_active_at", x["started_at"]), reverse=True)
        for item in items:
            if item.get("address") == address and item.get("bearing") == bearing:
                return item
        return None
    else:
        for s in _load():
            if s.get("address") == address and s.get("bearing") == bearing:
                return s
        return None


def get_history(session_id: str, session: dict | None = None) -> list[dict]:
    s = session if session is not None else get_session(session_id)
    if not s:
        return []
    return [{"role": m["role"], "content": m["content"]}
            for m in s.get("messages", [])]


# ── DynamoDB housekeeping ─────────────────────────────────────────────────────

def _enforce_max_sessions_dynamo() -> None:
    """Delete oldest sessions if table exceeds MAX_SESSIONS."""
    resp  = _table().scan(ProjectionExpression="id, last_active_at, started_at")
    items = resp.get("Items", [])
    if len(items) <= MAX_SESSIONS:
        return
    items.sort(key=lambda x: x.get("last_active_at", x.get("started_at", "")))
    for item in items[:len(items) - MAX_SESSIONS]:
        _table().delete_item(Key={"id": item["id"]})
