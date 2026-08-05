"""
Tests for detections_adapter.py -- YOLO labels + point cloud -> detections JSON.

This is the offline video pipeline rather than the live path, so the tests stay
on the arithmetic and the file parsing: the angle a bounding box maps to, and
that malformed rows are skipped rather than aborting the file.
"""

import csv
import os
import tempfile
import unittest

import detections_adapter as da


class TestAngle(unittest.TestCase):

    def test_centre_of_frame_is_straight_ahead(self):
        self.assertAlmostEqual(da.xyxy_to_angle(600, 680, 1280), 0.0)

    def test_left_of_centre_is_negative(self):
        self.assertLess(da.xyxy_to_angle(0, 100, 1280), 0)

    def test_right_of_centre_is_positive(self):
        self.assertGreater(da.xyxy_to_angle(1180, 1280, 1280), 0)

    def test_edges_span_the_field_of_view(self):
        left = da.xyxy_to_angle(0, 0, 1280)
        right = da.xyxy_to_angle(1280, 1280, 1280)
        self.assertAlmostEqual(left, -45.0)
        self.assertAlmostEqual(right, 45.0)

    def test_scales_with_image_width(self):
        self.assertAlmostEqual(da.xyxy_to_angle(320, 320, 640),
                               da.xyxy_to_angle(640, 640, 1280))


class TestLoadPointCloud(unittest.TestCase):

    def _csv(self, rows, header=("X", "Y", "Z")):
        fd, path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        self.addCleanup(os.unlink, path)
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)
        return path

    def test_reads_xyz_triples(self):
        path = self._csv([(1, 2, 3), (4, 5, 6)])
        self.assertEqual(da.load_point_cloud(path), [(1.0, 2.0, 3.0), (4.0, 5.0, 6.0)])

    def test_missing_file_is_empty_not_an_error(self):
        self.assertEqual(da.load_point_cloud("no_such_cloud.csv"), [])

    def test_unparseable_rows_are_skipped(self):
        path = self._csv([(1, 2, 3), ("a", "b", "c"), (7, 8, 9)])
        self.assertEqual(da.load_point_cloud(path), [(1.0, 2.0, 3.0), (7.0, 8.0, 9.0)])

    def test_wrong_columns_yield_nothing(self):
        path = self._csv([(1, 2, 3)], header=("A", "B", "C"))
        self.assertEqual(da.load_point_cloud(path), [])


class TestDistanceEstimate(unittest.TestCase):

    def test_no_points_gives_no_estimate(self):
        self.assertIsNone(da.estimate_distance_from_cloud(0, 0, 100, 100, []))

    def test_points_behind_the_camera_are_ignored(self):
        pts = [(0.0, 0.0, -5000.0)]
        self.assertIsNone(da.estimate_distance_from_cloud(0, 0, 1280, 720, pts))

    def test_returns_metres_from_millimetres(self):
        # A point straight ahead projects to the centre of the frame.
        pts = [(0.0, 0.0, 10_000.0)]
        got = da.estimate_distance_from_cloud(0, 0, 1280, 720, pts)
        self.assertAlmostEqual(got, 10.0)

    def test_takes_the_median_of_the_points_in_the_box(self):
        pts = [(0.0, 0.0, 1000.0), (0.0, 0.0, 5000.0), (0.0, 0.0, 9000.0)]
        got = da.estimate_distance_from_cloud(0, 0, 1280, 720, pts)
        self.assertAlmostEqual(got, 5.0)

    def test_points_outside_the_box_do_not_count(self):
        """A box in the top-left cannot claim a point centred in the frame."""
        pts = [(0.0, 0.0, 4000.0)]
        self.assertIsNone(da.estimate_distance_from_cloud(0, 0, 10, 10, pts))


