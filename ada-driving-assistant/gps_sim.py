#!/usr/bin/env python
"""
gps_sim.py -- stand-in for the u-blox GNSS receiver mounted on the real van.

On the van, video_collection/video_capture.py reads $GNGGA sentences off a
serial port and appends them to a CSV. There is no receiver on this desk, so
this script drives a plausible route over the real street geometry in
city_streets.json and writes that same CSV -- same columns, same raw NMEA
DDMM.MMMMM coordinate format, same ~1 Hz fix rate.

position_to_s3.py is the other half: it does what position_to_aws.py does on the
van, reading the last valid row once a minute and publishing van_position.json
to S3, where the ada-real-van-ingest Lambda and the fleet map pick it up. So the
whole downstream path runs unmodified -- the only thing replaced is the sensor.

Usage:
    python gps_sim.py                          # write van_gps.csv at 1 Hz
    python gps_sim.py --publish                # also upload to S3 every minute
    python gps_sim.py --speed-mph 30 --seed 7  # faster, reproducible route
    python gps_sim.py --stop-every-min 3       # pause at times, to exercise
                                               # the ingester's STATIONED mode

Ctrl-C stops it; press it twice to give up on a slow S3 upload and exit at once.
"""

from __future__ import annotations

import argparse
import csv
import os
import random
import signal
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone

# Shared with the web app on purpose: the simulator then walks exactly the
# street data the app itself serves, so a reported street name always matches
# something the UI knows about.
from location import _load_streets, compute_bearing_from_segment, haversine_m

CSV_FIELDS = ["Camera_time", "GPS_time", "Latitude", "Latitude_direction",
              "Longitude", "Longitude_direction"]

DEFAULT_CSV = "van_gps.csv"
MPH_TO_MPS = 0.44704

# ── Shutdown ─────────────────────────────────────────────────────────────────
# Every wait in the process is Event.wait on this flag rather than time.sleep, so
# a Ctrl-C is acted on at once. Relying on KeyboardInterrupt landing inside a
# sleep meant waiting out the current one -- up to a second in the fix loop, and
# up to a full --interval in the uploader thread.
STOP = threading.Event()
STOP_TICK = 0.2      # granularity of an interruptible wait, in seconds


def wait_or_stop(seconds: float, stop: threading.Event = STOP) -> bool:
    """Wait up to `seconds`, returning True as soon as a stop is requested.

    Waits in short slices rather than one long one. On Windows a Python signal
    handler only runs between bytecodes, so a thread parked in a single
    Event.wait can sit out the entire timeout before it even sees the signal --
    measured at 4.4 s to quit a loop with a 5 s period. Slicing caps that at
    STOP_TICK no matter which signal arrived.
    """
    deadline = time.monotonic() + seconds
    while not stop.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        stop.wait(min(STOP_TICK, remaining))
    return stop.is_set()


def install_signal_handlers() -> None:
    """First signal asks for a clean stop; a second one gives up and exits.

    The second is the escape hatch for a wedged network call in the uploader:
    that runs on a daemon thread, so a hard exit cannot leave it orphaned.
    """
    def handler(signum, frame):
        if STOP.is_set():
            print("\ngps_sim: forced exit", flush=True)
            os._exit(130)
        print("\ngps_sim: stopping...", flush=True)
        STOP.set()

    # SIGBREAK is Windows-only; SIGTERM is what a process kill sends.
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass        # not the main thread, or unsupported on this platform


# ── Street network ───────────────────────────────────────────────────────────

def _node_key(lat: float, lon: float) -> tuple:
    """Vertices are joined on exact coordinates; OSM shares them at junctions."""
    return (round(lat, 7), round(lon, 7))


def _bearing(a: tuple, b: tuple) -> float:
    """Forward azimuth between two (lat, lon) points, in degrees."""
    return compute_bearing_from_segment({"lat": a[0], "lon": a[1]},
                                        {"lat": b[0], "lon": b[1]})


