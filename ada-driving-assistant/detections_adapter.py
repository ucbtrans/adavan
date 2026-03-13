"""
Converts YOLO/parallelized_inference output to the format expected by the assistant.

Run this after the main pipeline to produce latest_detections.json.

Usage:
    python detections_adapter.py <yolo_output_dir> <point_cloud_csv> [--out latest_detections.json]
"""

import os
import csv
import json
import math
import argparse
from pathlib import Path


CLASS_NAMES = {0: "traffic cone", 1: "construction worker"}


def xyxy_to_angle(x_min, x_max, image_width=1280):
    """Estimate horizontal angle of a bounding box center relative to image center."""
    HFOV_DEG = 90.0  # ZED camera approximate horizontal field of view
    cx = (x_min + x_max) / 2.0
    norm = (cx / image_width) - 0.5  # -0.5 (left) to +0.5 (right)
    return norm * HFOV_DEG


def load_point_cloud(csv_path: str) -> list[tuple[float, float, float]]:
    """Load X,Y,Z points from a point cloud CSV."""
    points = []
    if not os.path.exists(csv_path):
        return points
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                x, y, z = float(row["X"]), float(row["Y"]), float(row["Z"])
                points.append((x, y, z))
            except (KeyError, ValueError):
                pass
    return points


def estimate_distance_from_cloud(
    x_min, y_min, x_max, y_max, points: list, image_width=1280, image_height=720
) -> float | None:
    """
    Rough distance estimate: find point cloud points whose projected 2-D pixel
    coordinates fall inside the bounding box and return median depth.
    """
    # Without intrinsics we use the bounding box center height as a rough proxy.
    # This is a best-effort heuristic when exact depth is unavailable.
    if not points:
        return None

    # Normalised box bounds
    nx_min = x_min / image_width
    nx_max = x_max / image_width
    ny_min = y_min / image_height
    ny_max = y_max / image_height

    depths = []
    for x, y, z in points:
        # ZED convention: X right, Y down, Z forward (depth)
        if z <= 0:
            continue
        nx = (x / z + 0.5)  # very rough pinhole projection
        ny = (y / z + 0.5)
        if nx_min <= nx <= nx_max and ny_min <= ny <= ny_max:
            depths.append(z / 1000.0)  # mm → m

    if not depths:
        return None
    depths.sort()
    return depths[len(depths) // 2]


def parse_yolo_detections(yolo_dir: str, point_cloud_csv: str) -> list[dict]:
    """
    Parse YOLO label .txt files from yolo_dir and return detections list.
    Each detection: {"label": str, "distance_m": float|None, "angle_deg": float}
    """
    points = load_point_cloud(point_cloud_csv)
    detections = []

    label_files = sorted(Path(yolo_dir).glob("*.txt"))
    if not label_files:
        return detections

    # Use the last label file (most recent frame)
    last_file = label_files[-1]

    with open(last_file) as f:
        for line in f:
            parts = line.strip().split()
            if len(parts) < 5:
                continue
            try:
                cls_id = int(parts[0])
                cx_n, cy_n, w_n, h_n = map(float, parts[1:5])
                conf = float(parts[5]) if len(parts) > 5 else None

                # Convert normalised YOLO coords to pixel coords (assume 1280x720)
                img_w, img_h = 1280, 720
                x_min = (cx_n - w_n / 2) * img_w
                x_max = (cx_n + w_n / 2) * img_w
                y_min = (cy_n - h_n / 2) * img_h
                y_max = (cy_n + h_n / 2) * img_h

                label = CLASS_NAMES.get(cls_id, f"class_{cls_id}")
                angle = xyxy_to_angle(x_min, x_max, img_w)
                distance = estimate_distance_from_cloud(x_min, y_min, x_max, y_max, points, img_w, img_h)

                entry = {"label": label, "angle_deg": round(angle, 1)}
                if distance is not None:
                    entry["distance_m"] = round(distance, 2)
                if conf is not None:
                    entry["confidence"] = round(conf, 3)

                detections.append(entry)
            except (ValueError, IndexError):
                pass

    return detections


def main():
    parser = argparse.ArgumentParser(description="Convert YOLO output to assistant detections JSON")
    parser.add_argument("yolo_dir", help="Directory containing YOLO label .txt files")
    parser.add_argument("point_cloud_csv", help="Path to point cloud CSV from ZED depth sensing")
    parser.add_argument("--out", default="latest_detections.json", help="Output JSON path")
    args = parser.parse_args()

    detections = parse_yolo_detections(args.yolo_dir, args.point_cloud_csv)
    with open(args.out, "w") as f:
        json.dump(detections, f, indent=2)

    print(f"Wrote {len(detections)} detection(s) to {args.out}")


if __name__ == "__main__":
    main()
