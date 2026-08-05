"""
Tests for schedule.py -- fleet van schedules.

The generator is random, so the tests assert the invariants a schedule must hold
rather than exact times: ten rides in sequence, the first pickup at 05:00, time
never running backwards, and every leg starting where the previous one ended.
That last one is what makes the map's van animation coherent.

OSRM and DynamoDB are both stubbed; nothing here touches the network.
"""

import json
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

import schedule as sched_mod

POOL = [{"address": f"{100 + i} Test Street, Berkeley",
         "lat": 37.86 + i * 0.002, "lon": -122.27 - i * 0.002}
        for i in range(30)]


class _StubbedTables(unittest.TestCase):
    def setUp(self):
        self.table = MagicMock()
        self.table.get_item.return_value = {}
        self.hist = MagicMock()
        self.hist.query.return_value = {"Items": []}
        for name, val in (("_table", self.table), ("_history_table", self.hist)):
            p = patch.object(sched_mod, name, return_value=val)
            p.start()
            self.addCleanup(p.stop)


# ── Naming and keys ─────────────────────────────────────────────────────────

class TestKeys(unittest.TestCase):

    def test_van_id_is_zero_padded_and_one_based(self):
        self.assertEqual(sched_mod.van_id(1), "VAN_01")
        self.assertEqual(sched_mod.van_id(10), "VAN_10")

    def test_schedule_key_includes_van_and_date(self):
        key = sched_mod._sched_key("VAN_03", "2026-08-04")
        self.assertEqual(key, "schedule#VAN_03#2026-08-04")

    def test_time_formatting(self):
        self.assertEqual(sched_mod._fmt(5 * 60), "05:00")
        self.assertEqual(sched_mod._fmt(5 * 60 + 7.9), "05:07")
        self.assertEqual(sched_mod._fmt(0), "00:00")

    def test_past_midnight_keeps_counting_up(self):
        """Transit notation: 25:30 rather than wrapping to 01:30 the next day."""
        self.assertEqual(sched_mod._fmt(25 * 60 + 30), "25:30")

    def test_parse_hhmm_round_trips(self):
        self.assertEqual(sched_mod._parse_hhmm("05:00"), 300)
        self.assertEqual(sched_mod._parse_hhmm("13:45"), 825)

    def test_parse_hhmm_rejects_rubbish(self):
        for bad in ("", "abc", "12", None):
            with self.subTest(value=bad):
                self.assertIsNone(sched_mod._parse_hhmm(bad))


# ── Travel time ─────────────────────────────────────────────────────────────

class TestTravelTime(unittest.TestCase):

    def test_fallback_scales_with_distance(self):
        near = sched_mod._fallback_sec(37.86, -122.27, 37.87, -122.27)
        far = sched_mod._fallback_sec(37.86, -122.27, 37.95, -122.27)
        self.assertGreater(far, near)

    def test_fallback_of_zero_distance_is_zero(self):
        self.assertAlmostEqual(sched_mod._fallback_sec(37.86, -122.27, 37.86, -122.27), 0)

    def test_fallback_speed_is_urban(self):
        """~32 km/h: 3.2 km should take about six minutes."""
        secs = sched_mod._fallback_sec(37.8600, -122.2700, 37.8888, -122.2700)
        self.assertAlmostEqual(secs / 60, 6.0, delta=1.5)

    def test_osrm_uses_the_returned_duration(self):
        resp = MagicMock()
        resp.json.return_value = {"code": "Ok", "routes": [{"duration": 512.0}]}
        with patch("schedule.requests.get", return_value=resp):
            self.assertEqual(
                sched_mod.osrm_duration_sec(37.86, -122.27, 37.87, -122.28), 512.0)

    def test_osrm_failure_falls_back_to_straight_line(self):
        with patch("schedule.requests.get", side_effect=Exception("timeout")):
            got = sched_mod.osrm_duration_sec(37.86, -122.27, 37.90, -122.27)
        self.assertAlmostEqual(got, sched_mod._fallback_sec(37.86, -122.27, 37.90, -122.27))

    def test_osrm_error_code_falls_back(self):
        resp = MagicMock()
        resp.json.return_value = {"code": "NoRoute"}
        with patch("schedule.requests.get", return_value=resp):
            got = sched_mod.osrm_duration_sec(37.86, -122.27, 37.90, -122.27)
        self.assertAlmostEqual(got, sched_mod._fallback_sec(37.86, -122.27, 37.90, -122.27))

    def test_geocode_failure_returns_none_pair(self):
        with patch("schedule.requests.get", side_effect=Exception("down")):
            self.assertEqual(sched_mod.geocode("nowhere"), (None, None))

    def test_geocode_parses_a_hit(self):
        resp = MagicMock()
        resp.json.return_value = [{"lat": "37.87", "lon": "-122.27"}]
        with patch("schedule.requests.get", return_value=resp):
            self.assertEqual(sched_mod.geocode("Somewhere"), (37.87, -122.27))