# Keep the van on through-roads rather than letting it wander side streets: a
# real van on a delivery round spends most of its time on arterials.
_ROAD_CLASS_WEIGHT = {
    "motorway": 2.5, "motorway_link": 1.2, "trunk": 2.5, "trunk_link": 1.2,
    "primary": 2.2, "primary_link": 1.2, "secondary": 2.0, "secondary_link": 1.1,
    "tertiary": 1.6, "tertiary_link": 1.0, "residential": 1.0,
    "unclassified": 0.8, "living_street": 0.4, "service": 0.25,
}


class StreetNetwork:
    """
    The block-level 'ways' of city_streets.json, joined at shared endpoints.

    Each entry in a street's "segments" list already runs intersection to
    intersection, and adjoining streets repeat the junction vertex exactly, so
    indexing ways by their two endpoints is enough to drive the network.
    """

    def __init__(self, streets: list[dict]):
        self.ways: list[dict] = []
        self.at_node: dict[tuple, list[tuple[int, int]]] = {}

        for st in streets:
            name = (st.get("name") or "").strip()
            # Unnamed_* ways are real geometry but would surface as
            # "Unnamed_tertiary" in the UI, so the van stays on named streets.
            if not name or name.startswith("Unnamed_"):
                continue
            oneway = st.get("lanes_backward", 1) == 0
            for seg in st.get("segments", []):
                pts = [(p["lat"], p["lon"]) for p in seg
                       if p.get("lat") is not None and p.get("lon") is not None]
                if len(pts) < 2:
                    continue
                idx = len(self.ways)
                self.ways.append({"name": name, "oneway": oneway, "pts": pts,
                                  "highway": (st.get("highway") or "").strip()})
                for end in (0, len(pts) - 1):
                    self.at_node.setdefault(_node_key(*pts[end]), []).append((idx, end))

        self.drivable = self._largest_scc()

        # Junctions the van can actually reach, so a destination is never one it
        # has no legal route to.
        reachable = {self._exit_key(s // 2, s % 2 == 0) for s in self.drivable}
        self._target_nodes = [k for k, v in self.at_node.items()
                              if len(v) > 1 and (not reachable or k in reachable)] \
            or [k for k, v in self.at_node.items() if len(v) > 1] or list(self.at_node)

    # -- reachability --------------------------------------------------------
    # A "state" is one way driven in one direction, encoded as way*2 + (0 fwd,
    # 1 reverse). Restricting the van to the largest strongly-connected set of
    # states is what makes it impossible to get stuck: by definition every state
    # in it has a legal route onward, and back.
    #
    # Both routing bugs came from states with no legal exit. Way #540, 'CA 13',
    # a one-way motorway_link, dead-ends at one end while its only neighbour at
    # the other is another one-way that would have to be driven backwards. The
    # van drove in and could never leave: 70 km of driving inside a 147 m box.

    def _states(self):
        for i, w in enumerate(self.ways):
            yield i * 2                      # forward is always legal
            if not w["oneway"]:
                yield i * 2 + 1              # reverse only on a two-way

    def _successors(self, state):
        i, fwd = state // 2, state % 2 == 0
        out = []
        for j, end in self.at_node.get(self._exit_key(i, fwd), ()):
            if j == i:
                continue
            forward = (end == 0)
            if not forward and self.ways[j]["oneway"]:
                continue
            out.append(j * 2 + (0 if forward else 1))
        if not self.ways[i]["oneway"]:
            out.append(i * 2 + (1 if fwd else 0))    # U-turn out of a cul-de-sac
        return out

    def _largest_scc(self) -> set:
        """Kosaraju, iteratively — the state graph is far too deep to recurse."""
        legal = set(self._states())
        succ = {s: [t for t in self._successors(s) if t in legal] for s in legal}

        seen, order = set(), []
        for start in legal:
            if start in seen:
                continue
            seen.add(start)
            stack = [(start, iter(succ[start]))]
            while stack:
                node, it = stack[-1]
                for nxt in it:
                    if nxt not in seen:
                        seen.add(nxt)
                        stack.append((nxt, iter(succ[nxt])))
                        break
                else:
                    order.append(node)
                    stack.pop()

        pred = {s: [] for s in legal}
        for s, outs in succ.items():
            for t in outs:
                pred[t].append(s)

        assigned, best = set(), []
        for s in reversed(order):
            if s in assigned:
                continue
            comp, stack = [], [s]
            assigned.add(s)
            while stack:
                n = stack.pop()
                comp.append(n)
                for p in pred[n]:
                    if p not in assigned:
                        assigned.add(p)
                        stack.append(p)
            if len(comp) > len(best):
                best = comp
        # A fixture too small to have a real component: impose no restriction.
        return set(best) if len(best) > 1 else set()

    def __len__(self) -> int:
        return len(self.ways)

    def junction_count(self) -> int:
        return sum(1 for v in self.at_node.values() if len(v) > 1)

    # -- traversal ----------------------------------------------------------

    def _points(self, way_idx: int, forward: bool) -> list[tuple]:
        pts = self.ways[way_idx]["pts"]
        return pts if forward else list(reversed(pts))

    def _exit_key(self, way_idx: int, forward: bool) -> tuple:
        pts = self.ways[way_idx]["pts"]
        return _node_key(*(pts[-1] if forward else pts[0]))

    def _usable(self, way_idx: int, forward: bool) -> bool:
        return (not self.drivable
                or (way_idx * 2 + (0 if forward else 1)) in self.drivable)

    def _candidates(self, node: tuple, came_from: int) -> list[tuple[int, bool]]:
        """Ways leaving `node`, as (way_idx, forward), excluding a U-turn."""
        out = []
        for way_idx, end in self.at_node.get(node, ()):
            if way_idx == came_from:
                continue
            forward = (end == 0)
            # A one-way street can only be entered at its first vertex.
            if not forward and self.ways[way_idx]["oneway"]:
                continue
            if not self._usable(way_idx, forward):
                continue        # entering here would strand the van
            out.append((way_idx, forward))
        return out

    def start(self, rng: random.Random) -> tuple[int, bool]:
        """Pick somewhere the van can drive away from."""
        if self.drivable:
            state = sorted(self.drivable)[rng.randrange(len(self.drivable))]
            return state // 2, state % 2 == 0
        for _ in range(500):
            idx = rng.randrange(len(self.ways))
            for forward in (True, False):
                if forward or not self.ways[idx]["oneway"]:
                    if self._candidates(self._exit_key(idx, forward), idx):
                        return idx, forward
        return 0, True

    def _heading_out(self, way_idx: int, forward: bool) -> float:
        """Bearing the van is travelling as it leaves this way."""
        pts = self._points(way_idx, forward)
        return _bearing(pts[-2], pts[-1])

    def _heading_in(self, way_idx: int, forward: bool) -> float:
        """Bearing the van would take on entering this way."""
        pts = self._points(way_idx, forward)
        return _bearing(pts[0], pts[1])

    def _end_point(self, way_idx: int, forward: bool) -> tuple:
        return self._points(way_idx, forward)[-1]

    def _weight(self, option: tuple[int, bool], heading: float, street: str,
                recent: deque, target: tuple, here: tuple) -> float:
        """
        How attractive a turn is: does it head for the destination, does it keep
        the current heading, is it a road a van would use, and is it new.

        Both the destination and the turn terms are needed. Without them the van
        takes any way it did not arrive on -- including one pointing straight
        back -- and in a district of short winding blocks it circles instead of
        getting anywhere: measured 2.1 km driven for 273 m of progress.
        """
        way_idx, forward = option
        way = self.ways[way_idx]

        turn = abs((self._heading_in(way_idx, forward) - heading + 180) % 360 - 180)
        if turn <= 30:     w = 1.0        # carry straight on
        elif turn <= 60:   w = 0.55
        elif turn <= 90:   w = 0.3        # ordinary turn at an intersection
        elif turn <= 120:  w = 0.1
        else:              w = 0.01       # doubling back: last resort

        # Closing on the destination is what stops it orbiting.
        end = self._end_point(way_idx, forward)
        closer = (haversine_m(here[0], here[1], target[0], target[1])
                  - haversine_m(end[0], end[1], target[0], target[1]))
        w *= 3.0 if closer > 0 else 0.2

        w *= _ROAD_CLASS_WEIGHT.get(way["highway"], 1.0)
        if way["name"] == street:
            w *= 1.4                      # same street continuing through
        if way_idx in recent:
            w *= 0.02                     # just drove this block
        return w

    def _pick_target(self, rng: random.Random, here: tuple,
                     min_m: float = 1500.0, max_m: float = 4000.0) -> tuple:
        """A junction 1.5-4 km away, standing in for the van's next stop."""
        nodes = self._target_nodes
        for _ in range(400):
            cand = nodes[rng.randrange(len(nodes))]
            if min_m <= haversine_m(here[0], here[1], cand[0], cand[1]) <= max_m:
                return cand
        return nodes[rng.randrange(len(nodes))]

    def drive(self, rng: random.Random, turn_chance: float = 0.35,
              arrive_m: float = 250.0):
        """
        Yield (lat, lon, street_name) forever, as a continuous drive.

        The van heads for a destination a few km off, picking a new one on
        arrival -- the same shape as a delivery round, and the reason the track
        advances instead of orbiting. Each junction is still a weighted draw, so
        the roads taken vary; `turn_chance` scales how willing it is to leave the
        street it is on.
        """
        way_idx, forward = self.start(rng)
        recent: deque[int] = deque(maxlen=40)
        target = self._pick_target(rng, self._end_point(way_idx, forward))

        while True:
            way = self.ways[way_idx]
            recent.append(way_idx)
            for lat, lon in self._points(way_idx, forward):
                yield lat, lon, way["name"]

            here = self._end_point(way_idx, forward)
            if haversine_m(here[0], here[1], target[0], target[1]) <= arrive_m:
                target = self._pick_target(rng, here)

            heading = self._heading_out(way_idx, forward)
            node = self._exit_key(way_idx, forward)
            options = self._candidates(node, way_idx)
            if not options:
                # A cul-de-sac: turn around and drive back out, which is what a
                # real van does. Restarting the walk elsewhere instead left the
                # cursor interpolating a straight line across the city -- one
                # such jump measured 5979 m and put ~600 fixes off-road. Staying
                # inside the drivable set means this is always a way back out,
                # never the only move available forever.
                forward = not forward
                continue

            weights = [self._weight(o, heading, way["name"], recent, target, here)
                       for o in options]
            # A turn away from the current street costs turn_chance; staying on it
            # is free. At turn_chance = 1 the name no longer influences the draw.
            weights = [w * (turn_chance if self.ways[o[0]]["name"] != way["name"] else 1.0)
                       for o, w in zip(options, weights)]
            if sum(weights) <= 0:
                weights = [1.0] * len(options)
            way_idx, forward = rng.choices(options, weights=weights, k=1)[0]


class Cursor:
    """Walks a stream of route points at a requested distance per step."""

    def __init__(self, points):
        self.points = points
        lat, lon, street = next(points)
        self.cur = (lat, lon, street)
        self.nxt = None
        self.metres = 0.0

    def advance(self, dist_m: float) -> tuple[float, float, str]:
        remaining = dist_m
        while remaining > 1e-9:
            if self.nxt is None:
                self.nxt = next(self.points)
            seg = haversine_m(self.cur[0], self.cur[1], self.nxt[0], self.nxt[1])
            if seg <= 1e-9:
                self.cur, self.nxt = self.nxt, None
                continue
            if remaining < seg:
                f = remaining / seg
                self.cur = (self.cur[0] + (self.nxt[0] - self.cur[0]) * f,
                            self.cur[1] + (self.nxt[1] - self.cur[1]) * f,
                            self.nxt[2])
                self.metres += remaining
                remaining = 0.0
            else:
                remaining -= seg
                self.metres += seg
                self.cur, self.nxt = self.nxt, None
        return self.cur


# ── NMEA formatting ──────────────────────────────────────────────────────────

def to_nmea_degrees(value: float, is_lat: bool) -> tuple[str, str]:
    """
    Decimal degrees -> the raw ($GNGGA) form video_capture.py logs verbatim:
    ddmm.mmmmm for latitude, dddmm.mmmmm for longitude, plus the hemisphere.
    """
    hemi = ("N" if value >= 0 else "S") if is_lat else ("E" if value >= 0 else "W")
    a = abs(value)
    degrees = int(a)
    minutes = (a - degrees) * 60.0
    raw = degrees * 100 + minutes
    return (f"{raw:010.5f}" if is_lat else f"{raw:011.5f}"), hemi


def gps_time(now: datetime) -> str:
    """NMEA UTC time of fix, hhmmss.ss."""
    return now.strftime("%H%M%S.") + f"{now.microsecond // 10000:02d}"


def camera_time(now: datetime) -> str:
    """Matches video_capture.py's Camera_time exactly."""
    return now.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def fix_row(lat: float, lon: float, now: datetime, valid: bool = True) -> dict:
    if not valid:
        # video_capture.py writes "Null" when no sentence arrived in the window.
        return {"Camera_time": camera_time(now), "GPS_time": "Null",
                "Latitude": "Null", "Latitude_direction": "Null",
                "Longitude": "Null", "Longitude_direction": "Null"}
    lat_raw, lat_dir = to_nmea_degrees(lat, True)
    lon_raw, lon_dir = to_nmea_degrees(lon, False)
    return {"Camera_time": camera_time(now), "GPS_time": gps_time(now),
            "Latitude": lat_raw, "Latitude_direction": lat_dir,
            "Longitude": lon_raw, "Longitude_direction": lon_dir}


# ── CSV sink ─────────────────────────────────────────────────────────────────

class FixLog:
    """
    Appends fixes to one rolling CSV.

    Only the last valid row is ever read downstream, so the file is trimmed
    once it passes max_rows -- that keeps position_to_s3.py's scan cheap over a
    long run. The trim is an atomic replace so a concurrent reader sees either
    the old file or the new one, never a half-written one.
    """

    def __init__(self, path: str, max_rows: int = 10_000):
        self.path = path
        self.max_rows = max_rows
        self.rows = 0
        fresh = not os.path.exists(path) or os.path.getsize(path) == 0
        self.fh = open(path, "a", newline="", encoding="utf-8")
        self.writer = csv.DictWriter(self.fh, fieldnames=CSV_FIELDS)
        if fresh:
            self.writer.writeheader()
            self.fh.flush()

    def write(self, row: dict) -> None:
        self.writer.writerow(row)
        self.fh.flush()
        self.rows += 1
        if self.max_rows and self.rows >= self.max_rows:
            self._trim()

    def _trim(self) -> None:
        keep = self.max_rows // 2
        try:
            with open(self.path, newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))[-keep:]
        except Exception:
            return
        tmp = self.path + ".tmp"
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            w.writeheader()
            w.writerows(rows)
        self.fh.close()
        os.replace(tmp, self.path)
        self.fh = open(self.path, "a", newline="", encoding="utf-8")
        self.writer = csv.DictWriter(self.fh, fieldnames=CSV_FIELDS)
        self.rows = len(rows)

    def close(self) -> None:
        try:
            self.fh.close()
        except Exception:
            pass


# ── Main loop ────────────────────────────────────────────────────────────────

def run(opt) -> int:
    streets = _load_streets().get("streets", [])
    net = StreetNetwork(streets)
    if len(net) < 2:
        print("ERROR: no drivable named streets found in city_streets.json")
        return 1

    rng = random.Random(opt.seed)
    cursor = Cursor(net.drive(rng, turn_chance=opt.turn_chance))
    log = FixLog(opt.csv, max_rows=opt.max_rows)

    speed_mps = opt.speed_mph * MPH_TO_MPS
    period = 1.0 / opt.fix_hz
    step_m = speed_mps * period
    # Expected one stop per --stop-every-min minutes, sampled per fix.
    stop_p = 0.0 if opt.stop_every_min <= 0 else 1.0 / (opt.stop_every_min * 60 * opt.fix_hz)

    print(f"gps_sim: {len(net)} named blocks, {net.junction_count()} junctions")
    print(f"         writing {opt.csv} at {opt.fix_hz} Hz, {opt.speed_mph} mph"
          f"{'' if opt.seed is None else f', seed {opt.seed}'}")
    if opt.publish:
        print(f"         publishing to s3://{opt.bucket}/{opt.key} every {opt.interval}s")
    print("         Ctrl-C to stop")

    if opt.publish:
        import position_to_s3
        threading.Thread(
            target=position_to_s3.publish_loop,
            kwargs=dict(csv_path=opt.csv, bucket=opt.bucket, key=opt.key,
                        region=opt.region, interval=opt.interval,
                        dry_run=opt.dry_run, quiet=True, stop=STOP),
            daemon=True,
        ).start()

    fixes = 0
    stop_until = 0.0
    lat, lon, street = cursor.cur
    try:
        while not STOP.is_set():
            now = datetime.now(timezone.utc)
            moving = time.time() >= stop_until
            if moving:
                lat, lon, street = cursor.advance(step_m)
                if stop_p and rng.random() < stop_p:
                    stop_until = time.time() + opt.stop_seconds
            valid = not (opt.dropout_chance and rng.random() < opt.dropout_chance)
            log.write(fix_row(lat, lon, now, valid=valid))
            fixes += 1
            if fixes % 30 == 0:
                print(f"  {fixes} fixes | {cursor.metres/1000:.2f} km | "
                      f"{lat:.6f},{lon:.6f} | {street}"
                      f"{'' if moving else ' | STOPPED'}      ", end="\r")
            if wait_or_stop(period):
                break
    except KeyboardInterrupt:
        pass            # no handler installed (e.g. run off the main thread)
    finally:
        log.close()
    print(f"\ngps_sim: stopped after {fixes} fixes, "
          f"{cursor.metres/1000:.2f} km driven")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--csv", default=DEFAULT_CSV, help="fix log to append to")
    p.add_argument("--fix-hz", type=float, default=1.0, help="fixes per second")
    p.add_argument("--speed-mph", type=float, default=22.0, help="driving speed")
    p.add_argument("--turn-chance", type=float, default=0.35,
                   help="chance of turning off the current street at a junction")
    p.add_argument("--stop-every-min", type=float, default=0.0,
                   help="average minutes between stops (0 = never stop)")
    p.add_argument("--stop-seconds", type=float, default=45.0, help="length of a stop")
    p.add_argument("--dropout-chance", type=float, default=0.0,
                   help="chance a fix is logged as Null, as on GPS signal loss")
    p.add_argument("--max-rows", type=int, default=10_000,
                   help="trim the CSV once it exceeds this many rows (0 = never)")
    p.add_argument("--seed", type=int, default=None, help="seed for a repeatable route")
    # Publishing (delegated to position_to_s3)
    p.add_argument("--publish", action="store_true",
                   help="also run the S3 uploader in a background thread")
    p.add_argument("--interval", type=float, default=60.0,
                   help="seconds between uploads when --publish is set")
    p.add_argument("--bucket", default="ada-van-service-data")
    p.add_argument("--key", default="van_position.json")
    p.add_argument("--region", default="us-west-1")
    p.add_argument("--dry-run", action="store_true",
                   help="with --publish, build the payload but skip the S3 write")
    return p


if __name__ == "__main__":
    install_signal_handlers()
    sys.exit(run(build_parser().parse_args()))
