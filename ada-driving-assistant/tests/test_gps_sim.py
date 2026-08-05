"""
Tests for the local real-van GPS simulator (gps_sim.py) and its uploader
(position_to_s3.py).

The contract that matters is the seam between them, because it is the same seam
the physical van uses: gps_sim writes the CSV that video_capture.py would write
from $GNGGA sentences, and position_to_s3 reads it the way position_to_aws.py
does. So the coordinate round-trip through the raw NMEA format, and the shape of
the published van_position.json, are what these tests pin down.
"""

import csv
import json
import os
import random
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import gps_sim
import position_to_s3 as pub
from location import haversine_m


# ── NMEA coordinate format ──────────────────────────────────────────────────

class TestNmeaRoundTrip(unittest.TestCase):
    """gps_sim writes DDMM.MMMMM; position_to_s3 must recover the same point."""

    POINTS = [
        (37.8660, -122.2588),    # Telegraph & Dwight, Berkeley
        (37.8897, -122.3024),    # the Albany base
        (37.8058, -122.2699),    # south Oakland edge
        (0.5, 0.5), (-33.8688, 151.2093), (-45.0, -170.0),   # other hemispheres
    ]

    def test_round_trip_within_two_centimetres(self):
        for lat, lon in self.POINTS:
            with self.subTest(lat=lat, lon=lon):
                lat_raw, lat_dir = gps_sim.to_nmea_degrees(lat, True)
                lon_raw, lon_dir = gps_sim.to_nmea_degrees(lon, False)
                back_lat = pub.to_decimal_degrees(lat_raw, lat_dir)
                back_lon = pub.to_decimal_degrees(lon_raw, lon_dir)
                # 5 decimal minutes is ~1.85 cm of resolution.
                self.assertLess(haversine_m(lat, lon, back_lat, back_lon), 0.05)

    def test_latitude_is_ddmm_and_longitude_is_dddmm(self):
        lat_raw, lat_dir = gps_sim.to_nmea_degrees(37.8660, True)
        lon_raw, lon_dir = gps_sim.to_nmea_degrees(-122.2588, False)
        self.assertEqual((len(lat_raw), lat_dir), (10, "N"))
        self.assertEqual((len(lon_raw), lon_dir), (11, "W"))
        self.assertTrue(lat_raw.startswith("3751."), lat_raw)
        self.assertTrue(lon_raw.startswith("12215."), lon_raw)

    def test_hemispheres(self):
        self.assertEqual(gps_sim.to_nmea_degrees(-37.0, True)[1], "S")
        self.assertEqual(gps_sim.to_nmea_degrees(151.0, False)[1], "E")

    def test_southern_and_western_hemispheres_go_negative(self):
        self.assertLess(pub.to_decimal_degrees("3751.21678", "S"), 0)
        self.assertLess(pub.to_decimal_degrees("12216.74993", "W"), 0)
        self.assertGreater(pub.to_decimal_degrees("3751.21678", "N"), 0)
        self.assertGreater(pub.to_decimal_degrees("12216.74993", "E"), 0)

    def test_matches_position_to_aws_arithmetic(self):
        """Same formula as the van's own converter, for a known value."""
        self.assertAlmostEqual(pub.to_decimal_degrees("3751.01095", "N"),
                               37 + 51.01095 / 60.0, places=9)


# ── Reading the fix log ─────────────────────────────────────────────────────

