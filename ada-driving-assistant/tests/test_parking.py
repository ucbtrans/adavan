"""
Tests for parking.py -- the simulated curb-parking answer.

The occupancy model is a formula, so it can be pinned exactly: distance from
campus, street class, and time of day. The question parsing is the other half --
a driver writes "two blocks from Solano and Tacoma", and everything downstream
depends on pulling the streets and the radius out of that.
"""

import unittest
from unittest.mock import patch

import parking as pk

# A small stand-in street set: two crossing streets and a freeway.
_FAKE = {"streets": [
    {"name": "Solano Avenue", "highway": "secondary", "segments": [
        [{"lat": 37.8900, "lon": -122.2800}, {"lat": 37.8900, "lon": -122.2750}]]},
    {"name": "Tacoma Avenue", "highway": "residential", "segments": [
        [{"lat": 37.8900, "lon": -122.2800}, {"lat": 37.8950, "lon": -122.2800}]]},
    {"name": "Eastshore Freeway", "highway": "motorway", "segments": [
        [{"lat": 37.8700, "lon": -122.3100}, {"lat": 37.8750, "lon": -122.3100}]]},
]}


class _FakeStreets(unittest.TestCase):
    def setUp(self):
        p = patch.object(pk, "_load_streets", return_value=_FAKE)
        p.start()
        self.addCleanup(p.stop)


# ── Street classification ───────────────────────────────────────────────────

class TestStreetType(unittest.TestCase):

    def test_motorways(self):
        self.assertEqual(pk._street_type("motorway"), "motorway")
        self.assertEqual(pk._street_type("motorway_link"), "motorway")

    def test_residential(self):
        self.assertEqual(pk._street_type("residential"), "residential")

    def test_everything_else_is_business(self):
        for tag in ("primary", "secondary", "tertiary", "unclassified", "", "nonsense"):
            with self.subTest(tag=tag):
                self.assertEqual(pk._street_type(tag), "business")


# ── Occupancy formula ───────────────────────────────────────────────────────

class TestBlockOccupancy(unittest.TestCase):

    CAMPUS = {"lat": pk._CENTER_LAT, "lon": pk._CENTER_LON}
    FAR = {"lat": pk._CENTER_LAT + 0.05, "lon": pk._CENTER_LON}

    def test_no_parking_on_a_motorway(self):
        self.assertEqual(pk._block_occupancy(self.CAMPUS, self.CAMPUS, "motorway"), 100)
        self.assertEqual(
            pk._block_occupancy(self.FAR, self.FAR, "motorway_link", daytime=False), 100)

    def test_occupancy_falls_with_distance_from_campus(self):
        near = pk._block_occupancy(self.CAMPUS, self.CAMPUS, "residential")
        far = pk._block_occupancy(self.FAR, self.FAR, "residential")
        self.assertGreater(near, far)

    def test_business_streets_are_busier_by_day(self):
        # Measured away from campus: at the centre the base is already 100 and
        # the daytime bonus is clipped, which hides the swing.
        day = pk._block_occupancy(self.FAR, self.FAR, "secondary", daytime=True)
        night = pk._block_occupancy(self.FAR, self.FAR, "secondary", daytime=False)
        self.assertEqual(day - night, 20)

    def test_occupancy_is_clipped_at_a_hundred_near_campus(self):
        self.assertEqual(
            pk._block_occupancy(self.CAMPUS, self.CAMPUS, "secondary", daytime=True), 100)

    def test_residential_streets_are_busier_at_night(self):
        day = pk._block_occupancy(self.FAR, self.FAR, "residential", daytime=True)
        night = pk._block_occupancy(self.FAR, self.FAR, "residential", daytime=False)
        self.assertGreater(night, day)

    def test_result_is_a_percentage(self):
        for lat_off in (0.0, 0.02, 0.2, 1.0):
            for highway in ("residential", "secondary", ""):
                for day in (True, False):
                    with self.subTest(off=lat_off, highway=highway, day=day):
                        p = {"lat": pk._CENTER_LAT + lat_off, "lon": pk._CENTER_LON}
                        got = pk._block_occupancy(p, p, highway, daytime=day)
                        self.assertGreaterEqual(got, 0)
                        self.assertLessEqual(got, 100)
                        self.assertIsInstance(got, int)

    def test_uses_the_midpoint_of_the_block(self):
        """Occupancy is a property of the block, so it is measured at its centre."""
        a = {"lat": pk._CENTER_LAT, "lon": pk._CENTER_LON}
        b = {"lat": pk._CENTER_LAT + 0.04, "lon": pk._CENTER_LON}
        mid = {"lat": pk._CENTER_LAT + 0.02, "lon": pk._CENTER_LON}
        self.assertEqual(pk._block_occupancy(a, b, "residential"),
                         pk._block_occupancy(mid, mid, "residential"))


