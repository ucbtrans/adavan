"""
Tests for objects.py -- the event-shape factories the simulator builds from.

Every consumer downstream reads these shapes: events.event_lat_lon() derives a
position from coordinates / polygon / left+right pairs, and the map draws from
the same fields. So the tests pin the shape of each factory and the geometry of
the boxes, not just that a dict comes back.
"""

import math
import unittest
from datetime import datetime, timedelta, timezone

import objects as ob
from events import event_lat_lon
from location import haversine_m

NOW = datetime(2026, 8, 4, 12, 0, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(hours=2)
LAT, LON = 37.8660, -122.2588


class TestBase(unittest.TestCase):

    def test_base_fields(self):
        o = ob.make_base("single_cone", NOW, LATER)
        self.assertEqual(o["type"], "single_cone")
        self.assertEqual(o["source"], "simulated")
        self.assertEqual(o["active_at"], NOW.isoformat())
        self.assertEqual(o["inactive_at"], LATER.isoformat())

    def test_ids_are_unique(self):
        ids = {ob.make_base("single_cone", NOW, LATER)["id"] for _ in range(50)}
        self.assertEqual(len(ids), 50)

    def test_every_type_has_a_lifespan(self):
        self.assertEqual(set(ob.OBJECT_TYPES), set(ob.LIFESPANS))
        for t, secs in ob.LIFESPANS.items():
            with self.subTest(type=t):
                self.assertGreater(secs, 0)


class TestGeometry(unittest.TestCase):

    def test_rect_polygon_has_four_corners(self):
        poly = ob.make_rect_polygon(LAT, LON)
        self.assertEqual(len(poly), 4)
        for p in poly:
            self.assertEqual(set(p), {"lat", "lon"})

    def test_rect_polygon_dimensions_match_request(self):
        half_w, half_l = 6.0, 30.0
        poly = ob.make_rect_polygon(LAT, LON, half_w_m=half_w, half_l_m=half_l)
        lats = [p["lat"] for p in poly]
        lons = [p["lon"] for p in poly]
        # Height spans 2 * half_l, width spans 2 * half_w, within a metre.
        height = haversine_m(min(lats), LON, max(lats), LON)
        width = haversine_m(LAT, min(lons), LAT, max(lons))
        self.assertAlmostEqual(height, 2 * half_l, delta=1.0)
        self.assertAlmostEqual(width, 2 * half_w, delta=1.0)

    def test_rect_polygon_is_centred_on_the_point(self):
        poly = ob.make_rect_polygon(LAT, LON)
        self.assertAlmostEqual(sum(p["lat"] for p in poly) / 4, LAT, places=9)
        self.assertAlmostEqual(sum(p["lon"] for p in poly) / 4, LON, places=9)

    def test_longitude_scaling_accounts_for_latitude(self):
        """A degree of longitude shrinks towards the poles; 4 m must stay 4 m."""
        for lat in (0.0, 37.87, 60.0):
            with self.subTest(lat=lat):
                poly = ob.make_rect_polygon(lat, LON, half_w_m=4.0, half_l_m=4.0)
                lons = [p["lon"] for p in poly]
                width = haversine_m(lat, min(lons), lat, max(lons))
                self.assertAlmostEqual(width, 8.0, delta=0.5)

    def test_cos_lat_never_zero(self):
        """Guards a divide-by-zero at the pole."""
        self.assertGreater(ob._cos_lat(90.0), 0)


class TestFactories(unittest.TestCase):
    """Each factory must produce something event_lat_lon() can place on a map."""

    def _built(self):
        cars = [{"type": "sedan", "color": "red"}]
        return {
            "single_cone": ob.make_single_cone(LAT, LON, NOW, LATER),
            "cone_group": ob.make_cone_group(LAT, LON, 5, NOW, LATER),
            "construction_zone": ob.make_construction_zone(LAT, LON, "one_lane", NOW, LATER),
            "car_accident": ob.make_car_accident(LAT, LON, cars, True, NOW, LATER),
            "double_parked_car": ob.make_double_parked_car(LAT, LON, "van", "white", NOW, LATER),
            "broken_car": ob.make_broken_car(LAT, LON, "SUV", "black", NOW, LATER),
            "road_barrier": ob.make_road_barrier(LAT, LON, NOW, LATER),
            "dropped_object": ob.make_dropped_object(LAT, LON, NOW, LATER),
            "protest": ob.make_protest(LAT, LON, NOW, LATER),
            "police_blocking": ob.make_police_blocking(LAT, LON, "both_directions", NOW, LATER),
        }

    def test_every_object_type_has_a_factory(self):
        self.assertEqual(set(self._built()), set(ob.OBJECT_TYPES))

    def test_type_field_matches_the_factory(self):
        for name, o in self._built().items():
            with self.subTest(type=name):
                self.assertEqual(o["type"], name)

    def test_position_is_recoverable_for_every_type(self):
        """event_lat_lon() is how the map places these; none may fall back to 0,0."""
        for name, o in self._built().items():
            with self.subTest(type=name):
                lat, lon = event_lat_lon(o)
                self.assertNotEqual((lat, lon), (0.0, 0.0),
                                    f"{name} has no derivable position")
                self.assertLess(haversine_m(lat, lon, LAT, LON), 60,
                                f"{name} resolved far from where it was placed")

    def test_cone_group_widens_with_cone_count(self):
        narrow = ob.make_cone_group(LAT, LON, 2, NOW, LATER)
        wide = ob.make_cone_group(LAT, LON, 10, NOW, LATER)
        span = lambda o: max(p["lon"] for p in o["polygon"]) - min(p["lon"] for p in o["polygon"])
        self.assertGreater(span(wide), span(narrow))
        self.assertEqual(wide["num_cones"], 10)

    def test_road_barrier_ends_straddle_the_centre(self):
        o = ob.make_road_barrier(LAT, LON, NOW, LATER)
        left, right = o["left_coordinates"], o["right_coordinates"]
        self.assertLess(left["lon"], LON)
        self.assertGreater(right["lon"], LON)
        self.assertAlmostEqual(haversine_m(left["lat"], left["lon"],
                                           right["lat"], right["lon"]), 6.0, delta=0.5)

    def test_type_specific_fields_are_carried_through(self):
        cars = [{"type": "sedan", "color": "red"}]
        self.assertEqual(ob.make_construction_zone(LAT, LON, "entire_street",
                                                   NOW, LATER)["blocking"],
                         "entire_street")
        acc = ob.make_car_accident(LAT, LON, cars, True, NOW, LATER)
        self.assertEqual(acc["cars"], cars)
        self.assertTrue(acc["police_present"])
        dp = ob.make_double_parked_car(LAT, LON, "van", "white", NOW, LATER)
        self.assertEqual((dp["car_type"], dp["car_color"]), ("van", "white"))
        br = ob.make_broken_car(LAT, LON, "SUV", "black", NOW, LATER)
        self.assertTrue(br["emergency_lights"])
        self.assertEqual(br["blocking_direction"], "one_direction")
        pb = ob.make_police_blocking(LAT, LON, "both_directions", NOW, LATER)
        self.assertEqual(pb["directions_blocked"], "both_directions")

    def test_option_vocabularies_are_what_the_factories_accept(self):
        for blocking in ob.BLOCKING_OPTIONS:
            with self.subTest(blocking=blocking):
                o = ob.make_construction_zone(LAT, LON, blocking, NOW, LATER)
                self.assertEqual(o["blocking"], blocking)
        for d in ob.POLICE_DIR_OPTIONS:
            with self.subTest(directions=d):
                o = ob.make_police_blocking(LAT, LON, d, NOW, LATER)
                self.assertEqual(o["directions_blocked"], d)


if __name__ == "__main__":
    unittest.main()