class TestReadLastFix(unittest.TestCase):

    def _write(self, rows):
        fd, path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        self.addCleanup(os.unlink, path)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=gps_sim.CSV_FIELDS)
            w.writeheader()
            w.writerows(rows)
        return path

    def _row(self, lat="3751.21678", lon="12216.74993", t="2026-08-04T21:00:00.000Z"):
        return {"Camera_time": t, "GPS_time": "210000.00",
                "Latitude": lat, "Latitude_direction": "N",
                "Longitude": lon, "Longitude_direction": "W"}

    def test_returns_the_last_valid_row(self):
        path = self._write([self._row(t="first"), self._row(t="last")])
        self.assertEqual(pub.read_last_fix(path)["time"], "last")

    def test_null_rows_are_skipped(self):
        """video_capture.py logs Null when no sentence arrived in the window."""
        path = self._write([
            self._row(t="good"),
            {"Camera_time": "later", "GPS_time": "Null", "Latitude": "Null",
             "Latitude_direction": "Null", "Longitude": "Null",
             "Longitude_direction": "Null"},
        ])
        self.assertEqual(pub.read_last_fix(path)["time"], "good")

    def test_uppercase_null_also_skipped(self):
        path = self._write([self._row(t="good"), self._row(lat="NULL", lon="NULL")])
        self.assertEqual(pub.read_last_fix(path)["time"], "good")

    def test_unparseable_row_is_skipped_not_fatal(self):
        path = self._write([self._row(t="good"), self._row(lat="", lon="garbage")])
        self.assertEqual(pub.read_last_fix(path)["time"], "good")

    def test_out_of_range_coordinates_rejected(self):
        path = self._write([self._row(t="good"), self._row(lat="99999.99999")])
        self.assertEqual(pub.read_last_fix(path)["time"], "good")

    def test_all_null_returns_none(self):
        path = self._write([self._row(lat="Null", lon="Null")])
        self.assertIsNone(pub.read_last_fix(path))

    def test_empty_log_returns_none(self):
        self.assertIsNone(pub.read_last_fix(self._write([])))

    def test_missing_file_raises_filenotfound(self):
        with self.assertRaises(FileNotFoundError):
            pub.read_last_fix("no_such_file_here.csv")

    def test_decodes_to_expected_coordinates(self):
        # 3751.21678 = 37 deg + 51.21678 min / 60;  12216.74993 W likewise.
        fix = pub.read_last_fix(self._write([self._row()]))
        self.assertAlmostEqual(fix["lat"], 37 + 51.21678 / 60, places=6)
        self.assertAlmostEqual(fix["lon"], -(122 + 16.74993 / 60), places=6)


# ── The published payload ───────────────────────────────────────────────────

class TestPayloadContract(unittest.TestCase):
    """
    real_van_ingest._fetch() and /api/fleet/real-van both read exactly these
    four keys, so the names are load-bearing.
    """

    FIX = {"time": "2026-08-04T21:00:01.910Z", "lat": 37.8660, "lon": -122.2588}

    def test_has_exactly_the_consumer_keys(self):
        with patch.object(pub, "street_at", return_value="Telegraph Avenue"):
            payload = pub.build_payload(dict(self.FIX))
        self.assertEqual(set(payload), {"Time", "Latitude", "Longitude", "Street"})

    def test_values_pass_through(self):
        with patch.object(pub, "street_at", return_value="Telegraph Avenue"):
            payload = pub.build_payload(dict(self.FIX))
        self.assertEqual(payload["Time"], self.FIX["time"])
        self.assertAlmostEqual(payload["Latitude"], 37.8660)
        self.assertAlmostEqual(payload["Longitude"], -122.2588)
        self.assertEqual(payload["Street"], "Telegraph Avenue")

    def test_coordinates_are_json_numbers_not_strings(self):
        """The Lambda does float(d["Latitude"]) but the map reads it directly."""
        with patch.object(pub, "street_at", return_value=""):
            payload = pub.build_payload(dict(self.FIX))
        self.assertIsInstance(payload["Latitude"], float)
        self.assertIsInstance(payload["Longitude"], float)

    def test_missing_time_is_filled_in(self):
        """A change in Time is what the ingester treats as a new report."""
        with patch.object(pub, "street_at", return_value=""):
            payload = pub.build_payload({"time": "", "lat": 37.87, "lon": -122.27})
        self.assertTrue(payload["Time"].endswith("Z"))


