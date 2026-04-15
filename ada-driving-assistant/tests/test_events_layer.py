"""
Tests for events.py — DynamoDB CRUD layer.

This file imports the REAL events.py (not the stub used by the app tests).
It installs its own boto3 stub and removes any previously cached stub for
the events module so the real implementation is loaded.
"""

import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Remove any previously cached stub for events and boto3 so we load the
# real events.py (other test files stub events as a MagicMock module).
# ---------------------------------------------------------------------------

for _key in ("events", "boto3", "boto3.dynamodb", "boto3.dynamodb.conditions"):
    sys.modules.pop(_key, None)


def _make_conditions_stub():
    """Lightweight Key/Attr stubs that support the operators events.py uses."""
    mod = types.ModuleType("boto3.dynamodb.conditions")

    class Key:
        def __init__(self, name): self._n = name
        def eq(self, v): return f"Key({self._n}).eq({v!r})"

    class Attr:
        def __init__(self, name): self._n = name
        def gt(self, v):  return _Cond(f"Attr({self._n}).gt({v!r})")
        def lt(self, v):  return _Cond(f"Attr({self._n}).lt({v!r})")
        def lte(self, v): return _Cond(f"Attr({self._n}).lte({v!r})")

    class _Cond:
        def __init__(self, s): self._s = s
        def __and__(self, other): return _Cond(f"({self._s} AND {other._s})")
        def __repr__(self): return self._s

    mod.Key  = Key
    mod.Attr = Attr
    return mod


_boto3 = types.ModuleType("boto3")
_boto3.resource = MagicMock()
_boto3.client   = MagicMock()
sys.modules["boto3"]                       = _boto3
sys.modules["boto3.dynamodb"]              = types.ModuleType("boto3.dynamodb")
sys.modules["boto3.dynamodb.conditions"]   = _make_conditions_stub()

import events as ev  # noqa: E402  (imports real events.py now)


# ── event_to_dynamo ────────────────────────────────────────────────────────

class TestEventToDynamo(unittest.TestCase):

    def _base(self, **kw):
        now = datetime.now(timezone.utc)
        e = {
            "id": "test-uuid-1",
            "type": "single_cone",
            "street": "Telegraph Ave",
            "city": "Berkeley",
            "lat": 37.866,
            "lon": -122.259,
            "active_at":   now.isoformat(),
            "inactive_at": (now + timedelta(hours=2)).isoformat(),
        }
        e.update(kw)
        return e

    def test_id_renamed_to_event_id(self):
        item = ev.event_to_dynamo(self._base())
        self.assertIn("event_id", item)
        self.assertNotIn("id", item)
        self.assertEqual(item["event_id"], "test-uuid-1")

    def test_geohash6_is_six_chars(self):
        item = ev.event_to_dynamo(self._base())
        self.assertIn("geohash6", item)
        self.assertEqual(len(item["geohash6"]), 6)

    def test_geohash7_is_seven_chars(self):
        item = ev.event_to_dynamo(self._base())
        self.assertIn("geohash7", item)
        self.assertEqual(len(item["geohash7"]), 7)

    def test_ttl_at_least_7_days_after_inactive_at(self):
        """TTL must be inactive_at + 7 days so DynamoDB doesn't auto-delete too early."""
        inactive = datetime.now(timezone.utc) + timedelta(hours=2)
        item = ev.event_to_dynamo(self._base(inactive_at=inactive.isoformat()))
        expected_min = int(inactive.timestamp()) + 7 * 86400
        self.assertGreaterEqual(item["ttl"], expected_min)

    def test_floats_converted_to_decimal(self):
        item = ev.event_to_dynamo(self._base())
        self.assertIsInstance(item["lat"], Decimal)
        self.assertIsInstance(item["lon"], Decimal)

    def test_lat_lon_at_top_level(self):
        item = ev.event_to_dynamo(self._base())
        self.assertAlmostEqual(float(item["lat"]), 37.866, places=3)
        self.assertAlmostEqual(float(item["lon"]), -122.259, places=3)

    def test_event_id_generated_when_id_missing(self):
        e = self._base()
        del e["id"]
        item = ev.event_to_dynamo(e)
        self.assertIn("event_id", item)
        self.assertGreater(len(item["event_id"]), 0)

    def test_invalid_inactive_at_gets_fallback_ttl(self):
        item = ev.event_to_dynamo(self._base(inactive_at="not-a-date"))
        self.assertIn("ttl", item)
        now_ts = int(datetime.now(timezone.utc).timestamp())
        self.assertGreater(item["ttl"], now_ts + 13 * 86400)


# ── dynamo_to_event ───────────────────────────────────────────────────────