# ── Question parsing ────────────────────────────────────────────────────────

class TestAbbreviations(unittest.TestCase):

    def test_expands_common_street_types(self):
        self.assertEqual(pk._expand_abbrevs("Solano Ave"), "solano avenue")
        self.assertEqual(pk._expand_abbrevs("Ashby St"), "ashby street")

    def test_expands_a_trailing_abbreviation_with_a_period(self):
        # The period itself is left in place; the text is only ever used for
        # substring matching, so "boulevard." still matches "boulevard".
        self.assertIn("boulevard", pk._expand_abbrevs("Shattuck Blvd."))

    def test_leaves_other_words_alone(self):
        self.assertEqual(pk._expand_abbrevs("parking available"), "parking available")

    def test_does_not_expand_inside_a_word(self):
        """'stop' must not become 'streetop'."""
        self.assertEqual(pk._expand_abbrevs("stop sign"), "stop sign")


class TestStreetStem(unittest.TestCase):

    def test_strips_the_type_word(self):
        self.assertEqual(pk._street_stem("Shattuck Avenue"), "shattuck")
        self.assertEqual(pk._street_stem("Bancroft Way"), "bancroft")

    def test_keeps_multi_word_names(self):
        self.assertEqual(pk._street_stem("Martin Luther King Jr Way"),
                         "martin luther king jr")

    def test_unrecognised_suffix_is_kept(self):
        self.assertEqual(pk._street_stem("Broadway"), "broadway")

    def test_single_word_is_unchanged(self):
        self.assertEqual(pk._street_stem("Avenue"), "avenue")


class TestNameMatching(unittest.TestCase):

    def test_full_name_matches(self):
        self.assertTrue(pk._name_matches_question(
            "Solano Avenue", "parking on solano avenue", "parking on solano avenue"))

    def test_stem_matches_on_a_word_boundary(self):
        self.assertTrue(pk._name_matches_question(
            "Solano Avenue", "parking near solano", "parking near solano"))

    def test_partial_word_does_not_match(self):
        self.assertFalse(pk._name_matches_question(
            "Solano Avenue", "solanoville parking", "solanoville parking"))

    def test_abbreviated_question_matches_via_expansion(self):
        self.assertTrue(pk._name_matches_question(
            "Solano Avenue", "parking on solano ave", "parking on solano avenue"))


class TestParseBlocks(unittest.TestCase):

    def test_defaults_to_two(self):
        self.assertEqual(pk._parse_blocks("where can I park?"), 2)

    def test_reads_a_number_word(self):
        self.assertEqual(pk._parse_blocks("within three blocks of campus"), 3)

    def test_reads_a_digit(self):
        self.assertEqual(pk._parse_blocks("within 4 blocks of campus"), 4)

    def test_singular_block_is_also_understood(self):
        self.assertEqual(pk._parse_blocks("one block away"), 1)


class TestBlueCurb(unittest.TestCase):

    def test_detects_accessible_parking_questions(self):
        found = [q for q in ("any blue curb nearby?",
                             "is there accessible parking?",
                             "wheelchair parking near here",
                             "handicap spot?")
                 if pk._is_blue_curb_question(q)]
        self.assertTrue(found, "no blue-curb phrasing was recognised")

    def test_ordinary_parking_question_is_not_blue_curb(self):
        self.assertFalse(pk._is_blue_curb_question("where can I park the van?"))


# ── Intersection anchoring ──────────────────────────────────────────────────

