"""
Tests for simulator.py -- the nightly traffic-event generator.

Events generated here are written to DynamoDB by lambda_function.py and read
back by /api/ask and the fleet map, so what matters is that every event has the
fields those consumers index on (street, city, active window, a position) and
that the random placement lands on the street it claims.
"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import simulator as sim
from events import event_lat_lon
from location import haversine_m
from objects import LIFESPANS, OBJECT_TYPES

DAY = datetime(2026, 8, 4, 0, 0, 0, tzinfo=timezone.utc)

STREETS = [
    {"name": "Telegraph Avenue", "highway": "secondary",
     "lanes_forward": 2, "lanes_backward": 2, "segments": [
         [{"lat": 37.8660, "lon": -122.2588}, {"lat": 37.8700, "lon": -122.2588}]]},
    {"name": "Oxford Street", "highway": "residential",
     "lanes_forward": 1, "lanes_backward": 1, "segments": [
         [{"lat": 37.8720, "lon": -122.2650}, {"lat": 37.8740, "lon": -122.2650}]]},
]


class TestRandomPointOnStreet(unittest.TestCase):

    def test_point_lies_on_the_segment(self):
        st = STREETS[0]
        for _ in range(50):
            lat, lon = sim.random_point_on_street(st)
            self.assertGreaterEqual(lat, 37.8660 - 1e-9)
            self.assertLessEqual(lat, 37.8700 + 1e-9)
            self.assertAlmostEqual(lon, -122.2588, places=6)

    def test_supports_legacy_flat_waypoints(self):
        st = {"name": "Old Format", "waypoints": [
            {"lat": 37.86, "lon": -122.26}, {"lat": 37.87, "lon": -122.26}]}
        lat, lon = sim.random_point_on_street(st)
        self.assertGreaterEqual(lat, 37.86 - 1e-9)
        self.assertLessEqual(lat, 37.87 + 1e-9)

    def test_single_point_segment_returns_that_point(self):
        st = {"name": "Stub", "segments": [[{"lat": 37.865, "lon": -122.255}]]}
        self.assertEqual(sim.random_point_on_street(st), (37.865, -122.255))

    def test_no_geometry_falls_back_to_berkeley(self):
        lat, lon = sim.random_point_on_street({"name": "Nothing", "segments": []})
        self.assertAlmostEqual(lat, 37.87, places=2)
        self.assertAlmostEqual(lon, -122.27, places=2)

    def test_ignores_degenerate_segments_when_a_usable_one_exists(self):
        st = {"name": "Mixed", "segments": [
            [{"lat": 37.90, "lon": -122.30}],                       # too short
            [{"lat": 37.86, "lon": -122.26}, {"lat": 37.861, "lon": -122.26}],
        ]}
        for _ in range(30):
            lat, _ = sim.random_point_on_street(st)
            self.assertLess(lat, 37.87, "picked the unusable single-point segment")


class TestTimestamps(unittest.TestCase):

    def test_activation_is_within_working_hours(self):
        for _ in range(100):
            t = sim.random_activation(DAY)
            self.assertGreaterEqual(t.hour, 8)
            self.assertLessEqual(t.hour, 20)

    def test_activation_is_on_the_requested_day_in_utc(self):
        t = sim.random_activation(DAY)
        self.assertEqual(t.date(), DAY.date())
        self.assertEqual(t.tzinfo, timezone.utc)

    def test_lifespan_is_within_ten_percent_of_the_base(self):
        for obj_type, base in LIFESPANS.items():
            with self.subTest(type=obj_type):
                for _ in range(20):
                    secs = sim.varied_lifespan(obj_type).total_seconds()
                    self.assertGreaterEqual(secs, base * 0.9 - 1)
                    self.assertLessEqual(secs, base * 1.1 + 1)

    def test_lifespans_vary(self):
        seen = {sim.varied_lifespan("single_cone").total_seconds() for _ in range(20)}
        self.assertGreater(len(seen), 1)


class TestBlockingOption(unittest.TestCase):

    def test_one_way_street_cannot_block_one_direction(self):
        """There is only one direction, so that mode would be meaningless."""
        for st in ({"lanes_forward": 2, "lanes_backward": 0},
                   {"lanes_forward": 0, "lanes_backward": 2}):
            with self.subTest(street=st):
                got = {sim._blocking_option(st) for _ in range(80)}
                self.assertNotIn("all_lanes_one_direction", got)
                self.assertTrue(got)

    def test_two_way_street_can_use_every_mode(self):
        from objects import BLOCKING_OPTIONS
        st = {"lanes_forward": 3, "lanes_backward": 3}
        got = {sim._blocking_option(st) for _ in range(200)}
        self.assertEqual(got, set(BLOCKING_OPTIONS))

    def test_defaults_to_two_way_when_lane_counts_are_absent(self):
        got = {sim._blocking_option({}) for _ in range(200)}
        self.assertIn("all_lanes_one_direction", got)

    def test_always_returns_a_known_option(self):
        from objects import BLOCKING_OPTIONS
        for lanes in (1, 2, 4):
            st = {"lanes_forward": lanes, "lanes_backward": lanes}
            for _ in range(30):
                self.assertIn(sim._blocking_option(st), BLOCKING_OPTIONS)


class TestGenerateEvents(unittest.TestCase):

    def setUp(self):
        self.events = sim.generate_events(60, day=DAY, streets=STREETS)

    def test_generates_the_requested_count(self):
        self.assertEqual(len(self.events), 60)

    def test_does_not_read_the_streets_file_when_given_streets(self):
        with patch.object(sim, "load_streets",
                          side_effect=AssertionError("should not load from disk")):
            sim.generate_events(5, day=DAY, streets=STREETS)

    def test_every_event_carries_street_metadata(self):
        names = {s["name"] for s in STREETS}
        for e in self.events:
            self.assertIn(e["street"], names)
            self.assertIn("lanes_forward", e)
            self.assertIn("lanes_backward", e)

    def test_lane_counts_match_the_street_used(self):
        by_name = {s["name"]: s for s in STREETS}
        for e in self.events:
            st = by_name[e["street"]]
            self.assertEqual(e["lanes_forward"], st["lanes_forward"])
            self.assertEqual(e["lanes_backward"], st["lanes_backward"])

    def test_every_event_has_a_recoverable_position(self):
        for e in self.events:
            lat, lon = event_lat_lon(e)
            self.assertNotEqual((lat, lon), (0.0, 0.0), f"{e['type']} has no position")

    def test_position_is_on_the_street_it_claims(self):
        by_name = {s["name"]: s for s in STREETS}
        for e in self.events:
            lat, lon = event_lat_lon(e)
            seg = by_name[e["street"]]["segments"][0]
            near = min(haversine_m(lat, lon, p["lat"], p["lon"]) for p in seg)
            self.assertLess(near, 500, f"{e['type']} placed off {e['street']}")

    def test_active_window_is_ordered(self):
        for e in self.events:
            self.assertLess(datetime.fromisoformat(e["active_at"]),
                            datetime.fromisoformat(e["inactive_at"]))

    def test_all_types_appear_over_a_large_run(self):
        many = sim.generate_events(600, day=DAY, streets=STREETS)
        self.assertEqual({e["type"] for e in many}, set(OBJECT_TYPES))

    def test_ids_are_unique(self):
        self.assertEqual(len({e["id"] for e in self.events}), len(self.events))

    def test_zero_requested_gives_nothing(self):
        self.assertEqual(sim.generate_events(0, day=DAY, streets=STREETS), [])


class TestPurgeExpired(unittest.TestCase):

    def _obj(self, hours):
        t = datetime.now(timezone.utc) + timedelta(hours=hours)
        return {"id": f"h{hours}", "inactive_at": t.isoformat()}

    def test_keeps_future_and_drops_past(self):
        kept = sim.purge_expired([self._obj(-2), self._obj(2)])
        self.assertEqual([o["id"] for o in kept], ["h2"])

    def test_respects_an_explicit_now(self):
        now = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
        objs = [{"id": "old", "inactive_at": "2026-08-04T11:00:00+00:00"},
                {"id": "new", "inactive_at": "2026-08-04T13:00:00+00:00"}]
        self.assertEqual([o["id"] for o in sim.purge_expired(objs, now=now)], ["new"])

    def test_empty_list(self):
        self.assertEqual(sim.purge_expired([]), [])


if __name__ == "__main__":
    unittest.main()
