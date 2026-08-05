"""
Tests for assistant.py -- the Claude call and everything fed into it.

No API call is made: conftest.py replaces anthropic.Anthropic before this module
is imported, and _client() is lru_cached, so the tests patch the client and
assert on the request that would have been sent. The prompt is where the model's
answer quality comes from, so the tests pin what reaches it -- distances in the
right units, off-route events labelled, checked streets named.
"""

import unittest
from unittest.mock import MagicMock, patch

import assistant as ast_mod


def _reply(text="Clear ahead.", in_tok=100, out_tok=20):
    msg = MagicMock()
    msg.content = [MagicMock(text=text)]
    msg.usage.input_tokens = in_tok
    msg.usage.output_tokens = out_tok
    return msg


LOCATION = {
    "address": "2020 Telegraph Ave, Berkeley",
    "lat": 37.866, "lon": -122.259,
    "bearing": 0, "bearing_direction": "N",
    "destination": "",
}


class TestDistanceFormatting(unittest.TestCase):
    """US customary, because the answers are read by drivers in Berkeley."""

    def test_short_distances_in_feet(self):
        self.assertEqual(ast_mod._fmt_dist(10), "33 ft")
        self.assertEqual(ast_mod._fmt_dist(100), "328 ft")

    def test_switches_to_miles_past_a_thousand_feet(self):
        self.assertTrue(ast_mod._fmt_dist(304).endswith("ft"))
        self.assertTrue(ast_mod._fmt_dist(400).endswith("mi"))

    def test_miles_get_one_decimal_under_ten(self):
        self.assertEqual(ast_mod._fmt_dist(1609.34), "1.0 mi")
        self.assertEqual(ast_mod._fmt_dist(4828), "3.0 mi")

    def test_long_distances_are_whole_miles(self):
        self.assertEqual(ast_mod._fmt_dist(1609.34 * 20), "20 mi")

    def test_zero(self):
        self.assertEqual(ast_mod._fmt_dist(0), "0 ft")


class TestObjectSummary(unittest.TestCase):

    def test_type_is_humanised(self):
        self.assertIn("single cone", ast_mod._obj_summary({"type": "single_cone"}))

    def test_street_and_distance_are_included(self):
        line = ast_mod._obj_summary({"type": "single_cone", "street": "Telegraph Ave",
                                     "_distance_m": 100})
        self.assertIn("on Telegraph Ave", line)
        self.assertIn("328 ft ahead", line)

    def test_distance_is_omitted_when_unknown(self):
        line = ast_mod._obj_summary({"type": "single_cone", "street": "Telegraph Ave"})
        self.assertNotIn("ahead", line)

    def test_blocking_is_spelled_out(self):
        line = ast_mod._obj_summary({"type": "construction_zone",
                                     "blocking": "all_lanes_one_direction"})
        self.assertIn("all lanes one direction", line)

    def test_vehicle_details_are_listed(self):
        line = ast_mod._obj_summary({"type": "car_accident",
                                     "cars": [{"color": "red", "type": "sedan"},
                                              {"color": "blue", "type": "SUV"}],
                                     "police_present": True})
        self.assertIn("red sedan", line)
        self.assertIn("blue SUV", line)
        self.assertIn("police on scene", line)

    def test_lane_counts_are_reported(self):
        line = ast_mod._obj_summary({"type": "single_cone",
                                     "lanes_forward": 2, "lanes_backward": 1})
        self.assertIn("2+1 lanes", line)

    def test_cone_count_and_car_description(self):
        self.assertIn("5 cones", ast_mod._obj_summary({"type": "cone_group",
                                                       "num_cones": 5}))
        self.assertIn("white van", ast_mod._obj_summary({"type": "double_parked_car",
                                                         "car_type": "van",
                                                         "car_color": "white"}))

    def test_unknown_type_does_not_raise(self):
        self.assertIn("unknown", ast_mod._obj_summary({}))


class TestBuildContext(unittest.TestCase):

    def test_includes_the_address_and_heading(self):
        ctx = ast_mod._build_context(LOCATION, [])
        self.assertIn("2020 Telegraph Ave", ctx)
        self.assertIn("N", ctx)

    def test_lists_nearby_events(self):
        ctx = ast_mod._build_context(LOCATION, [
            {"type": "single_cone", "street": "Telegraph Ave", "_distance_m": 50}])
        self.assertIn("single cone", ctx)
        self.assertIn("Telegraph Ave", ctx)

    def test_off_route_events_are_kept_separate(self):
        """The model must not describe an off-route event as being ahead."""
        ctx = ast_mod._build_context(LOCATION, [
            {"type": "single_cone", "street": "Ashby Ave", "_off_route": True}])
        self.assertIn("Off-route", ctx)

    def test_checked_streets_with_nothing_found_are_named(self):
        loc = dict(LOCATION, checked_streets=["Ashby Avenue"])
        ctx = ast_mod._build_context(loc, [])
        self.assertIn("Ashby Avenue", ctx)

    def test_spelling_suggestions_are_passed_through(self):
        loc = dict(LOCATION, street_suggestions={"telegrah ave": "Telegraph Avenue"})
        ctx = ast_mod._build_context(loc, [])
        self.assertIn("Telegraph Avenue", ctx)
        self.assertIn("telegrah ave", ctx)

    def test_parking_context_is_included(self):
        loc = dict(LOCATION, parking={
            "anchor_label": "Solano and Tacoma", "daytime": True,
            "best_chance": 40, "best_street": "Solano Avenue",
            "blocks": [{"street": "Solano Avenue", "occupancy": 60, "chance": 40}],
        })
        ctx = ast_mod._build_context(loc, [])
        self.assertIn("Solano", ctx)

    def test_destination_is_mentioned_when_set(self):
        ctx = ast_mod._build_context(dict(LOCATION, destination="Ashby BART"), [])
        self.assertIn("Ashby BART", ctx)