class TestIntersectionAnchor(_FakeStreets):

    def test_finds_an_x_and_y_intersection(self):
        got = pk.extract_intersection_anchor(
            "where can I park near Solano Avenue and Tacoma Avenue?")
        self.assertIsNotNone(got)
        lat, lon, radius = got
        self.assertAlmostEqual(lat, 37.8900, places=3)
        self.assertAlmostEqual(lon, -122.2800, places=3)
        self.assertGreaterEqual(radius, pk._DEFAULT_RADIUS_M)

    def test_works_with_bare_stems(self):
        got = pk.extract_intersection_anchor("parking near Solano and Tacoma")
        self.assertIsNotNone(got)

    def test_works_with_abbreviations(self):
        got = pk.extract_intersection_anchor("parking at Solano Ave and Tacoma Ave")
        self.assertIsNotNone(got)

    def test_block_count_widens_the_radius(self):
        two = pk.extract_intersection_anchor("within two blocks of Solano and Tacoma")
        five = pk.extract_intersection_anchor("within five blocks of Solano and Tacoma")
        self.assertGreaterEqual(five[2], two[2])
        self.assertEqual(five[2], 5 * pk._BLOCK_M)

    def test_one_street_is_not_an_intersection(self):
        self.assertIsNone(pk.extract_intersection_anchor("parking on Solano Avenue"))

    def test_no_streets_named_returns_none(self):
        self.assertIsNone(pk.extract_intersection_anchor("where can I park?"))

    def test_streets_that_do_not_meet_return_none(self):
        """Two known names that are nowhere near each other are not a junction."""
        self.assertIsNone(pk.extract_intersection_anchor(
            "parking near Solano and Eastshore Freeway"))

    def test_missing_street_data_is_survived(self):
        with patch.object(pk, "_load_streets", side_effect=Exception("no data")):
            self.assertIsNone(pk.extract_intersection_anchor("Solano and Tacoma"))


class TestFindIntersection(_FakeStreets):

    def test_returns_the_shared_corner(self):
        got = pk._find_intersection("Solano Avenue", "Tacoma Avenue")
        self.assertIsNotNone(got)
        self.assertAlmostEqual(got[0], 37.8900, places=4)

    def test_unknown_street_returns_none(self):
        self.assertIsNone(pk._find_intersection("Solano Avenue", "Nowhere Road"))

    def test_far_apart_streets_return_none(self):
        self.assertIsNone(pk._find_intersection("Solano Avenue", "Eastshore Freeway"))


# ── Whole-answer assembly ───────────────────────────────────────────────────

class TestParkingNear(_FakeStreets):

    def test_reports_blocks_and_a_best_street(self):
        got = pk.parking_near(37.8900, -122.2790, radius_m=500)
        self.assertIsNotNone(got)
        self.assertIn("best_chance", got)
        self.assertIn("blocks", got)
        self.assertTrue(got["blocks"])
        for b in got["blocks"]:
            self.assertIn("street", b)
            self.assertIn("occupancy", b)
            self.assertIn("chance", b)

    def test_chance_is_the_complement_of_occupancy(self):
        got = pk.parking_near(37.8900, -122.2790, radius_m=500)
        for b in got["blocks"]:
            self.assertEqual(b["chance"], 100 - b["occupancy"])

    def test_best_chance_is_the_maximum(self):
        got = pk.parking_near(37.8900, -122.2790, radius_m=500)
        self.assertEqual(got["best_chance"], max(b["chance"] for b in got["blocks"]))

    def test_nothing_within_radius_returns_none(self):
        self.assertIsNone(pk.parking_near(0.0, 0.0, radius_m=100))


class TestGetParkingContext(_FakeStreets):

    def test_intersection_question_anchors_there(self):
        got = pk.get_parking_context("park within two blocks of Solano and Tacoma",
                                     fallback_lat=37.87, fallback_lon=-122.27)
        self.assertIsNotNone(got)

    def test_falls_back_to_the_given_position(self):
        got = pk.get_parking_context("where can I park?",
                                     fallback_lat=37.8900, fallback_lon=-122.2790)
        self.assertIsNotNone(got)

    def test_prefers_the_destination_when_given(self):
        got = pk.get_parking_context("where can I park?",
                                     dest_lat=37.8900, dest_lon=-122.2790,
                                     fallback_lat=0.0, fallback_lon=0.0)
        self.assertIsNotNone(got, "should have used the destination, not the fallback")

    def test_nowhere_to_look_returns_none(self):
        self.assertIsNone(pk.get_parking_context("where can I park?",
                                                 fallback_lat=0.0, fallback_lon=0.0))


if __name__ == "__main__":
    unittest.main()
