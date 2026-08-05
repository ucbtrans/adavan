"""
Tests for real_van_ingest.py -- the Lambda that turns the GPS feed into a track.

This is the consumer of everything gps_sim.py publishes, so the tests cover the
decisions it makes: whether a report is new, whether it counts as movement, when
to drop to STATIONED and when to wake, and that pause really is a no-op. The
table and the HTTP feed are both stubbed.

Its module-level _table is built at import time from boto3.resource, which
conftest.py has already replaced with a mock, so nothing reaches AWS.
"""

import itertools
import json
import time
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

import real_van_ingest as rvi


def _feed(lat=37.8660, lon=-122.2588, street="Telegraph Avenue",
          t="2026-08-04T21:00:00Z"):
    return {"Latitude": lat, "Longitude": lon, "Street": street, "Time": t}


class _Harness(unittest.TestCase):
    def setUp(self):
        self.table = MagicMock()
        self.table.get_item.return_value = {}
        p = patch.object(rvi, "_table", self.table)
        p.start()
        self.addCleanup(p.stop)
        p2 = patch.object(rvi.time, "sleep")      # never actually pace
        p2.start()
        self.addCleanup(p2.stop)

    def _clock(self, start=0.0, step=30.0):
        """
        A time.time() that advances on every call.

        The ACTIVE branch loops until time.time() passes a deadline it computed
        from time.time(), so a frozen clock spins forever. Stepping 30 s against
        the 50 s budget gives one pass through the poll loop.
        """
        ticks = itertools.count(start, step)
        p = patch.object(rvi.time, "time", side_effect=lambda: next(ticks))
        p.start()
        self.addCleanup(p.stop)

    def _state(self, **kw):
        self.table.get_item.return_value = {"Item": {"van_id": rvi.VAN_ID,
                                                     "ts": "STATE", **kw}}

    def _fetch_returns(self, value):
        p = patch.object(rvi, "_fetch", return_value=value)
        mock = p.start()
        self.addCleanup(p.stop)
        return mock

    def _recorded(self):
        """Position items written, ignoring STATE writes."""
        return [c[1]["Item"] for c in self.table.put_item.call_args_list
                if c[1]["Item"].get("ts") != "STATE"]

    def _state_writes(self):
        return [c[1]["Item"] for c in self.table.put_item.call_args_list
                if c[1]["Item"].get("ts") == "STATE"]


# ── Geometry and fetch ──────────────────────────────────────────────────────

class TestHelpers(unittest.TestCase):

    def test_haversine_zero(self):
        self.assertAlmostEqual(rvi._haversine_m(37.87, -122.27, 37.87, -122.27), 0)

    def test_haversine_known_distance(self):
        # One minute of latitude is about 1852 m.
        d = rvi._haversine_m(37.0, -122.0, 37.0 + 1 / 60, -122.0)
        self.assertAlmostEqual(d, 1852, delta=5)

    def test_fetch_parses_the_feed(self):
        resp = MagicMock()
        resp.json.return_value = _feed()
        with patch.object(rvi.requests, "get", return_value=resp):
            got = rvi._fetch()
        self.assertEqual(got, {"lat": 37.8660, "lon": -122.2588,
                               "street": "Telegraph Avenue",
                               "source_time": "2026-08-04T21:00:00Z"})

    def test_fetch_returns_none_without_coordinates(self):
        resp = MagicMock()
        resp.json.return_value = {"Time": "t"}
        with patch.object(rvi.requests, "get", return_value=resp):
            self.assertIsNone(rvi._fetch())

    def test_fetch_coerces_string_coordinates(self):
        resp = MagicMock()
        resp.json.return_value = _feed(lat="37.87", lon="-122.27")
        with patch.object(rvi.requests, "get", return_value=resp):
            self.assertIsInstance(rvi._fetch()["lat"], float)

    def test_fetch_tolerates_missing_street(self):
        resp = MagicMock()
        resp.json.return_value = {"Latitude": 37.87, "Longitude": -122.27, "Time": "t"}
        with patch.object(rvi.requests, "get", return_value=resp):
            self.assertEqual(rvi._fetch()["street"], "")


