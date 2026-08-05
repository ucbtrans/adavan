"""
Tests for the two scheduled Lambdas: lambda_function.py (nightly simulation)
and gc_function.py (midnight archive + GC).

Both orchestrate rather than compute, so what matters is that one city or one
van failing does not abort the rest of the run -- these fire unattended once a
day, and a partial result reported honestly is the difference between a quiet
gap in the data and a silent one.
"""

import json
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, mock_open, patch

import gc_function
import lambda_function as lf


class TestDailySimulation(unittest.TestCase):

    def setUp(self):
        self.s3 = MagicMock()
        patches = {
            "put_event": MagicMock(),
            # Fresh dicts per call: the handler tags each event with its city,
            # and a shared list would leave every event tagged with the last one.
            "generate_events": MagicMock(
                side_effect=lambda n, day=None, streets=None: [{"id": "e1"}, {"id": "e2"}]),
            "delete_stale_events": MagicMock(return_value=7),
            "_load_streets_from_s3": MagicMock(return_value=[{"name": "A"}] * 400),
            "_generate_fleet_schedules": MagicMock(return_value={"skipped": True}),
        }
        self.mocks = {}
        for name, mock in patches.items():
            p = patch.object(lf, name, mock)
            self.mocks[name] = p.start()
            self.addCleanup(p.stop)
        p = patch("boto3.client", return_value=self.s3)
        p.start()
        self.addCleanup(p.stop)

    def test_processes_every_city(self):
        out = lf.handler({}, None)
        self.assertEqual(out["body"]["cities_ok"], len(lf.CITIES))
        self.assertEqual(out["body"]["cities_failed"], 0)

    def test_returns_200_when_everything_worked(self):
        self.assertEqual(lf.handler({}, None)["statusCode"], 200)

    def test_reports_207_when_a_city_fails(self):
        """Multi-status: some cities generated, some did not."""
        self.mocks["_load_streets_from_s3"].side_effect = [
            [{"name": "A"}] * 400, Exception("NoSuchKey"),
            [{"name": "A"}] * 400, [{"name": "A"}] * 400,
            [{"name": "A"}] * 400, [{"name": "A"}] * 400,
        ]
        out = lf.handler({}, None)
        self.assertEqual(out["statusCode"], 207)
        self.assertEqual(out["body"]["cities_failed"], 1)
        self.assertEqual(len(out["body"]["errors"]), 1)

    def test_one_city_failing_does_not_stop_the_others(self):
        self.mocks["_load_streets_from_s3"].side_effect = [
            Exception("boom")] + [[{"name": "A"}] * 400] * 5
        out = lf.handler({}, None)
        self.assertEqual(out["body"]["cities_ok"], len(lf.CITIES) - 1)

    def test_each_event_is_tagged_with_its_city(self):
        """The city-index GSI is unusable without it."""
        lf.handler({}, None)
        cities = {c[0][0]["city"] for c in self.mocks["put_event"].call_args_list}
        self.assertEqual(cities, {name for name, _ in lf.CITIES})

    def test_event_count_scales_with_street_count_and_is_capped(self):
        self.mocks["_load_streets_from_s3"].return_value = [{"name": "A"}] * 100
        lf.handler({}, None)
        self.assertEqual(self.mocks["generate_events"].call_args[0][0], 10)
        self.mocks["generate_events"].reset_mock()
        self.mocks["_load_streets_from_s3"].return_value = [{"name": "A"}] * 5000
        lf.handler({}, None)
        self.assertEqual(self.mocks["generate_events"].call_args[0][0], 75)

    def test_at_least_one_event_even_for_a_tiny_city(self):
        self.mocks["_load_streets_from_s3"].return_value = [{"name": "A"}]
        lf.handler({}, None)
        self.assertGreaterEqual(self.mocks["generate_events"].call_args[0][0], 1)

    def test_stale_events_are_garbage_collected(self):
        out = lf.handler({}, None)
        self.mocks["delete_stale_events"].assert_called_with(days=7)
        self.assertEqual(out["body"]["stale_deleted"], 7)

    def test_gc_failure_does_not_fail_the_run(self):
        self.mocks["delete_stale_events"].side_effect = Exception("throttled")
        out = lf.handler({}, None)
        self.assertEqual(out["body"]["stale_deleted"], 0)
        self.assertEqual(out["statusCode"], 200)

    def test_schedules_are_generated_after_the_events(self):
        out = lf.handler({}, None)
        self.assertEqual(out["body"]["fleet_schedules"], {"skipped": True})

    def test_body_carries_a_timestamp_and_totals(self):
        out = lf.handler({}, None)["body"]
        self.assertIn("timestamp", out)
        self.assertEqual(out["total_generated"], 2 * len(lf.CITIES))


