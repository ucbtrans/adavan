"""
Tests for sessions.py -- both backends.

The backend is chosen by whether SESSIONS_TABLE is set, and _TABLE_NAME is read
once at import, so each test class pins the backend it means to exercise rather
than depending on the environment it happens to run in. The file backend is
pointed at a temp file: its default is sessions.json beside the source.
"""

import json
import os
import tempfile
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

import sessions as sess


class _FileBackend(unittest.TestCase):
    """Isolates the JSON-file backend in a temp file."""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(self.path)                     # start with no file at all
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))
        for target, value in (("_SESSIONS_FILE", self.path), ("_TABLE_NAME", None)):
            p = patch.object(sess, target, value)
            p.start()
            self.addCleanup(p.stop)

    def _create(self, address="2020 Telegraph Ave", bearing=0, **kw):
        return sess.create_session(address, 37.866, -122.259, bearing, "N", **kw)


class TestFileBackend(_FileBackend):

    def test_missing_file_reads_as_empty(self):
        self.assertEqual(sess.list_sessions(), [])
        self.assertIsNone(sess.get_session("nope"))

    def test_corrupt_file_reads_as_empty_rather_than_raising(self):
        with open(self.path, "w") as f:
            f.write("{not json")
        self.assertEqual(sess.list_sessions(), [])

    def test_create_then_get_round_trip(self):
        s = self._create()
        got = sess.get_session(s["id"])
        self.assertIsNotNone(got)
        self.assertEqual(got["address"], "2020 Telegraph Ave")
        self.assertEqual(got["bearing_direction"], "N")

    def test_created_session_has_the_fields_app_py_reads(self):
        s = self._create()
        for key in ("id", "started_at", "last_active_at", "address", "lat", "lon",
                    "bearing", "bearing_direction", "street", "destination",
                    "dest_lat", "dest_lon", "route_coords_json",
                    "route_streets_json", "messages"):
            self.assertIn(key, s)

    def test_route_is_stored_as_json_text(self):
        s = self._create(route_coords=[[-122.26, 37.87]], route_streets=["Telegraph Ave"])
        self.assertEqual(json.loads(s["route_coords_json"]), [[-122.26, 37.87]])
        self.assertEqual(json.loads(s["route_streets_json"]), ["Telegraph Ave"])

    def test_absent_route_is_empty_string_not_null(self):
        """app.py does `if route_json:`, so None would break the check."""
        s = self._create()
        self.assertEqual(s["route_coords_json"], "")
        self.assertEqual(s["route_streets_json"], "")

    def test_newest_session_is_listed_first(self):
        first = self._create(address="First St")
        second = self._create(address="Second St")
        listed = sess.list_sessions()
        self.assertEqual([x["address"] for x in listed], ["Second St", "First St"])
        self.assertEqual(listed[0]["id"], second["id"])
        self.assertEqual(listed[-1]["id"], first["id"])

    def test_max_sessions_is_enforced(self):
        for i in range(sess.MAX_SESSIONS + 5):
            self._create(address=f"{i} Test St")
        self.assertEqual(len(sess.list_sessions()), sess.MAX_SESSIONS)

    def test_oldest_sessions_are_the_ones_dropped(self):
        ids = [self._create(address=f"{i} Test St")["id"]
               for i in range(sess.MAX_SESSIONS + 2)]
        kept = {x["id"] for x in sess.list_sessions()}
        self.assertNotIn(ids[0], kept)
        self.assertNotIn(ids[1], kept)
        self.assertIn(ids[-1], kept)

    def test_list_exposes_message_count_not_messages(self):
        s = self._create()
        sess.add_message(s["id"], "user", "hello")
        entry = next(x for x in sess.list_sessions() if x["id"] == s["id"])
        self.assertEqual(entry["message_count"], 1)
        self.assertNotIn("messages", entry)

    def test_add_message_appends_in_order(self):
        s = self._create()
        sess.add_message(s["id"], "user", "first")
        sess.add_message(s["id"], "assistant", "second")
        hist = sess.get_history(s["id"])
        self.assertEqual([(m["role"], m["content"]) for m in hist],
                         [("user", "first"), ("assistant", "second")])

    def test_history_only_exposes_role_and_content(self):
        s = self._create()
        sess.add_message(s["id"], "user", "hello")
        self.assertEqual(set(sess.get_history(s["id"])[0]), {"role", "content"})

    def test_history_accepts_a_preloaded_session(self):
        """app.py passes the session it already fetched, to save a read."""
        s = self._create()
        sess.add_message(s["id"], "user", "hello")
        loaded = sess.get_session(s["id"])
        with patch.object(sess, "_load", side_effect=AssertionError("should not reload")):
            self.assertEqual(len(sess.get_history(s["id"], session=loaded)), 1)

    def test_history_of_unknown_session_is_empty(self):
        self.assertEqual(sess.get_history("no-such-session"), [])

    def test_add_message_to_unknown_session_returns_false(self):
        self.assertFalse(sess.add_message("no-such-session", "user", "hi"))

    def test_find_session_matches_address_and_bearing(self):
        s = self._create(address="2020 Telegraph Ave", bearing=90)
        self.assertEqual(sess.find_session("2020 Telegraph Ave", 90)["id"], s["id"])
        self.assertIsNone(sess.find_session("2020 Telegraph Ave", 180))
        self.assertIsNone(sess.find_session("Elsewhere", 90))

    def test_touch_updates_last_active(self):
        s = self._create()
        before = sess.get_session(s["id"])["last_active_at"]
        sess.touch_session(s["id"])
        self.assertGreaterEqual(sess.get_session(s["id"])["last_active_at"], before)

    def test_touch_unknown_session_is_a_no_op(self):
        sess.touch_session("no-such-session")      # must not raise


