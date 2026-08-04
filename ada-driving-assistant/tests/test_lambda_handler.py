"""
Tests for the Lambda handler in app.py.

Primary regression target: aws-wsgi returns statusCode as a string ("200"),
which API Gateway HTTP API v2 rejects with a 503.  The handler must convert
it to an int before returning.

Network access is blocked in tests/conftest.py, so app.py is imported for real.
"""

import json
import unittest
from unittest.mock import MagicMock, patch

import app as _app


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
        # app.py calls awsgi.response(...) by attribute, so patching the module
        # attribute intercepts it without touching app.py's import.
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
        self.assertEqual(resp["statusCode"], 500)

    def test_int_statuscode_unchanged(self):
        resp = self._call({"statusCode": 200, "body": "{}", "headers": {}})
        self.assertIsInstance(resp["statusCode"], int)
        self.assertEqual(resp["statusCode"], 200)

    def test_real_route_statuscode_is_int(self):
        """End-to-end through real aws-wsgi + Flask: statusCode is always int."""
        with patch.object(_app.sess, "list_sessions", return_value=[]):
            resp = _app.handler(_v2_event("GET", "/api/session/list"), MagicMock())
        self.assertIsInstance(resp["statusCode"], int,
                              f"statusCode type was {type(resp['statusCode'])}")
        self.assertEqual(resp["statusCode"], 200)


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