# ── Generation ──────────────────────────────────────────────────────────────

class TestGenerateVanSchedule(unittest.TestCase):

    def setUp(self):
        self.sched = sched_mod.generate_van_schedule("VAN_01", "2026-08-04", POOL)

    def test_has_the_expected_ride_count(self):
        self.assertEqual(len(self.sched["rides"]), sched_mod.RIDES_PER_VAN)

    def test_identifies_the_van_and_date(self):
        self.assertEqual(self.sched["van_id"], "VAN_01")
        self.assertEqual(self.sched["date"], "2026-08-04")

    def test_sequence_numbers_run_one_to_n(self):
        self.assertEqual([r["seq"] for r in self.sched["rides"]],
                         list(range(1, sched_mod.RIDES_PER_VAN + 1)))

    def test_first_pickup_is_at_day_start(self):
        first = self.sched["rides"][0]
        self.assertEqual(first["pickup_min"], float(sched_mod.DAY_START_MIN))
        self.assertEqual(first["start_time"], "05:00")

    def test_first_van_departs_base_early_enough_to_arrive_by_five(self):
        first = self.sched["rides"][0]
        self.assertLessEqual(first["depart_min"], first["pickup_min"])

    def test_time_never_runs_backwards(self):
        prev = -1.0
        for r in self.sched["rides"]:
            self.assertGreaterEqual(r["pickup_min"], prev)
            self.assertGreaterEqual(r["dropoff_min"], r["pickup_min"])
            prev = r["dropoff_min"]

    def test_each_leg_starts_where_the_previous_one_ended(self):
        """The dead-head origin must be the last dropoff, or the map jumps."""
        rides = self.sched["rides"]
        self.assertAlmostEqual(rides[0]["dh_from_lat"], sched_mod.BASE_LAT, places=5)
        self.assertAlmostEqual(rides[0]["dh_from_lon"], sched_mod.BASE_LON, places=5)
        for prev, nxt in zip(rides, rides[1:]):
            self.assertAlmostEqual(nxt["dh_from_lat"], prev["to_lat"], places=5)
            self.assertAlmostEqual(nxt["dh_from_lon"], prev["to_lon"], places=5)

    def test_return_to_base_starts_at_the_last_dropoff(self):
        last = self.sched["rides"][-1]
        self.assertAlmostEqual(self.sched["rtb_from_lat"], last["to_lat"], places=5)
        self.assertAlmostEqual(self.sched["rtb_from_lon"], last["to_lon"], places=5)
        self.assertGreater(self.sched["rtb_sec"], 0)

    def test_every_ride_carries_the_fields_the_ui_reads(self):
        for r in self.sched["rides"]:
            for key in ("seq", "start_time", "from_address", "to_address",
                        "from_lat", "from_lon", "to_lat", "to_lon",
                        "dh_from_lat", "dh_from_lon", "depart_min",
                        "pickup_min", "dropoff_min", "dh_sec", "ride_sec"):
                self.assertIn(key, r)

    def test_addresses_come_from_the_pool(self):
        addresses = {p["address"] for p in POOL}
        for r in self.sched["rides"]:
            self.assertIn(r["from_address"], addresses)
            self.assertIn(r["to_address"], addresses)

    def test_pickup_and_dropoff_differ(self):
        for r in self.sched["rides"]:
            self.assertNotEqual((r["from_lat"], r["from_lon"]),
                                (r["to_lat"], r["to_lon"]))

    def test_small_pool_does_not_crash(self):
        """random.sample would raise if it asked for more than the pool holds."""
        s = sched_mod.generate_van_schedule("VAN_01", "2026-08-04", POOL[:3])
        self.assertEqual(len(s["rides"]), sched_mod.RIDES_PER_VAN)

    def test_does_not_call_osrm_unless_asked(self):
        with patch.object(sched_mod, "osrm_duration_sec",
                          side_effect=AssertionError("should not call OSRM")):
            sched_mod.generate_van_schedule("VAN_01", "2026-08-04", POOL)

    def test_use_osrm_routes_every_leg(self):
        with patch.object(sched_mod, "osrm_duration_sec", return_value=300.0) as osrm, \
             patch.object(sched_mod.time, "sleep"):
            s = sched_mod.generate_van_schedule("VAN_01", "2026-08-04", POOL, use_osrm=True)
        # two legs per ride (dead-head + ride) plus the return to base
        self.assertEqual(osrm.call_count, sched_mod.RIDES_PER_VAN * 2 + 1)
        self.assertEqual(s["rides"][0]["ride_sec"], 300)