class TestRecord(_Harness):

    def test_writes_decimals_and_a_ttl(self):
        rvi._record({"lat": 37.87, "lon": -122.27, "street": "X", "source_time": "t"},
                    moving=True, dist_m=123.45)
        item = self._recorded()[0]
        self.assertIsInstance(item["lat"], Decimal)
        self.assertIsInstance(item["lon"], Decimal)
        self.assertIsInstance(item["dist_m"], Decimal)
        self.assertGreater(item["ttl"], time.time())

    def test_ttl_matches_the_retention_window(self):
        rvi._record({"lat": 37.87, "lon": -122.27, "street": "", "source_time": ""},
                    moving=False, dist_m=0.0)
        item = self._recorded()[0]
        self.assertAlmostEqual(item["ttl"] - time.time(), rvi.RETENTION_SECONDS, delta=60)

    def test_timestamp_is_utc_with_a_z_suffix(self):
        """The history API and the browser both parse this."""
        rvi._record({"lat": 37.87, "lon": -122.27, "street": "", "source_time": ""},
                    moving=False, dist_m=0.0)
        self.assertTrue(self._recorded()[0]["ts"].endswith("Z"))

    def test_route_distance_is_only_written_when_given(self):
        rvi._record({"lat": 37.87, "lon": -122.27, "street": "", "source_time": ""},
                    moving=True, dist_m=1.0)
        self.assertNotIn("route_m", self._recorded()[0])

    def test_simulated_records_carry_route_distance(self):
        rvi._record({"lat": 37.87, "lon": -122.27, "street": "", "source_time": ""},
                    moving=True, dist_m=1.0, simulated=True, route_m=650.0)
        item = self._recorded()[0]
        self.assertIn("route_m", item)
        self.assertTrue(item["simulated"])


# ── Pause ───────────────────────────────────────────────────────────────────

class TestPaused(_Harness):

    def test_paused_records_nothing(self):
        self._state(tracking="paused")
        out = rvi.handler({}, None)
        self.assertEqual(out, {"tracking": "paused", "recorded": 0})
        self.assertEqual(self._recorded(), [])

    def test_paused_does_not_even_fetch(self):
        """The whole point of pause is a near-free invocation."""
        self._state(tracking="paused")
        with patch.object(rvi, "_fetch", side_effect=AssertionError("should not fetch")):
            rvi.handler({}, None)


# ── Active mode ─────────────────────────────────────────────────────────────