class TestDynamoToEvent(unittest.TestCase):

    def _item(self, **kw):
        base = {
            "event_id":   "ev-1",
            "street":     "Telegraph Ave",
            "lat":        Decimal("37.866"),
            "lon":        Decimal("-122.259"),
            "geohash6":   "9q9p3u",
            "geohash7":   "9q9p3uf",
            "ttl":        9999999,
            "active_at":  "2026-01-01T00:00:00+00:00",
            "inactive_at": "2026-01-02T00:00:00+00:00",
        }
        base.update(kw)
        return base

    def test_decimals_become_float(self):
        result = ev.dynamo_to_event(self._item())
        self.assertIsInstance(result["lat"], float)
        self.assertIsInstance(result["lon"], float)

    def test_internal_fields_stripped(self):
        result = ev.dynamo_to_event(self._item())
        self.assertNotIn("geohash6", result)
        self.assertNotIn("geohash7", result)
        self.assertNotIn("ttl", result)

    def test_values_preserved(self):
        result = ev.dynamo_to_event(self._item())
        self.assertAlmostEqual(result["lat"], 37.866, places=3)
        self.assertEqual(result["street"], "Telegraph Ave")

    def test_round_trip(self):
        original = {
            "id": "ev-rt",
            "type": "single_cone",
            "street": "Telegraph Ave",
            "city": "Berkeley",
            "lat": 37.866,
            "lon": -122.259,
            "active_at":   datetime.now(timezone.utc).isoformat(),
            "inactive_at": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
        }
        item   = ev.event_to_dynamo(original)
        result = ev.dynamo_to_event(item)
        self.assertAlmostEqual(result["lat"], 37.866, places=3)
        self.assertAlmostEqual(result["lon"], -122.259, places=3)
        self.assertEqual(result["street"], "Telegraph Ave")


# ── event_lat_lon ─────────────────────────────────────────────────────────

class TestEventLatLon(unittest.TestCase):

    def test_top_level_lat_lon(self):
        lat, lon = ev.event_lat_lon({"lat": 37.87, "lon": -122.27})
        self.assertAlmostEqual(lat, 37.87)
        self.assertAlmostEqual(lon, -122.27)

    def test_nested_coordinates(self):
        lat, lon = ev.event_lat_lon({"coordinates": {"lat": 37.87, "lon": -122.27}})
        self.assertAlmostEqual(lat, 37.87)
        self.assertAlmostEqual(lon, -122.27)

    def test_polygon_centroid(self):
        polygon = [
            {"lat": 37.860, "lon": -122.260},
            {"lat": 37.862, "lon": -122.258},
            {"lat": 37.864, "lon": -122.260},
        ]
        lat, lon = ev.event_lat_lon({"polygon": polygon})
        self.assertAlmostEqual(lat, (37.860 + 37.862 + 37.864) / 3, places=4)

    def test_road_barrier_midpoint(self):
        lat, lon = ev.event_lat_lon({
            "left_coordinates":  {"lat": 37.860, "lon": -122.260},
            "right_coordinates": {"lat": 37.862, "lon": -122.258},
        })
        self.assertAlmostEqual(lat, (37.860 + 37.862) / 2)
        self.assertAlmostEqual(lon, (-122.260 + -122.258) / 2)

    def test_no_coords_returns_zero(self):
        lat, lon = ev.event_lat_lon({})
        self.assertEqual(lat, 0.0)
        self.assertEqual(lon, 0.0)


# ── get_events_by_city: FilterExpression regression ────────────────────────

class TestGetEventsByCityFilterExpression(unittest.TestCase):
    """
    Regression: without FilterExpression, DynamoDB returned all ~31 K items
    (mostly expired), causing 10+ second queries and Lambda timeouts.
    """

    def _mock_table(self, items=None):
        t = MagicMock()
        t.query.return_value = {"Items": items or []}
        return t

    def test_filter_expression_sent_to_dynamo(self):
        t = self._mock_table()
        with patch.object(ev, "_get_table", return_value=t):
            ev.get_events_by_city("Berkeley", datetime.now(timezone.utc))
        kwargs = t.query.call_args[1]
        self.assertIn("FilterExpression", kwargs,
                      "Must include FilterExpression — without it all expired "
                      "items are transferred and the Lambda times out")

    def test_city_index_used(self):
        t = self._mock_table()
        with patch.object(ev, "_get_table", return_value=t):
            ev.get_events_by_city("Berkeley", datetime.now(timezone.utc))
        kwargs = t.query.call_args[1]
        self.assertEqual(kwargs.get("IndexName"), "city-index")

    def test_returns_list(self):
        t = self._mock_table()
        with patch.object(ev, "_get_table", return_value=t):
            result = ev.get_events_by_city("Berkeley", datetime.now(timezone.utc))
        self.assertIsInstance(result, list)

    def test_pagination_combines_all_pages(self):
        page1 = {
            "event_id": "ev-1", "street": "Telegraph",
            "lat": Decimal("37.86"), "lon": Decimal("-122.26"),
            "city": "Berkeley",
            "active_at":   "2026-01-01T00:00:00+00:00",
            "inactive_at": "2099-01-01T00:00:00+00:00",
        }
        page2 = {**page1, "event_id": "ev-2"}
        t = MagicMock()
        t.query.side_effect = [
            {"Items": [page1], "LastEvaluatedKey": {"street": "Telegraph", "event_id": "ev-1"}},
            {"Items": [page2]},
        ]
        with patch.object(ev, "_get_table", return_value=t):
            result = ev.get_events_by_city("Berkeley", datetime.now(timezone.utc))
        self.assertEqual(len(result), 2)
        self.assertEqual(t.query.call_count, 2)


if __name__ == "__main__":
    unittest.main()
