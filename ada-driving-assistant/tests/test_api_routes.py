"""
Tests for Flask API routes in app.py.

Primary regression targets:
  1. /api/ask always returns JSON with "answer" or "error" — never a missing
     field that renders as "(empty response)" in the UI.
  2. /api/ask returns a JSON error when Anthropic fails — not a Lambda crash
     that surfaces as "Connection error" in the browser.
  3. All routes return valid JSON on both happy-path and bad input.

app.py is imported for real (network is blocked in tests/conftest.py); each
test class patches only the collaborators its routes touch, so these tests do
not depend on which test file pytest imported first.
"""

import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import app as _app

_FAKE_SESSION = {
    "id": "sess-1",
    "address": "2020 Telegraph Ave, Berkeley",
    "lat": 37.866,
    "lon": -122.259,
    "bearing": 0,
    "bearing_direction": "N",
    "street": "Telegraph Ave",
    "destination": "",
    "dest_lat": None,
    "dest_lon": None,
    "route_coords_json": "",
    "route_streets_json": "",
    "history": [],
}

_FAKE_EVENT = {
    "event_id": "ev-1",
    "type": "single_cone",
    "street": "Telegraph Ave",
    "city": "Berkeley",
    "lat": 37.866,
    "lon": -122.259,
    "active_at":   (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
    "inactive_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
}

_app.app.config["TESTING"] = True
_client = _app.app.test_client()


# ── Helpers ────────────────────────────────────────────────────────────────

def _post(path, body):
    return _client.post(path, data=json.dumps(body),
                        content_type="application/json")

def _get(path):
    return _client.get(path)


class _PatchingTestCase(unittest.TestCase):
    """Starts patches that are undone after each test."""

    def _patch(self, target, attr, **kw):
        p = patch.object(target, attr, **kw)
        mock = p.start()
        self.addCleanup(p.stop)
        return mock


# ── /api/ask regression tests ──────────────────────────────────────────────

class TestAskEndpoint(_PatchingTestCase):

    def setUp(self):
        self._patch(_app.sess, "get_session",
                    new=lambda sid: dict(_FAKE_SESSION) if sid == "sess-1" else None)
        self._patch(_app.sess, "get_history", new=lambda *a, **k: [])
        self._patch(_app.sess, "add_message", new=lambda *a: None)
        self._patch(_app, "get_all_events", return_value=[])
        self.answer = self._patch(_app, "answer_question", return_value=(
            "Clear ahead.",
            {"input_tokens": 10, "output_tokens": 5, "model": "claude-opus-4-6"},
        ))

    def _ask(self, question="Any hazards ahead?", session_id="sess-1", **extra):
        return _post("/api/ask", {"session_id": session_id, "question": question, **extra})

    # -- Happy path -----------------------------------------------------------

    def test_successful_ask_has_answer_key(self):
        """Regression: UI showed '(empty response)' when answer key was missing."""
        r = self._ask()
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertIn("answer", data, "'answer' key must be present on success")
        self.assertIsNotNone(data["answer"])

    def test_answer_is_nonempty_string(self):
        r = self._ask()
        data = r.get_json()
        self.assertIsInstance(data["answer"], str)
        self.assertGreater(len(data["answer"]), 0)

    def test_response_is_valid_json(self):
        r = self._ask()
        self.assertIsNotNone(r.get_json())

    def test_nearby_count_present(self):
        r = self._ask()
        self.assertIn("nearby_count", r.get_json())

    # -- Anthropic failure must return JSON, not crash ----------------------

    def test_anthropic_exception_returns_json_error(self):
        """
        Regression: before the fix, an Anthropic exception propagated to
        API Gateway which returned a 503 with no 'answer'/'error' field,
        causing the JS to show "Connection error".
        """
        self.answer.side_effect = Exception("Anthropic timeout")
        r = self._ask()
        self.assertIn(r.status_code, (500, 503))
        data = r.get_json()
        self.assertIsNotNone(data, "Must return JSON even on Anthropic failure")
        self.assertIn("error", data, "'error' key required on Anthropic failure")

    def test_anthropic_failure_has_no_answer_key(self):
        self.answer.side_effect = Exception("timeout")
        r = self._ask()
        self.assertNotIn("answer", r.get_json())

    # -- Validation -----------------------------------------------------------

    def test_missing_session_id_returns_400(self):
        r = _post("/api/ask", {"question": "hello"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("error", r.get_json())

    def test_missing_question_returns_400(self):
        r = _post("/api/ask", {"session_id": "sess-1"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("error", r.get_json())

    def test_unknown_session_returns_404(self):
        r = _post("/api/ask", {"session_id": "no-such-id", "question": "hi"})
        self.assertEqual(r.status_code, 404)
        self.assertIn("error", r.get_json())


# ── /api/events endpoints ─────────────────────────────────────────────────

class TestEventsEndpoints(_PatchingTestCase):

    def setUp(self):
        self._patch(_app, "get_all_events", return_value=[_FAKE_EVENT])
        self._patch(_app, "get_events_by_city", return_value=[_FAKE_EVENT])
        self.by_street = self._patch(_app, "get_events_by_street",
                                     return_value=[_FAKE_EVENT])
        self.put_event   = self._patch(_app, "put_event")
        self.clear_event = self._patch(_app, "clear_event")

    def test_events_list_returns_events_key(self):
        r = _get("/api/events")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertIn("events", data)
        self.assertIsInstance(data["events"], list)

    def test_events_block_missing_params_returns_400(self):
        r = _get("/api/events/block")
        self.assertEqual(r.status_code, 400)
        self.assertIn("error", r.get_json())

    def test_events_block_missing_lat_returns_400(self):
        r = _get("/api/events/block?street=Telegraph+Ave&lon=-122.26")
        self.assertEqual(r.status_code, 400)

    def test_events_block_valid_returns_events_key(self):
        r = _get("/api/events/block?street=Telegraph+Ave&lat=37.866&lon=-122.259")
        self.assertEqual(r.status_code, 200)
        self.assertIn("events", r.get_json())

    def test_events_block_filters_by_radius(self):
        """Events outside the radius must not come back for the block."""
        far = {**_FAKE_EVENT, "event_id": "ev-far", "lat": 37.9, "lon": -122.4}
        self.by_street.return_value = [_FAKE_EVENT, far]
        r = _get("/api/events/block?street=Telegraph+Ave&lat=37.866&lon=-122.259")
        ids = [e["event_id"] for e in r.get_json()["events"]]
        self.assertEqual(ids, ["ev-1"])

    def test_events_create_missing_field_returns_400(self):
        r = _post("/api/events", {"type": "single_cone", "lat": 37.866})
        self.assertEqual(r.status_code, 400)
        self.assertIn("error", r.get_json())

    def test_events_create_active_at_in_past_returns_400(self):
        past   = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        r = _post("/api/events", {
            "type": "single_cone", "lat": 37.866, "lon": -122.259,
            "street": "Telegraph Ave", "city": "Berkeley",
            "active_at": past, "inactive_at": future,
        })
        self.assertEqual(r.status_code, 400)

    def test_events_create_inactive_before_active_returns_400(self):
        now = datetime.now(timezone.utc)
        r = _post("/api/events", {
            "type": "single_cone", "lat": 37.866, "lon": -122.259,
            "street": "Telegraph Ave", "city": "Berkeley",
            "active_at":   now.isoformat(),
            "inactive_at": (now - timedelta(minutes=5)).isoformat(),
        })
        self.assertEqual(r.status_code, 400)

    def test_events_create_valid_returns_event_id(self):
        now = datetime.now(timezone.utc)
        r = _post("/api/events", {
            "type": "single_cone", "lat": 37.866, "lon": -122.259,
            "street": "Telegraph Ave", "city": "Berkeley",
            "active_at":   now.isoformat(),
            "inactive_at": (now + timedelta(hours=2)).isoformat(),
        })
        self.assertEqual(r.status_code, 200)
        self.assertIn("event_id", r.get_json())
        self.assertEqual(self.put_event.call_count, 1)

    def test_events_clear_missing_street_returns_400(self):
        r = _post("/api/events/ev-1/clear", {})
        self.assertEqual(r.status_code, 400)
        self.assertIn("error", r.get_json())

    def test_events_clear_valid_returns_cleared_true(self):
        r = _post("/api/events/ev-1/clear", {"street": "Telegraph Ave"})
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertIn("cleared", data)
        self.assertTrue(data["cleared"])
        self.assertEqual(self.clear_event.call_args[0][:2], ("Telegraph Ave", "ev-1"))

    def test_events_create_dynamo_failure_returns_json_error(self):
        self.put_event.side_effect = Exception("ProvisionedThroughputExceeded")
        now = datetime.now(timezone.utc)
        r = _post("/api/events", {
            "type": "single_cone", "lat": 37.866, "lon": -122.259,
            "street": "Telegraph Ave", "city": "Berkeley",
            "active_at":   now.isoformat(),
            "inactive_at": (now + timedelta(hours=2)).isoformat(),
        })
        self.assertEqual(r.status_code, 500)
        self.assertIn("error", r.get_json())


# ── Other routes ──────────────────────────────────────────────────────────

class TestOtherRoutes(_PatchingTestCase):

    def setUp(self):
        self._patch(_app.sess, "list_sessions", new=lambda: [dict(_FAKE_SESSION)])
        self._patch(_app.sess, "get_session",
                    new=lambda sid: dict(_FAKE_SESSION) if sid == "sess-1" else None)
        self.geocode = self._patch(_app, "geocode_address", return_value=None)

    def test_session_list_returns_list(self):
        r = _get("/api/session/list")
        self.assertEqual(r.status_code, 200)
        self.assertIsInstance(r.get_json(), list)

    def test_session_get_known_returns_200(self):
        r = _get("/api/session/sess-1")
        self.assertEqual(r.status_code, 200)

    def test_session_get_unknown_returns_404(self):
        r = _get("/api/session/nonexistent")
        self.assertEqual(r.status_code, 404)
        self.assertIn("error", r.get_json())

    def test_geocode_missing_address_returns_400(self):
        r = _post("/api/location/geocode", {})
        self.assertEqual(r.status_code, 400)

    def test_geocode_unknown_address_returns_404(self):
        r = _post("/api/location/geocode", {"address": "nowhere land xyz"})
        self.assertEqual(r.status_code, 404)

    def test_geocode_known_address_returns_coords(self):
        self.geocode.return_value = {"address": "2020 Telegraph Ave",
                                     "lat": 37.866, "lon": -122.259}
        r = _post("/api/location/geocode", {"address": "2020 Telegraph Ave"})
        self.assertEqual(r.status_code, 200)
        self.assertAlmostEqual(r.get_json()["lat"], 37.866, places=3)


if __name__ == "__main__":
    unittest.main()