class TestRecomputeTiming(_StubbedTables):

    def _edited(self):
        s = sched_mod.generate_van_schedule("VAN_01", "2026-08-04", POOL)
        return s

    def test_user_start_time_is_kept_when_achievable(self):
        s = self._edited()
        s["rides"][1]["start_time"] = "23:00"          # far later than feasible
        with patch.object(sched_mod, "osrm_duration_sec", return_value=60.0), \
             patch.object(sched_mod.time, "sleep"):
            out = sched_mod.recompute_schedule_timing(s)
        self.assertEqual(out["rides"][1]["start_time"], "23:00")

    def test_impossible_start_time_is_advanced_to_the_earliest(self):
        s = self._edited()
        s["rides"][1]["start_time"] = "00:01"          # before ride 1 even finishes
        with patch.object(sched_mod, "osrm_duration_sec", return_value=600.0), \
             patch.object(sched_mod.time, "sleep"):
            out = sched_mod.recompute_schedule_timing(s)
        self.assertGreater(out["rides"][1]["pickup_min"], out["rides"][0]["dropoff_min"])

    def test_first_ride_stays_pinned_to_day_start(self):
        s = self._edited()
        s["rides"][0]["start_time"] = "09:30"
        with patch.object(sched_mod, "osrm_duration_sec", return_value=60.0), \
             patch.object(sched_mod.time, "sleep"):
            out = sched_mod.recompute_schedule_timing(s)
        self.assertEqual(out["rides"][0]["start_time"], "05:00")

    def test_chain_stays_continuous_after_recompute(self):
        s = self._edited()
        with patch.object(sched_mod, "osrm_duration_sec", return_value=120.0), \
             patch.object(sched_mod.time, "sleep"):
            out = sched_mod.recompute_schedule_timing(s)
        rides = out["rides"]
        for prev, nxt in zip(rides, rides[1:]):
            self.assertAlmostEqual(nxt["dh_from_lat"], prev["to_lat"], places=5)


# ── Completed rides ─────────────────────────────────────────────────────────

class TestCompletedRides(unittest.TestCase):

    SCHED = {"rides": [{"seq": 1, "dropoff_min": 320},
                       {"seq": 2, "dropoff_min": 400},
                       {"seq": 3, "dropoff_min": 500}]}

    def test_counts_only_rides_already_dropped_off(self):
        self.assertEqual(sched_mod.compute_completed_rides(self.SCHED, 410), [1, 2])

    def test_boundary_is_inclusive(self):
        self.assertEqual(sched_mod.compute_completed_rides(self.SCHED, 320), [1])

    def test_before_the_day_starts_nothing_is_complete(self):
        self.assertEqual(sched_mod.compute_completed_rides(self.SCHED, 0), [])

    def test_after_the_day_everything_is_complete(self):
        self.assertEqual(sched_mod.compute_completed_rides(self.SCHED, 9999), [1, 2, 3])

    def test_missing_dropoff_is_treated_as_not_done(self):
        self.assertEqual(sched_mod.compute_completed_rides({"rides": [{"seq": 1}]}, 500), [])

    def test_empty_schedule(self):
        self.assertEqual(sched_mod.compute_completed_rides({}, 500), [])


# ── Decimal conversion ──────────────────────────────────────────────────────