class TestParseYoloDetections(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cloud = os.path.join(self.dir, "cloud.csv")

    def tearDown(self):
        for f in os.listdir(self.dir):
            os.unlink(os.path.join(self.dir, f))
        os.rmdir(self.dir)

    def _labels(self, name, lines):
        path = os.path.join(self.dir, name)
        with open(path, "w") as f:
            f.write("\n".join(lines) + "\n")
        return path

    def test_no_label_files_gives_nothing(self):
        self.assertEqual(da.parse_yolo_detections(self.dir, self.cloud), [])

    def test_maps_class_ids_to_names(self):
        self._labels("frame_001.txt", ["0 0.5 0.5 0.1 0.2", "1 0.25 0.5 0.1 0.2"])
        got = da.parse_yolo_detections(self.dir, self.cloud)
        self.assertEqual([d["label"] for d in got],
                         ["traffic cone", "construction worker"])

    def test_unknown_class_falls_back_to_its_id(self):
        self._labels("frame_001.txt", ["7 0.5 0.5 0.1 0.2"])
        self.assertEqual(da.parse_yolo_detections(self.dir, self.cloud)[0]["label"],
                         "class_7")

    def test_centre_box_reports_zero_angle(self):
        self._labels("frame_001.txt", ["0 0.5 0.5 0.1 0.2"])
        self.assertAlmostEqual(
            da.parse_yolo_detections(self.dir, self.cloud)[0]["angle_deg"], 0.0)

    def test_left_box_reports_a_negative_angle(self):
        self._labels("frame_001.txt", ["0 0.2 0.5 0.1 0.2"])
        self.assertLess(da.parse_yolo_detections(self.dir, self.cloud)[0]["angle_deg"], 0)

    def test_confidence_is_included_when_present(self):
        self._labels("frame_001.txt", ["0 0.5 0.5 0.1 0.2 0.87"])
        got = da.parse_yolo_detections(self.dir, self.cloud)[0]
        self.assertAlmostEqual(got["confidence"], 0.87)

    def test_confidence_is_omitted_when_absent(self):
        self._labels("frame_001.txt", ["0 0.5 0.5 0.1 0.2"])
        self.assertNotIn("confidence", da.parse_yolo_detections(self.dir, self.cloud)[0])

    def test_distance_is_omitted_without_a_point_cloud(self):
        self._labels("frame_001.txt", ["0 0.5 0.5 0.1 0.2"])
        self.assertNotIn("distance_m", da.parse_yolo_detections(self.dir, self.cloud)[0])

    def test_distance_is_included_when_the_cloud_covers_the_box(self):
        self._labels("frame_001.txt", ["0 0.5 0.5 0.9 0.9"])
        with open(self.cloud, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["X", "Y", "Z"])
            w.writerow([0.0, 0.0, 8000.0])
        got = da.parse_yolo_detections(self.dir, self.cloud)[0]
        self.assertAlmostEqual(got["distance_m"], 8.0)

    def test_short_lines_are_skipped(self):
        self._labels("frame_001.txt", ["0 0.5", "0 0.5 0.5 0.1 0.2"])
        self.assertEqual(len(da.parse_yolo_detections(self.dir, self.cloud)), 1)

    def test_unparseable_lines_are_skipped(self):
        self._labels("frame_001.txt", ["x y z w v", "0 0.5 0.5 0.1 0.2"])
        self.assertEqual(len(da.parse_yolo_detections(self.dir, self.cloud)), 1)

    def test_uses_the_most_recent_frame_only(self):
        """Labels are per-frame; the assistant wants the latest, not all of them."""
        self._labels("frame_001.txt", ["0 0.5 0.5 0.1 0.2"])
        self._labels("frame_002.txt", ["1 0.5 0.5 0.1 0.2", "1 0.3 0.5 0.1 0.2"])
        got = da.parse_yolo_detections(self.dir, self.cloud)
        self.assertEqual([d["label"] for d in got],
                         ["construction worker", "construction worker"])


if __name__ == "__main__":
    unittest.main()