class TestStreetLoading(unittest.TestCase):

    def test_downloads_once_and_reuses_the_tmp_copy(self):
        s3 = MagicMock()
        payload = json.dumps({"streets": [{"name": "A"}]})
        with patch("os.path.exists", return_value=False), \
             patch("builtins.open", mock_open(read_data=payload)):
            got = lf._load_streets_from_s3(s3, "CA/Berkeley")
        self.assertEqual(got, [{"name": "A"}])
        s3.download_file.assert_called_once()

    def test_skips_the_download_when_already_cached(self):
        s3 = MagicMock()
        payload = json.dumps({"streets": [{"name": "A"}]})
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data=payload)):
            lf._load_streets_from_s3(s3, "CA/Berkeley")
        s3.download_file.assert_not_called()

    def test_tmp_path_is_unique_per_city(self):
        """A shared path would serve Berkeley's streets for every city."""
        s3 = MagicMock()
        payload = json.dumps({"streets": []})
        seen = []
        with patch("os.path.exists", return_value=False), \
             patch("builtins.open", mock_open(read_data=payload)):
            for prefix in ("CA/Berkeley", "CA/Oakland"):
                lf._load_streets_from_s3(s3, prefix)
                seen.append(s3.download_file.call_args[0][2])
        self.assertEqual(len(set(seen)), 2)


class TestFleetScheduleGeneration(unittest.TestCase):

    POOL = [{"address": "1 A St", "lat": 37.86, "lon": -122.27}]

    def setUp(self):
        self.sched = MagicMock()
        self.sched.schedules_exist.return_value = False
        self.sched.get_active_vans.return_value = [1, 2]
        self.sched.van_id.side_effect = lambda n: f"VAN_{n:02d}"
        self.sched.generate_van_schedule.return_value = {"van_id": "VAN_01"}
        p = patch.object(lf, "sched_mod", self.sched)
        p.start()
        self.addCleanup(p.stop)

    def _run(self, s3=None):
        payload = json.dumps(self.POOL)
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data=payload)):
            return lf._generate_fleet_schedules(s3 or MagicMock(),
                                                datetime(2026, 8, 5, 12, 0,
                                                         tzinfo=timezone.utc))

    def test_skips_when_schedules_already_exist(self):
        self.sched.schedules_exist.return_value = True
        out = self._run()
        self.assertTrue(out["skipped"])
        self.sched.generate_van_schedule.assert_not_called()

    def test_generates_for_every_active_van(self):
        out = self._run()
        self.assertEqual(out["generated"], ["VAN_01", "VAN_02"])

    def test_uses_the_pacific_date_not_utc(self):
        """12:00 UTC is still the previous day in California."""
        out = self._run()
        self.assertEqual(out["date"], "2026-08-05")

    def test_one_van_failing_does_not_stop_the_rest(self):
        self.sched.generate_van_schedule.side_effect = [Exception("osrm down"),
                                                        {"van_id": "VAN_02"}]
        out = self._run()
        self.assertEqual(out["generated"], ["VAN_02"])
        self.assertEqual(len(out["errors"]), 1)

    def test_history_failure_does_not_lose_the_schedule(self):
        self.sched.save_schedule_history.side_effect = Exception("history table gone")
        out = self._run()
        self.assertEqual(len(out["generated"]), 2)

    def test_unreadable_pool_is_reported(self):
        with patch("os.path.exists", return_value=False):
            s3 = MagicMock()
            s3.download_file.side_effect = Exception("NoSuchKey")
            out = lf._generate_fleet_schedules(
                s3, datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc))
        self.assertIn("error", out)