class TestDynamoConversion(unittest.TestCase):

    def test_floats_become_decimal(self):
        out = sched_mod._dynamo_encode({"lat": 37.8660001234})
        self.assertIsInstance(out["lat"], Decimal)

    def test_ints_stay_ints(self):
        self.assertIsInstance(sched_mod._dynamo_encode({"n": 5})["n"], int)

    def test_encode_recurses(self):
        out = sched_mod._dynamo_encode({"rides": [{"lat": 1.5}]})
        self.assertIsInstance(out["rides"][0]["lat"], Decimal)

    def test_decode_restores_numbers(self):
        out = sched_mod._dynamo_decode({"a": Decimal("1.5"), "b": Decimal("3")})
        self.assertEqual(out["a"], 1.5)
        self.assertIsInstance(out["b"], int)

    def test_round_trip_preserves_a_schedule(self):
        s = sched_mod.generate_van_schedule("VAN_01", "2026-08-04", POOL)
        back = sched_mod._dynamo_decode(sched_mod._dynamo_encode(s))
        self.assertEqual(back["van_id"], s["van_id"])
        self.assertEqual(len(back["rides"]), len(s["rides"]))
        self.assertAlmostEqual(back["rides"][0]["from_lat"], s["rides"][0]["from_lat"],
                               places=5)

    def test_strings_and_none_pass_through(self):
        out = sched_mod._dynamo_encode({"s": "text", "n": None})
        self.assertEqual(out, {"s": "text", "n": None})


# ── Persistence ─────────────────────────────────────────────────────────────

class TestPersistence(_StubbedTables):

    def _sched(self):
        return sched_mod.generate_van_schedule("VAN_02", "2026-08-04", POOL)

    def test_save_uses_the_composite_key(self):
        sched_mod.save_schedule(self._sched())
        item = self.table.put_item.call_args[1]["Item"]
        self.assertEqual(item["config_key"], "schedule#VAN_02#2026-08-04")
        self.assertEqual(item["van_id"], "VAN_02")

    def test_save_encodes_floats_for_dynamo(self):
        sched_mod.save_schedule(self._sched())
        item = self.table.put_item.call_args[1]["Item"]
        self.assertIsInstance(item["rides"][0]["from_lat"], Decimal)

    def test_save_stamps_generated_at(self):
        sched_mod.save_schedule(self._sched())
        self.assertIn("generated_at", self.table.put_item.call_args[1]["Item"])

    def test_get_missing_schedule_is_none(self):
        self.table.get_item.return_value = {}
        self.assertIsNone(sched_mod.get_schedule("VAN_01", "2026-08-04"))

    def test_get_decodes_decimals(self):
        self.table.get_item.return_value = {"Item": {"van_id": "VAN_01",
                                                     "rtb_sec": Decimal("120")}}
        self.assertEqual(sched_mod.get_schedule("VAN_01", "2026-08-04")["rtb_sec"], 120)

    def test_delete_targets_the_same_key(self):
        sched_mod.delete_schedule("VAN_02", "2026-08-04")
        self.assertEqual(self.table.delete_item.call_args[1]["Key"]["config_key"],
                         "schedule#VAN_02#2026-08-04")

    def test_schedules_exist_checks_the_first_active_van(self):
        self.table.get_item.return_value = {"Item": {"van_id": "VAN_01"}}
        self.assertTrue(sched_mod.schedules_exist("2026-08-04"))

    def test_schedules_exist_is_false_when_absent(self):
        self.table.get_item.return_value = {}
        self.assertFalse(sched_mod.schedules_exist("2026-08-04"))

    def test_get_all_skips_vans_with_no_schedule(self):
        with patch.object(sched_mod, "get_active_vans", return_value=[1, 2, 3]), \
             patch.object(sched_mod, "get_schedule",
                          side_effect=lambda v, d: {"van_id": v} if v != "VAN_02" else None):
            got = sched_mod.get_all_schedules("2026-08-04")
        self.assertEqual([g["van_id"] for g in got], ["VAN_01", "VAN_03"])


# ── Active vans ─────────────────────────────────────────────────────────────