class TestPublishOnce(unittest.TestCase):

    def setUp(self):
        fd, self.csv_path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        self.addCleanup(os.unlink, self.csv_path)
        with open(self.csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=gps_sim.CSV_FIELDS)
            w.writeheader()
            w.writerow({"Camera_time": "2026-08-04T21:00:00.000Z",
                        "GPS_time": "210000.00", "Latitude": "3751.21678",
                        "Latitude_direction": "N", "Longitude": "12216.74993",
                        "Longitude_direction": "W"})
        fd, self.json_path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self.addCleanup(os.unlink, self.json_path)
        p = patch.object(pub, "street_at", return_value="Russell Street")
        p.start()
        self.addCleanup(p.stop)

    def test_dry_run_writes_json_but_does_not_upload(self):
        with patch("boto3.client") as client:
            got = pub.publish_once(self.csv_path, dry_run=True, quiet=True,
                                   local_json=self.json_path)
        client.assert_not_called()
        self.assertEqual(json.load(open(self.json_path))["Street"], "Russell Street")
        self.assertEqual(got["Street"], "Russell Street")

    def test_upload_targets_the_requested_bucket_and_key(self):
        with patch("boto3.client") as client:
            pub.publish_once(self.csv_path, bucket="b", key="k.json",
                             region="us-west-1", quiet=True,
                             local_json=self.json_path)
        client.assert_called_once_with("s3", region_name="us-west-1")
        client.return_value.upload_file.assert_called_once_with(
            self.json_path, "b", "k.json")

    def test_missing_log_returns_none_without_uploading(self):
        with patch("boto3.client") as client:
            self.assertIsNone(pub.publish_once("nope.csv", quiet=True,
                                               local_json=self.json_path))
        client.assert_not_called()

    def test_upload_failure_is_reported_not_raised(self):
        with patch("boto3.client", side_effect=Exception("no credentials")):
            got = pub.publish_once(self.csv_path, quiet=True,
                                   local_json=self.json_path)
        self.assertIsNotNone(got, "payload should still be returned")


# ── Driving the street network ──────────────────────────────────────────────

def _grid_streets():
    """Two crossing streets plus a one-way stub, sharing exact junction vertices."""
    return [
        {"name": "Main Street", "lanes_backward": 1, "segments": [
            [{"lat": 37.800, "lon": -122.300}, {"lat": 37.801, "lon": -122.300}],
            [{"lat": 37.801, "lon": -122.300}, {"lat": 37.802, "lon": -122.300}],
        ]},
        {"name": "Cross Avenue", "lanes_backward": 1, "segments": [
            [{"lat": 37.801, "lon": -122.300}, {"lat": 37.801, "lon": -122.299}],
        ]},
        {"name": "Oneway Lane", "lanes_backward": 0, "segments": [
            [{"lat": 37.801, "lon": -122.300}, {"lat": 37.801, "lon": -122.301}],
        ]},
    ]


class TestStreetNetwork(unittest.TestCase):

    def setUp(self):
        self.net = gps_sim.StreetNetwork(_grid_streets())

    def test_builds_one_way_entry_per_segment(self):
        self.assertEqual(len(self.net), 4)

    def test_junction_is_detected(self):
        self.assertGreaterEqual(self.net.junction_count(), 1)

    def test_unnamed_streets_are_excluded(self):
        streets = _grid_streets() + [{"name": "Unnamed_tertiary",
                                      "lanes_backward": 1, "segments": [
                                          [{"lat": 37.9, "lon": -122.4},
                                           {"lat": 37.901, "lon": -122.4}]]}]
        self.assertEqual(len(gps_sim.StreetNetwork(streets)), 4)

    def test_oneway_is_never_driven_backwards(self):
        """lanes_backward == 0 means the way is only enterable at its first vertex."""
        oneway_idx = [i for i, w in enumerate(self.net.ways) if w["oneway"]]
        self.assertTrue(oneway_idx)
        for node in self.net.at_node:
            for way_idx, forward in self.net._candidates(node, came_from=-1):
                if way_idx in oneway_idx:
                    self.assertTrue(forward, "entered a one-way street backwards")

    def test_drive_yields_a_connected_path(self):
        pts = []
        for i, p in enumerate(self.net.drive(random.Random(3))):
            pts.append(p)
            if i >= 200:
                break
        # No teleporting: this fixture's blocks are ~110 m, so any hop between
        # consecutive route vertices must stay well under that.
        for a, b in zip(pts, pts[1:]):
            self.assertLess(haversine_m(a[0], a[1], b[0], b[1]), 200)

    def test_drive_reports_a_street_name_for_every_point(self):
        gen = self.net.drive(random.Random(1))
        for _ in range(50):
            _, _, name = next(gen)
            self.assertIn(name, {"Main Street", "Cross Avenue", "Oneway Lane"})


