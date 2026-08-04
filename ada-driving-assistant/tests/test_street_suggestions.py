"""
Tests for find_street_suggestions() in location.py.

Regression: several street-type words are also everyday English — way, road,
drive, run, path, row, place, court. Matching on the suffix alone turned
ordinary questions into phantom spelling corrections, and app.py hands those to
the model, which then answers with "did you mean …?" instead of the road
conditions. Observed on the deployed dev stack:

    "Are road conditions clear ahead?" -> {'are road': 'Arden Road'}
    "Is the way ahead clear?"          -> {'the way': 'Stephens Way'}

The two guards under test: candidates whose leading words are function words
are dropped, and a fuzzy match must preserve the distinguishing word rather
than riding on the shared street-type word ("way street" -> "Bay Street").
"""

import unittest
from unittest.mock import patch

import location as loc

# Small fixed street list — keeps the logic tests independent of the 1.3 MB
# city_streets.json and of whatever streets a real city happens to contain.
_FAKE_STREETS = {"streets": [{"name": n} for n in [
    "Telegraph Avenue", "Shattuck Avenue", "Ashby Avenue", "University Avenue",
    "Adeline Street", "Bay Street", "Ordway Street", "Arden Road",
    "Stephens Way", "Grizzly Peak Boulevard", "Hazel Road", "Circle Drive",
    "Terrace Avenue", "Deer Run Drive", "May Road", "Over Street",
]]}


class _FakeStreetsTestCase(unittest.TestCase):
    def setUp(self):
        p = patch.object(loc, "_load_streets", return_value=_FAKE_STREETS)
        p.start()
        self.addCleanup(p.stop)


class TestProseIsNotAStreet(_FakeStreetsTestCase):
    """Ordinary questions must produce no suggestions at all."""

    PROSE = [
        "Are road conditions clear ahead?",
        "How are road conditions?",
        "Is the way ahead clear?",
        "What is my drive time?",
        "Is there a faster way?",
        "What is the best way to go?",
        "Should I take that road?",
        "Is the path clear?",
        "Anything in the way?",
        "Is it a one way street?",
        "Which way should I turn?",
        "Is the road clear ahead?",
        "How long is the drive?",
        "Are there hazards on the road?",
        "Give me the fastest way home.",
        "Is this street closed?",
    ]

    def test_no_suggestions_for_prose(self):
        for q in self.PROSE:
            with self.subTest(question=q):
                self.assertEqual(loc.find_street_suggestions(q), {},
                                 f"prose was read as a misspelled street: {q!r}")

    def test_are_road_does_not_become_arden_road(self):
        """The exact question that broke on the deployed stack."""
        self.assertEqual(loc.find_street_suggestions("Are road conditions clear ahead?"), {})

    def test_the_way_does_not_become_stephens_way(self):
        self.assertEqual(loc.find_street_suggestions("Is the way ahead clear?"), {})

    def test_one_way_street_does_not_become_ordway_street(self):
        self.assertEqual(loc.find_street_suggestions("Is it a one way street?"), {})


class TestRealTyposStillCorrected(_FakeStreetsTestCase):
    """Precision must not cost us the actual feature."""

    def _suggest(self, q):
        return loc.find_street_suggestions(q)

    def test_misspelled_street_is_corrected(self):
        self.assertEqual(self._suggest("Any obstacles on Telegrah Avenue?"),
                         {"telegrah avenue": "Telegraph Avenue"})

    def test_missing_letter_corrected(self):
        self.assertEqual(self._suggest("Is Shattck Avenue clear?"),
                         {"shattck avenue": "Shattuck Avenue"})

    def test_two_word_street_corrected(self):
        s = self._suggest("Anything on Grizly Peak Blvd?")
        self.assertEqual(list(s.values()), ["Grizzly Peak Boulevard"])

    def test_transposed_letters_corrected(self):
        self.assertEqual(self._suggest("Is Adelien Street blocked?"),
                         {"adelien street": "Adeline Street"})

    def test_leading_preposition_dropped_but_street_kept(self):
        """'on telegrah ave' is rejected for the 'on'; 'telegrah ave' survives."""
        s = self._suggest("Any obstacles on Telegrah Ave?")
        self.assertEqual(s, {"telegrah ave": "Telegraph Avenue"})

    def test_exactly_spelled_street_needs_no_suggestion(self):
        self.assertEqual(self._suggest("Any obstacles on Telegraph Avenue?"), {})


class TestDistinguishingWordMustMatch(_FakeStreetsTestCase):
    """A shared street-type word alone is not enough to propose a correction."""

    def test_way_street_does_not_become_bay_street(self):
        # "way street" vs "bay street" is 0.95 on the whole phrase but only
        # 0.67 on the distinguishing word, which is what should decide it.
        self.assertNotIn("Bay Street",
                         loc.find_street_suggestions("Is it a one way street?").values())

    def test_generic_two_word_street_not_proposed_from_prose(self):
        for q in ["Is the circle drive busy?", "Is there a deer run drive nearby?"]:
            with self.subTest(question=q):
                self.assertEqual(loc.find_street_suggestions(q), {})


class TestAgainstRealStreetData(unittest.TestCase):
    """
    Runs against the tracked city_streets.json (Berkeley, ~840 streets). The
    false positives that shipped were only visible with a real street list: it
    gives the fuzzy matcher far more chances to find a bad neighbour than any
    fixture does, which is how "way street" -> "Bay Street" turned up.
    """

    PROSE = TestProseIsNotAStreet.PROSE + [
        "Any construction over there?",
        "Can you check the route ahead?",
        "How is traffic on my drive home?",
        "Any delays on my route?",
        "What street am I on?",
        "Is there a detour I should take?",
    ]

    def test_no_prose_produces_a_suggestion(self):
        leaks = {q: s for q in self.PROSE if (s := loc.find_street_suggestions(q))}
        self.assertEqual(leaks, {}, f"prose leaked suggestions: {leaks}")

    def test_real_misspelling_still_corrected(self):
        s = loc.find_street_suggestions("Any obstacles on Telegrah Avenue?")
        self.assertTrue(any("Telegraph" in v for v in s.values()),
                        f"expected a Telegraph correction, got {s}")

    def test_correctly_spelled_street_is_left_alone(self):
        self.assertEqual(loc.find_street_suggestions("Is Ashby Avenue clear?"), {})


if __name__ == "__main__":
    unittest.main()