class TestActiveMode(_Harness):

    def test_first_report_is_recorded_as_movement(self):
        self._state()
        self._clock()
        self._fetch_returns({"lat": 37.87, "lon": -122.27, "street": "A",
                             "source_time": "t1"})
        rvi.handler({}, None)
        rec = self._recorded()
        self.assertEqual(len(rec), 1)
        self.assertTrue(rec[0]["moving"])

    def test_repeated_identical_report_is_not_recorded_twice(self):
        """The feed's Time only advances when the van moves."""
        self._state(last_source_time="t1", last_lat=Decimal("37.87"),
                    last_lon=Decimal("-122.27"))
        self._clock()
        self._fetch_returns({"lat": 37.87, "lon": -122.27, "street": "A",
                             "source_time": "t1"})
        rvi.handler({}, None)
        self.assertEqual(self._recorded(), [])

    def test_report_without_a_timestamp_is_ignored(self):
        self._state()
        self._clock()
        self._fetch_returns({"lat": 37.87, "lon": -122.27, "street": "A",
                             "source_time": ""})
        rvi.handler({}, None)
        self.assertEqual(self._recorded(), [])

    def test_small_hop_is_recorded_but_not_as_movement(self):
        """Under the threshold is GPS jitter, not driving."""
        self._state(last_source_time="t0", last_lat=Decimal("37.8700"),
                    last_lon=Decimal("-122.2700"), last_move_epoch=Decimal("0"))
        self._clock()
        self._fetch_returns({"lat": 37.87005, "lon": -122.27, "street": "A",
                             "source_time": "t1"})
        rvi.handler({}, None)
        rec = self._recorded()
        self.assertEqual(len(rec), 1)
        self.assertFalse(rec[0]["moving"])
        self.assertLess(float(rec[0]["dist_m"]), rvi.MOVE_THRESHOLD_M)

    def test_large_hop_counts_as_movement(self):
        self._state(last_source_time="t0", last_lat=Decimal("37.8700"),
                    last_lon=Decimal("-122.2700"), last_move_epoch=Decimal("0"))
        self._clock()
        self._fetch_returns({"lat": 37.8760, "lon": -122.2700, "street": "A",
                             "source_time": "t1"})
        rvi.handler({}, None)
        rec = self._recorded()
        self.assertTrue(rec[0]["moving"])
        self.assertGreater(float(rec[0]["dist_m"]), rvi.MOVE_THRESHOLD_M)

    def test_fetch_failure_does_not_crash_the_invocation(self):
        self._state()
        self._clock()
        with patch.object(rvi, "_fetch", side_effect=Exception("upstream 502")):
            out = rvi.handler({}, None)
        self.assertEqual(out["recorded"], 0)

    def test_idle_too_long_drops_to_stationed(self):
        self._state(last_source_time="t1", last_move_epoch=Decimal("0"),
                    last_lat=Decimal("37.87"), last_lon=Decimal("-122.27"))
        self._clock(start=rvi.STATIONED_AFTER + 1000)
        self._fetch_returns(None)
        out = rvi.handler({}, None)
        self.assertEqual(out["mode"], "stationed")
        self.assertEqual(self._state_writes()[-1]["mode"], "stationed")

    def test_state_is_merged_not_overwritten(self):
        """A user pausing mid-invocation must not have the flag clobbered."""
        self._clock()
        self._fetch_returns(None)
        # The handler starts on a running van, then _persist re-reads STATE at
        # the end -- by which point the user has pressed pause.
        reads = itertools.chain(
            [{"Item": {"van_id": rvi.VAN_ID, "ts": "STATE", "tracking": "running"}}],
            itertools.repeat(
                {"Item": {"van_id": rvi.VAN_ID, "ts": "STATE", "tracking": "paused"}}),
        )
        self.table.get_item.side_effect = lambda **kw: next(reads)
        rvi.handler({}, None)
        self.assertEqual(self._state_writes()[-1]["tracking"], "paused")

    def test_position_is_carried_into_state_for_the_next_run(self):
        self._state()
        self._clock()
        self._fetch_returns({"lat": 37.8760, "lon": -122.2700,
                             "street": "Telegraph Avenue", "source_time": "t1"})
        rvi.handler({}, None)
        st = self._state_writes()[-1]
        self.assertAlmostEqual(float(st["last_lat"]), 37.8760)
        self.assertEqual(st["last_street"], "Telegraph Avenue")
        self.assertEqual(st["last_source_time"], "t1")


# ── Stationed mode ──────────────────────────────────────────────────────────