class TestDynamoBackend(unittest.TestCase):
    """The path used in AWS, with the table mocked."""

    def setUp(self):
        self.table = MagicMock()
        self.table.get_item.return_value = {}
        self.table.scan.return_value = {"Items": []}
        for target, value in (("_TABLE_NAME", "ada-sessions-test"),):
            p = patch.object(sess, target, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(sess, "_table", return_value=self.table)
        p.start()
        self.addCleanup(p.stop)

    def test_selects_the_dynamo_backend(self):
        self.assertTrue(sess._use_dynamo())

    def test_create_writes_coordinates_as_decimal(self):
        """Floats are rejected by DynamoDB."""
        sess.create_session("2020 Telegraph Ave", 37.866, -122.259, 0, "N",
                            dest_lat=37.87, dest_lon=-122.27)
        item = self.table.put_item.call_args[1]["Item"]
        for key in ("lat", "lon", "dest_lat", "dest_lon"):
            with self.subTest(key=key):
                self.assertIsInstance(item[key], Decimal)

    def test_create_sets_a_ttl(self):
        sess.create_session("2020 Telegraph Ave", 37.866, -122.259, 0, "N")
        self.assertIn("ttl", self.table.put_item.call_args[1]["Item"])

    def test_absent_destination_is_not_written_as_decimal(self):
        sess.create_session("2020 Telegraph Ave", 37.866, -122.259, 0, "N")
        item = self.table.put_item.call_args[1]["Item"]
        self.assertIsNone(item["dest_lat"])

    def test_get_session_decodes_decimals(self):
        self.table.get_item.return_value = {"Item": {
            "id": "s1", "lat": Decimal("37.866"), "lon": Decimal("-122.259"),
            "bearing": Decimal("90"), "messages": [], "started_at": "t",
        }}
        got = sess.get_session("s1")
        self.assertIsInstance(got["lat"], float)
        self.assertIsInstance(got["bearing"], int, "whole numbers should stay ints")
        self.assertAlmostEqual(got["lat"], 37.866)

    def test_decode_recurses_into_nested_structures(self):
        item = {"id": "s1", "messages": [{"n": Decimal("2")}],
                "meta": {"deep": {"v": Decimal("1.5")}}}
        got = sess._dynamo_to_session(item)
        self.assertEqual(got["messages"][0]["n"], 2)
        self.assertEqual(got["meta"]["deep"]["v"], 1.5)

    def test_missing_item_returns_none(self):
        self.table.get_item.return_value = {}
        self.assertIsNone(sess.get_session("nope"))

    def test_list_sorts_newest_first_and_caps(self):
        self.table.scan.return_value = {"Items": [
            {"id": f"s{i}", "started_at": "2026-01-01T00:00:00",
             "last_active_at": f"2026-01-{i+1:02d}T00:00:00",
             "address": f"{i} St", "bearing": 0, "bearing_direction": "N",
             "messages": []}
            for i in range(sess.MAX_SESSIONS + 4)
        ]}
        listed = sess.list_sessions()
        self.assertEqual(len(listed), sess.MAX_SESSIONS)
        stamps = [x["last_active_at"] for x in listed]
        self.assertEqual(stamps, sorted(stamps, reverse=True))

    def test_add_message_appends_server_side(self):
        """list_append avoids a read-modify-write race between concurrent asks."""
        self.assertTrue(sess.add_message("s1", "user", "hello"))
        kwargs = self.table.update_item.call_args[1]
        self.assertIn("list_append", kwargs["UpdateExpression"])
        self.assertEqual(kwargs["ExpressionAttributeValues"][":msg"][0]["content"], "hello")

    def test_add_message_failure_returns_false(self):
        self.table.update_item.side_effect = Exception("throughput exceeded")
        self.assertFalse(sess.add_message("s1", "user", "hello"))

    def test_touch_failure_is_swallowed(self):
        self.table.update_item.side_effect = Exception("throughput exceeded")
        sess.touch_session("s1")            # must not raise

    def test_enforce_max_deletes_only_the_excess_oldest(self):
        over = 3
        self.table.scan.return_value = {"Items": [
            {"id": f"s{i}", "started_at": f"2026-01-{i+1:02d}T00:00:00",
             "last_active_at": f"2026-01-{i+1:02d}T00:00:00"}
            for i in range(sess.MAX_SESSIONS + over)
        ]}
        sess._enforce_max_sessions_dynamo()
        deleted = [c[1]["Key"]["id"] for c in self.table.delete_item.call_args_list]
        self.assertEqual(deleted, ["s0", "s1", "s2"])

    def test_enforce_max_deletes_nothing_when_under_the_limit(self):
        self.table.scan.return_value = {"Items": [
            {"id": "s1", "started_at": "t", "last_active_at": "t"}]}
        sess._enforce_max_sessions_dynamo()
        self.table.delete_item.assert_not_called()

    def test_find_session_returns_the_most_recent_match(self):
        self.table.scan.return_value = {"Items": [
            {"id": "old", "address": "A St", "bearing": 0, "started_at": "t",
             "last_active_at": "2026-01-01T00:00:00"},
            {"id": "new", "address": "A St", "bearing": 0, "started_at": "t",
             "last_active_at": "2026-06-01T00:00:00"},
        ]}
        self.assertEqual(sess.find_session("A St", 0)["id"], "new")


if __name__ == "__main__":
    unittest.main()