class TestCursor(unittest.TestCase):

    def test_advance_covers_the_requested_distance(self):
        net = gps_sim.StreetNetwork(_grid_streets())
        c = gps_sim.Cursor(net.drive(random.Random(0)))
        start = c.cur
        c.advance(50.0)
        self.assertAlmostEqual(c.metres, 50.0, places=6)
        self.assertGreater(haversine_m(start[0], start[1], c.cur[0], c.cur[1]), 1)

    def test_repeated_small_steps_accumulate(self):
        net = gps_sim.StreetNetwork(_grid_streets())
        c = gps_sim.Cursor(net.drive(random.Random(0)))
        for _ in range(20):
            c.advance(9.83)
        self.assertAlmostEqual(c.metres, 20 * 9.83, places=5)

    def test_seed_makes_the_route_repeatable(self):
        net = gps_sim.StreetNetwork(_grid_streets())
        runs = []
        for _ in range(2):
            c = gps_sim.Cursor(net.drive(random.Random(99)))
            runs.append([c.advance(9.83)[:2] for _ in range(40)])
        self.assertEqual(runs[0], runs[1])


# ── The fix log ─────────────────────────────────────────────────────────────

class TestFixLog(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "fixes.csv")

    def tearDown(self):
        for f in os.listdir(self.dir):
            os.unlink(os.path.join(self.dir, f))
        os.rmdir(self.dir)

    def _row(self, n):
        return {"Camera_time": f"t{n}", "GPS_time": "210000.00",
                "Latitude": "3751.21678", "Latitude_direction": "N",
                "Longitude": "12216.74993", "Longitude_direction": "W"}

    def test_writes_header_and_rows(self):
        log = gps_sim.FixLog(self.path, max_rows=0)
        log.write(self._row(1))
        log.close()
        with open(self.path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Camera_time"], "t1")

    def test_columns_match_video_capture(self):
        log = gps_sim.FixLog(self.path, max_rows=0)
        log.write(self._row(1))
        log.close()
        with open(self.path, encoding="utf-8") as f:
            header = f.readline().strip()
        self.assertEqual(header, "Camera_time,GPS_time,Latitude,"
                                 "Latitude_direction,Longitude,Longitude_direction")

    def test_trims_to_half_when_full(self):
        log = gps_sim.FixLog(self.path, max_rows=10)
        for i in range(10):
            log.write(self._row(i))
        log.close()
        with open(self.path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[-1]["Camera_time"], "t9", "must keep the newest rows")

    def test_trimmed_log_is_still_readable_by_the_uploader(self):
        log = gps_sim.FixLog(self.path, max_rows=10)
        for i in range(12):
            log.write(self._row(i))
        log.close()
        fix = pub.read_last_fix(self.path)
        self.assertEqual(fix["time"], "t11")

    def test_reopening_appends_rather_than_truncating(self):
        log = gps_sim.FixLog(self.path, max_rows=0)
        log.write(self._row(1))
        log.close()
        log2 = gps_sim.FixLog(self.path, max_rows=0)
        log2.write(self._row(2))
        log2.close()
        with open(self.path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual([r["Camera_time"] for r in rows], ["t1", "t2"])


class TestDriveMakesProgress(unittest.TestCase):
    """
    Regression: the first routing pass excluded only the block it arrived on, so
    at a junction it could pick a way heading straight back. On the real network
    the van circled -- 2.1 km driven for 273 m of progress, and consecutive
    once-a-minute samples only 33 m apart, which on the map reads as a van
    jittering in place rather than driving.

    The fleet map animates between samples a minute apart, so per-minute *net*
    displacement is the number that matters, not distance driven.
    """

    STEP_M = 22 * gps_sim.MPH_TO_MPS   # one second at the default 22 mph

    def _minute_hops(self, seed, minutes=10):
        from location import _load_streets
        net = gps_sim.StreetNetwork(_load_streets()["streets"])
        c = gps_sim.Cursor(net.drive(random.Random(seed)))
        pts = [c.advance(self.STEP_M) for _ in range(minutes * 60)]
        marks = pts[::60]
        return [haversine_m(a[0], a[1], b[0], b[1])
                for a, b in zip(marks, marks[1:])], c.metres

    def test_distance_driven_matches_the_requested_speed(self):
        _, metres = self._minute_hops(11, minutes=5)
        self.assertAlmostEqual(metres, 5 * 60 * self.STEP_M, delta=1.0)

    def test_median_minute_advances_most_of_the_way_driven(self):
        hops = []
        for seed in (11, 42, 7, 99, 123):
            hops.extend(self._minute_hops(seed)[0])
        hops.sort()
        median = hops[len(hops) // 2]
        # 60 s at 22 mph covers ~590 m of road; a median well above half of that
        # means the route is advancing, not looping back on itself.
        self.assertGreater(median, 300,
                           f"median minute advanced only {median:.0f} m -- the "
                           f"van is circling again")

    def test_few_minutes_barely_move(self):
        """Dead-end turnarounds are legitimate; a majority of them is not."""
        hops = []
        for seed in (11, 42, 7, 99, 123):
            hops.extend(self._minute_hops(seed)[0])
        stalled = [h for h in hops if h < 100]
        self.assertLess(len(stalled) / len(hops), 0.20,
                        f"{len(stalled)}/{len(hops)} minutes advanced under 100 m")


class TestRouteStaysOnTheRoad(unittest.TestCase):
    """
    Regression: at the end of a one-way with nowhere to go, the walk restarted
    somewhere else entirely. Cursor interpolates between whatever points it is
    handed, so it drew a straight line from here to there -- one jump measured
    5979 m, and the ~600 fixes along it were all off-road. That is what
    published an empty Street, since street_at() rightly refuses to name a
    street 123 m away. A dead end now turns the van around instead.
    """

    SEEDS = (42, 11, 7, 2026)

    @classmethod
    def setUpClass(cls):
        from location import _load_streets
        cls.data = _load_streets()["streets"]
        cls.net = gps_sim.StreetNetwork(cls.data)
        # Longest gap between neighbouring vertices of one real polyline: any
        # step longer than this cannot be ordinary geometry.
        cls.max_span = max(
            haversine_m(a["lat"], a["lon"], b["lat"], b["lon"])
            for st in cls.data for seg in st.get("segments", [])
            for a, b in zip(seg, seg[1:])
        )

    def test_route_has_no_discontinuities(self):
        for seed in self.SEEDS:
            with self.subTest(seed=seed):
                gen = self.net.drive(random.Random(seed))
                pts = [next(gen) for _ in range(4000)]
                worst = max(haversine_m(a[0], a[1], b[0], b[1])
                            for a, b in zip(pts, pts[1:]))
                self.assertLessEqual(
                    worst, self.max_span,
                    f"seed {seed} jumped {worst:.0f} m between route points, "
                    f"further than the longest real block ({self.max_span:.0f} m)")

    def test_dead_end_turns_around_rather_than_restarting(self):
        """A cul-de-sac must be left the way it was entered, not teleported out."""
        streets = [{"name": "Dead End Lane", "lanes_backward": 0, "highway": "residential",
                    "segments": [[{"lat": 37.800, "lon": -122.300},
                                  {"lat": 37.801, "lon": -122.300}]]},
                   {"name": "Far Away Road", "lanes_backward": 1, "highway": "residential",
                    "segments": [[{"lat": 37.900, "lon": -122.400},
                                  {"lat": 37.901, "lon": -122.400}]]}]
        net = gps_sim.StreetNetwork(streets)
        gen = net.drive(random.Random(0))
        pts = [next(gen) for _ in range(60)]
        for a, b in zip(pts, pts[1:]):
            self.assertLess(haversine_m(a[0], a[1], b[0], b[1]), 500,
                            "walk teleported between disconnected blocks")
        self.assertEqual({p[2] for p in pts}, {"Far Away Road"},
                         "the one-way with no legal exit should never be driven")

    def test_every_drivable_state_has_somewhere_to_go(self):
        """
        The invariant that makes getting stuck impossible. Way #540, 'CA 13', a
        one-way motorway_link, dead-ended one way and was reachable only by
        driving another one-way backwards the other; the van drove in and spent
        70 km inside a 147 m box.
        """
        stranded = [s for s in self.net.drivable
                    if not any(t in self.net.drivable
                               for t in self.net._successors(s))]
        self.assertEqual(stranded, [], f"{len(stranded)} states have no legal exit")

    def test_a_one_way_sink_is_not_drivable(self):
        """A one-way that dead-ends is a trap and must be left out."""
        streets = [{"name": "Main Street", "lanes_backward": 1, "highway": "residential",
                    "segments": [[{"lat": 37.800, "lon": -122.300},
                                  {"lat": 37.801, "lon": -122.300}]]},
                   {"name": "Sink Ramp", "lanes_backward": 0, "highway": "motorway_link",
                    "segments": [[{"lat": 37.801, "lon": -122.300},
                                  {"lat": 37.802, "lon": -122.299}]]}]
        net = gps_sim.StreetNetwork(streets)
        sink = next(i for i, w in enumerate(net.ways) if w["name"] == "Sink Ramp")
        self.assertFalse(net._usable(sink, True),
                         "a one-way into a dead end must not be drivable")
        pts = [next(net.drive(random.Random(0))) for _ in range(30)]
        self.assertNotIn("Sink Ramp", {p[2] for p in pts})

    def test_van_keeps_covering_new_ground(self):
        """Half an hour of driving should roam, not orbit one block."""
        for seed in self.SEEDS:
            with self.subTest(seed=seed):
                c = gps_sim.Cursor(self.net.drive(random.Random(seed)))
                pts = [c.advance(22 * gps_sim.MPH_TO_MPS) for _ in range(1800)]
                lats = [p[0] for p in pts]
                lons = [p[1] for p in pts]
                span = haversine_m(min(lats), min(lons), max(lats), max(lons))
                self.assertGreater(span, 1000,
                                   f"seed {seed} stayed inside a {span:.0f} m box "
                                   f"over 30 minutes of driving")


class TestWaitOrStop(unittest.TestCase):
    """
    Regression: Ctrl-C took seconds and several presses. Each loop slept its
    full period before noticing, and on Windows a Python signal handler only
    runs between bytecodes, so even a single Event.wait sat out its whole
    timeout -- 4.4 s measured on a loop with a 5 s period. Waiting in slices
    caps that at STOP_TICK.
    """

    def setUp(self):
        self.ev = threading.Event()

    def test_returns_immediately_when_already_stopped(self):
        self.ev.set()
        t0 = time.monotonic()
        self.assertTrue(gps_sim.wait_or_stop(30.0, self.ev))
        self.assertLess(time.monotonic() - t0, 0.5)

    def test_waits_out_the_period_when_not_stopped(self):
        t0 = time.monotonic()
        self.assertFalse(gps_sim.wait_or_stop(0.5, self.ev))
        self.assertGreaterEqual(time.monotonic() - t0, 0.45)

    def test_wakes_promptly_when_stopped_mid_wait(self):
        threading.Timer(0.2, self.ev.set).start()
        t0 = time.monotonic()
        self.assertTrue(gps_sim.wait_or_stop(10.0, self.ev))
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 1.0, f"took {elapsed:.2f}s to notice the stop")

    def test_slices_are_short_enough_to_stay_responsive(self):
        self.assertLessEqual(gps_sim.STOP_TICK, 0.5)

    def test_zero_and_negative_waits_do_not_hang(self):
        self.assertFalse(gps_sim.wait_or_stop(0, self.ev))
        self.assertFalse(gps_sim.wait_or_stop(-1, self.ev))


class TestPublishLoopStops(unittest.TestCase):

    def test_stop_event_ends_the_loop_without_waiting_the_interval(self):
        ev = threading.Event()
        calls = []
        with patch.object(pub, "publish_once", side_effect=lambda *a, **k: calls.append(1)):
            t = threading.Thread(target=pub.publish_loop,
                                 kwargs=dict(interval=30.0, stop=ev, quiet=True))
            t.start()
            time.sleep(0.3)
            ev.set()
            t.join(timeout=3.0)
        self.assertFalse(t.is_alive(), "publish_loop ignored the stop event")
        self.assertEqual(len(calls), 1, "should have published once before stopping")

    def test_returns_at_once_if_stopped_before_starting(self):
        ev = threading.Event()
        ev.set()
        with patch.object(pub, "publish_once") as once:
            pub.publish_loop(interval=30.0, stop=ev, quiet=True)
        once.assert_not_called()


class TestStreetAt(unittest.TestCase):
    """Uses the real city_streets.json, like the uploader does in practice."""

    def test_names_the_street_the_van_is_on(self):
        # On Telegraph Avenue just south of Dwight Way.
        self.assertEqual(pub.street_at(37.8645, -122.2585), "Telegraph Avenue")

    def test_returns_empty_when_nothing_is_near(self):
        self.assertEqual(pub.street_at(0.0, 0.0), "")

    def test_far_offshore_point_is_not_snapped(self):
        self.assertEqual(pub.street_at(37.60, -122.90), "")


if __name__ == "__main__":
    unittest.main()