class TestActiveVans(_StubbedTables):

    def test_defaults_to_the_full_fleet(self):
        self.table.get_item.return_value = {}
        self.assertEqual(sched_mod.get_active_vans(),
                         list(range(1, sched_mod.NUM_VANS + 1)))

    def test_reads_a_json_encoded_list(self):
        self.table.get_item.return_value = {"Item": {"van_nums": json.dumps([3, 1, 2])}}
        self.assertEqual(sched_mod.get_active_vans(), [1, 2, 3])

    def test_reads_a_native_list_of_decimals(self):
        self.table.get_item.return_value = {
            "Item": {"van_nums": [Decimal("2"), Decimal("1")]}}
        self.assertEqual(sched_mod.get_active_vans(), [1, 2])

    def test_set_sorts_and_dedupes_types(self):
        sched_mod.set_active_vans([3, "1", 2])
        item = self.table.put_item.call_args[1]["Item"]
        self.assertEqual(json.loads(item["van_nums"]), [1, 2, 3])
        self.assertEqual(item["config_key"], sched_mod.ACTIVE_VANS_KEY)


# ── History ─────────────────────────────────────────────────────────────────

class TestScheduleHistory(_StubbedTables):

    def test_first_entry_starts_at_sequence_zero(self):
        self.hist.query.return_value = {"Items": []}
        sched_mod.save_schedule_history("VAN_01", "2026-08-04", "generated",
                                        {"rides": []}, [], 1)
        item = self.hist.put_item.call_args[1]["Item"]
        self.assertEqual(item["date_seq"], "2026-08-04#0000")
        self.assertEqual(item["seq"], 0)

    def test_next_entry_increments_the_sequence(self):
        self.hist.query.return_value = {"Items": [{"date_seq": "2026-08-04#0007"}]}
        sched_mod.save_schedule_history("VAN_01", "2026-08-04", "edited",
                                        {"rides": []}, [1], 2)
        item = self.hist.put_item.call_args[1]["Item"]
        self.assertEqual(item["date_seq"], "2026-08-04#0008")
        self.assertEqual(item["seq"], 8)

    def test_unparseable_previous_sequence_restarts_at_zero(self):
        self.hist.query.return_value = {"Items": [{"date_seq": "garbage"}]}
        sched_mod.save_schedule_history("VAN_01", "2026-08-04", "edited",
                                        {"rides": []}, [], 1)
        self.assertEqual(self.hist.put_item.call_args[1]["Item"]["seq"], 0)

    def test_entry_carries_a_ttl_after_the_date(self):
        sched_mod.save_schedule_history("VAN_01", "2026-08-04", "generated",
                                        {"rides": []}, [], 1)
        item = self.hist.put_item.call_args[1]["Item"]
        self.assertGreater(item["ttl"], 0)

    def test_event_type_and_payload_are_stored(self):
        sched = {"rides": [{"seq": 1, "from_lat": 37.5}]}
        sched_mod.save_schedule_history("VAN_01", "2026-08-04", "deleted",
                                        sched, [1, 2], 3)
        item = self.hist.put_item.call_args[1]["Item"]
        self.assertEqual(item["event_type"], "deleted")
        self.assertEqual(item["completed_rides"], [1, 2])
        self.assertEqual(item["active_from_ride_seq"], 3)
        self.assertIsInstance(item["schedule"]["rides"][0]["from_lat"], Decimal)

    def test_history_for_date_groups_by_van_and_sorts(self):
        self.hist.query.return_value = {"Items": [
            {"van_id": "VAN_01", "date_seq": "2026-08-04#0001", "seq": Decimal("1")},
            {"van_id": "VAN_01", "date_seq": "2026-08-04#0000", "seq": Decimal("0")},
            {"van_id": "VAN_02", "date_seq": "2026-08-04#0000", "seq": Decimal("0")},
        ]}
        got = sched_mod.get_schedule_history_for_date("2026-08-04")
        self.assertEqual(set(got), {"VAN_01", "VAN_02"})
        self.assertEqual([e["seq"] for e in got["VAN_01"]], [0, 1])

    def test_history_for_date_follows_pagination(self):
        self.hist.query.side_effect = [
            {"Items": [{"van_id": "VAN_01", "date_seq": "2026-08-04#0000"}],
             "LastEvaluatedKey": {"van_id": "VAN_01"}},
            {"Items": [{"van_id": "VAN_01", "date_seq": "2026-08-04#0001"}]},
        ]
        got = sched_mod.get_schedule_history_for_date("2026-08-04")
        self.assertEqual(len(got["VAN_01"]), 2)
        self.assertEqual(self.hist.query.call_count, 2)


if __name__ == "__main__":
    unittest.main()