class TestStationedMode(_Harness):

    def test_heartbeat_at_the_last_known_spot(self):
        self._state(mode="stationed", last_source_time="t1",
                    last_lat=Decimal("37.87"), last_lon=Decimal("-122.27"),
                    last_street="Telegraph Avenue", last_move_epoch=Decimal("0"))
        self._clock()
        self._fetch_returns(None)
        rvi.handler({}, None)
        rec = self._recorded()
        self.assertEqual(len(rec), 1)
        self.assertFalse(rec[0]["moving"])
        self.assertEqual(rec[0]["street"], "Telegraph Avenue")
        self.assertAlmostEqual(float(rec[0]["lat"]), 37.87)

    def test_movement_wakes_it_back_to_active(self):
        self._state(mode="stationed", last_source_time="t1",
                    last_lat=Decimal("37.8700"), last_lon=Decimal("-122.2700"),
                    last_move_epoch=Decimal("0"))
        self._clock()
        self._fetch_returns({"lat": 37.8760, "lon": -122.2700, "street": "A",
                             "source_time": "t2"})
        out = rvi.handler({}, None)
        self.assertEqual(out["mode"], "active")
        self.assertEqual(len(self._recorded()), 1)

    def test_only_one_check_per_invocation(self):
        """Stationed is the cheap path: a single fetch, not a polling loop."""
        self._state(mode="stationed", last_source_time="t1",
                    last_lat=Decimal("37.87"), last_lon=Decimal("-122.27"),
                    last_move_epoch=Decimal("0"))
        self._clock()
        fetch = self._fetch_returns(None)
        rvi.handler({}, None)
        self.assertEqual(fetch.call_count, 1)

    def test_no_previous_position_writes_no_heartbeat(self):
        self._state(mode="stationed", last_move_epoch=Decimal("0"))
        self._clock()
        self._fetch_returns(None)
        rvi.handler({}, None)
        self.assertEqual(self._recorded(), [])

    def test_fetch_failure_still_heartbeats(self):
        self._state(mode="stationed", last_source_time="t1",
                    last_lat=Decimal("37.87"), last_lon=Decimal("-122.27"),
                    last_street="A", last_move_epoch=Decimal("0"))
        self._clock()
        with patch.object(rvi, "_fetch", side_effect=Exception("upstream down")):
            rvi.handler({}, None)
        self.assertEqual(len(self._recorded()), 1)


# ── Built-in waypoint simulation (legacy path, still shipped) ───────────────

class TestSimulationMode(_Harness):

    WPS = [[-122.270, 37.870, 0.0],
           [-122.271, 37.871, 650.0],
           [-122.272, 37.872, 1300.0]]

    def test_emits_waypoints_in_order(self):
        self._state(sim="on", sim_wp=json.dumps(self.WPS), sim_index=Decimal("0"))
        self._clock(step=5.0)
        rvi.handler({}, None)
        rec = self._recorded()
        self.assertGreaterEqual(len(rec), 2)
        self.assertAlmostEqual(float(rec[0]["lat"]), 37.870)
        self.assertAlmostEqual(float(rec[1]["lat"]), 37.871)
        self.assertTrue(all(r["simulated"] for r in rec))

    def test_index_is_persisted_so_the_next_run_continues(self):
        self._state(sim="on", sim_wp=json.dumps(self.WPS), sim_index=Decimal("2"))
        self._clock(step=5.0)
        rvi.handler({}, None)
        self.assertIn("sim_index", self._state_writes()[-1])

    def test_index_wraps_at_the_end_of_the_loop(self):
        self._state(sim="on", sim_wp=json.dumps(self.WPS), sim_index=Decimal("2"))
        self._clock(step=5.0)
        rvi.handler({}, None)
        idx = int(self._state_writes()[-1]["sim_index"])
        self.assertLess(idx, len(self.WPS))

    def test_no_waypoints_is_reported_not_crashed(self):
        self._state(sim="on", sim_wp="[]")
        out = rvi.handler({}, None)
        self.assertEqual(out["recorded"], 0)
        self.assertIn("note", out)

    def test_unparseable_waypoints_are_survived(self):
        self._state(sim="on", sim_wp="{not json")
        out = rvi.handler({}, None)
        self.assertEqual(out["recorded"], 0)

    def test_paused_beats_simulation(self):
        self._state(tracking="paused", sim="on", sim_wp=json.dumps(self.WPS))
        out = rvi.handler({}, None)
        self.assertEqual(out["tracking"], "paused")
        self.assertEqual(self._recorded(), [])

    def test_simulation_does_not_consult_the_live_feed(self):
        self._state(sim="on", sim_wp=json.dumps(self.WPS), sim_index=Decimal("0"))
        self._clock(step=5.0)
        with patch.object(rvi, "_fetch", side_effect=AssertionError("should not fetch")):
            rvi.handler({}, None)


if __name__ == "__main__":
    unittest.main()
