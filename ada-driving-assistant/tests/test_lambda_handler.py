"""
Tests for the Lambda handler in app.py.

Primary regression target: aws-wsgi returns statusCode as a string ("200"),
which API Gateway HTTP API v2 rejects with a 503.  The handler must convert
it to an int before returning.
"""

import json
import sys
import types
import unittest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Stubs — must be installed before app.py is imported.
# Each stub is a plain ModuleType so app.py's top-level imports succeed.
# ---------------------------------------------------------------------------

def _stub(name, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


# awsgi — critical: without this stub the try/except ImportError block in
# app.py is skipped and handler / _normalise_event are never defined.
_awsgi_default = {"statusCode": 200, "body": "{}", "headers": {}}
_awsgi = _stub("awsgi", response=MagicMock(return_value=_awsgi_default))

# boto3
_dynamo_table = MagicMock()
_dynamo_table.get_item.return_value = {"Item": None}
_dynamo_resource = MagicMock()
_dynamo_resource.Table.return_value = _dynamo_table
_boto3 = _stub("boto3")
_boto3.client   = MagicMock(return_value=MagicMock())
_boto3.resource = MagicMock(return_value=_dynamo_resource)

_stub("anthropic", Anthropic=MagicMock)
_stub("dotenv", load_dotenv=lambda: None)
_stub("sessions",
      list_sessions=lambda: [],
      find_session=lambda *a, **k: None,
      get_session=lambda sid: None,
      create_session=MagicMock(return_value={"id": "s1"}),
      touch_session=lambda *a: None,
      get_history=lambda *a, **k: [],
      add_message=lambda *a: None)
_stub("assistant",
      answer_question=MagicMock(return_value=("ok", {})))
_stub("events",
      put_event=MagicMock(),
      clear_event=MagicMock(),
      event_lat_lon=MagicMock(return_value=(37.87, -122.27)),
      get_events_by_city=MagicMock(return_value=[]),
      get_events_by_street=MagicMock(return_value=[]),
      get_events_near=MagicMock(return_value=[]))
_stub("location",
      find_nearby_objects=MagicMock(return_value=[]),
      find_objects_on_street=MagicMock(return_value=[]),
      find_street_suggestions=MagicMock(return_value={}),
      find_streets_mentioned=MagicMock(return_value=[]),
      geocode_address=MagicMock(return_value=None),
      object_center=MagicMock(return_value=(37.87, -122.27)),
      random_location=MagicMock(return_value={"address": "Test St"}),
      bearing_to_direction=MagicMock(return_value="N"),
      find_objects_along_route=MagicMock(return_value=[]))
_stub("parking", get_parking_context=MagicMock(return_value=None))

import app as _app  # noqa: E402


# ── Helpers ────────────────────────────────────────────────────────────────

def _v2_event(method="GET", path="/", body=None, qs="", stage="prod"):
    return {
        "requestContext": {
            "http": {"method": method, "path": f"/{stage}{path}"},
            "stage": stage,
        },
        "rawQueryString": qs,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body) if body else None,
        "isBase64Encoded": False,
    }


def _v1_event(method="GET", path="/", body=None):
    return {
        "httpMethod": method,
        "path": path,
        "queryStringParameters": None,
        "headers": {},
        "body": json.dumps(body) if body else None,
        "isBase64Encoded": False,
    }


# ── Tests: statusCode must always be int ───────────────────────────────────

class TestStatusCodeIsInt(unittest.TestCase):
    """
    Regression: aws-wsgi 0.2.7 returns statusCode as a string.
    API Gateway HTTP API v2 rejects string statusCode with a 503 Service Unavailable.
    The handler must convert it to int before returning.
    """

    def _call(self, awsgi_resp):
        with patch("awsgi.response", return_value=awsgi_resp):
            return _app.handler(_v2_event(), MagicMock())

    def test_string_200_converted_to_int(self):
        resp = self._call({"statusCode": "200", "body": "{}", "headers": {}})
        self.assertIsInstance(resp["statusCode"], int)
        self.assertEqual(resp["statusCode"], 200)

    def test_string_400_converted_to_int(self):
        resp = self._call({"statusCode": "400", "body": '{"error":"x"}', "headers": {}})
        self.assertIsInstance(resp["statusCode"], int)
        self.assertEqual(resp["statusCode"], 400)

    def test_string_500_converted_to_int(self):
        resp = self._call({"statusCode": "500", "body": '{"error":"x"}', "headers": {}})
        self.assertIsInstance(resp["statusCode"], int)

    def test_int_statuscode_unchanged(self):
        resp = self._call({"statusCode": 200, "body": "{}", "headers": {}})
        self.assertIsInstance(resp["statusCode"], int)
        self.assertEqual(resp["statusCode"], 200)

    def test_real_route_statuscode_is_int(self):
        """End-to-end through Flask: statusCode in handler return is always int."""
        resp = _app.handler(_v2_event("GET", "/api/session/list"), MagicMock())
        self.assertIsInstance(resp["statusCode"], int,
                              f"statusCode type was {type(resp['statusCode'])}")


# ── Tests: _normalise_event ────────────────────────────────────────────────

class TestNormaliseEvent(unittest.TestCase):

    def test_strips_stage_prefix_from_path(self):
        event = _v2_event("GET", "/api/session/list", stage="prod")
        n = _app._normalise_event(event)
        self.assertEqual(n["path"], "/api/session/list")

    def test_v1_event_passed_through(self):
        event = _v1_event("GET", "/api/session/list")
        n = _app._normalise_event(event)
        self.assertIn("httpMethod", n)
        self.assertEqual(n["path"], "/api/session/list")

    def test_query_string_decoded(self):
        event = _v2_event("GET", "/api/events/block",
                          qs="street=Telegraph+Ave&lat=37.85&lon=-122.26")
        n = _app._normalise_event(event)
        qs = n["queryStringParameters"]
        self.assertEqual(qs["street"], "Telegraph Ave")
        self.assertEqual(qs["lat"], "37.85")

    def test_method_preserved(self):
        event = _v2_event("POST", "/api/ask", body={"q": "test"})
        n = _app._normalise_event(event)
        self.assertEqual(n["httpMethod"], "POST")

    def test_empty_qs_gives_none_params(self):
        event = _v2_event("GET", "/api/session/list", qs="")
        n = _app._normalise_event(event)
        self.assertIsNone(n["queryStringParameters"])


if __name__ == "__main__":
    unittest.main()