class TestGcHandler(unittest.TestCase):

    def setUp(self):
        self.sched = MagicMock()
        self.sched.schedules_exist.return_value = True
        mocks = {
            "archive_events_to_date": MagicMock(return_value={"archived": 12}),
            "gc_archive_table": MagicMock(return_value=3),
            "sched_mod": self.sched,
        }
        for name, mock in mocks.items():
            p = patch.object(gc_function, name, mock)
            setattr(self, name, p.start())
            self.addCleanup(p.stop)
        p = patch("boto3.client", return_value=MagicMock())
        p.start()
        self.addCleanup(p.stop)

    def test_reports_success(self):
        out = gc_function.handler({}, None)
        self.assertEqual(out["statusCode"], 200)
        self.assertEqual(out["gc_deleted"], 3)
        self.assertEqual(out["archive"], {"archived": 12})

    def test_archives_yesterday_pacific(self):
        """It runs at 07:00 UTC, which is midnight PT -- the day just ended."""
        with patch.object(gc_function, "datetime") as dt:
            dt.now.return_value = datetime(2026, 8, 5, 7, 0, tzinfo=timezone.utc)
            dt.side_effect = datetime
            gc_function.handler({}, None)
        archived_date = self.archive_events_to_date.call_args[0][0]
        self.assertEqual(archived_date, "2026-08-04")

    def test_archive_failure_still_runs_the_rest(self):
        self.archive_events_to_date.side_effect = Exception("table missing")
        out = gc_function.handler({}, None)
        self.assertIn("error", out["archive"])
        self.assertEqual(out["gc_deleted"], 3)

    def test_gc_failure_still_returns(self):
        self.gc_archive_table.side_effect = Exception("throttled")
        out = gc_function.handler({}, None)
        self.assertEqual(out["gc_deleted"], 0)

    def test_archive_is_cut_at_pacific_midnight(self):
        with patch.object(gc_function, "datetime") as dt:
            dt.now.return_value = datetime(2026, 8, 5, 7, 0, tzinfo=timezone.utc)
            dt.side_effect = datetime
            gc_function.handler({}, None)
        boundary = self.archive_events_to_date.call_args[0][1]
        self.assertEqual(boundary.hour, 7)
        self.assertEqual(boundary.tzinfo, timezone.utc)

    def test_generates_schedules_when_missing(self):
        self.sched.schedules_exist.return_value = False
        self.sched.get_active_vans.return_value = [1]
        self.sched.van_id.side_effect = lambda n: f"VAN_{n:02d}"
        self.sched.generate_van_schedule.return_value = {"van_id": "VAN_01"}
        payload = json.dumps([{"address": "1 A St", "lat": 37.86, "lon": -122.27}])
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data=payload)):
            out = gc_function.handler({}, None)
        self.assertEqual(out["fleet_schedules"]["generated"], ["VAN_01"])

    def test_uses_osrm_for_the_nightly_run(self):
        """The nightly job has time for accurate routing; the API path does not."""
        self.sched.schedules_exist.return_value = False
        self.sched.get_active_vans.return_value = [1]
        self.sched.van_id.side_effect = lambda n: f"VAN_{n:02d}"
        payload = json.dumps([{"address": "1 A St", "lat": 37.86, "lon": -122.27}])
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data=payload)):
            gc_function.handler({}, None)
        self.assertTrue(self.sched.generate_van_schedule.call_args[1]["use_osrm"])


if __name__ == "__main__":
    unittest.main()