class TestAnswerQuestion(unittest.TestCase):

    def setUp(self):
        self.client = MagicMock()
        self.client.messages.create.return_value = _reply()
        p = patch.object(ast_mod, "_client", return_value=self.client)
        p.start()
        self.addCleanup(p.stop)

    def _call(self, question="Any hazards ahead?", nearby=None, history=None):
        return ast_mod.answer_question(question, LOCATION, nearby or [], history or [])

    def test_returns_the_reply_text(self):
        answer, _ = self._call()
        self.assertEqual(answer, "Clear ahead.")

    def test_strips_surrounding_whitespace(self):
        self.client.messages.create.return_value = _reply("  padded  ")
        answer, _ = self._call()
        self.assertEqual(answer, "padded")

    def test_reports_token_usage_and_model(self):
        _, usage = self._call()
        self.assertEqual(usage["input_tokens"], 100)
        self.assertEqual(usage["output_tokens"], 20)
        self.assertIn("haiku", usage["model"])

    def test_model_matches_what_is_reported(self):
        _, usage = self._call()
        self.assertEqual(self.client.messages.create.call_args[1]["model"],
                         usage["model"])

    def test_history_is_sent_before_the_new_question(self):
        history = [{"role": "user", "content": "earlier"},
                   {"role": "assistant", "content": "reply"}]
        self._call(history=history)
        messages = self.client.messages.create.call_args[1]["messages"]
        self.assertEqual(messages[:2], history)
        self.assertEqual(messages[-1]["role"], "user")

    def test_history_is_not_mutated(self):
        history = [{"role": "user", "content": "earlier"}]
        self._call(history=history)
        self.assertEqual(len(history), 1)

    def test_conditions_travel_with_the_question(self):
        """Context is re-sent each turn so it cannot go stale mid-conversation."""
        self._call(nearby=[{"type": "single_cone", "street": "Telegraph Ave"}])
        last = self.client.messages.create.call_args[1]["messages"][-1]["content"]
        self.assertIn("[Current conditions]", last)
        self.assertIn("[Question]", last)
        self.assertIn("single cone", last)
        self.assertIn("Any hazards ahead?", last)

    def test_sends_a_system_prompt_and_a_timeout(self):
        self._call()
        kwargs = self.client.messages.create.call_args[1]
        self.assertTrue(kwargs["system"])
        self.assertIn("timeout", kwargs)
        self.assertGreater(kwargs["max_tokens"], 0)

    def test_tags_the_call_with_the_address(self):
        self._call()
        meta = self.client.messages.create.call_args[1]["metadata"]
        self.assertEqual(meta["user_id"], LOCATION["address"])

    def test_long_address_is_truncated_for_the_tag(self):
        long_loc = dict(LOCATION, address="x" * 900)
        ast_mod.answer_question("q", long_loc, [], [])
        meta = self.client.messages.create.call_args[1]["metadata"]
        self.assertLessEqual(len(meta["user_id"]), 512)

    def test_api_errors_propagate_for_the_route_to_handle(self):
        """app.py turns this into a JSON 503; it must not be swallowed here."""
        self.client.messages.create.side_effect = Exception("overloaded")
        with self.assertRaises(Exception):
            self._call()


class TestApiKeyResolution(unittest.TestCase):

    def setUp(self):
        ast_mod._api_key.cache_clear()
        self.addCleanup(ast_mod._api_key.cache_clear)

    def test_without_the_ssm_parameter_the_sdk_reads_the_environment(self):
        with patch.dict(ast_mod.os.environ, {}, clear=False):
            ast_mod.os.environ.pop("ANTHROPIC_SSM_PARAM", None)
            self.assertIsNone(ast_mod._api_key())

    def test_ssm_parameter_is_fetched_and_decrypted(self):
        ssm = MagicMock()
        ssm.get_parameter.return_value = {"Parameter": {"Value": "sk-from-ssm"}}
        with patch.dict(ast_mod.os.environ, {"ANTHROPIC_SSM_PARAM": "/ada/key"}), \
             patch("boto3.client", return_value=ssm):
            self.assertEqual(ast_mod._api_key(), "sk-from-ssm")
        self.assertTrue(ssm.get_parameter.call_args[1]["WithDecryption"])

    def test_key_is_fetched_once_per_container(self):
        ssm = MagicMock()
        ssm.get_parameter.return_value = {"Parameter": {"Value": "sk-from-ssm"}}
        with patch.dict(ast_mod.os.environ, {"ANTHROPIC_SSM_PARAM": "/ada/key"}), \
             patch("boto3.client", return_value=ssm):
            ast_mod._api_key()
            ast_mod._api_key()
        self.assertEqual(ssm.get_parameter.call_count, 1)


if __name__ == "__main__":
    unittest.main()
