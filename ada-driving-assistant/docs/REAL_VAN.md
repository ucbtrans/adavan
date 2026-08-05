# Real Van Feature — Summary

Everything added since introducing the real (physical) van alongside the
simulated fleet. Current version: **v3.7**.

## 1. Icon (tag `v3.1`)
- Created `van_real.png` (+ `van_real_16.png`) from `Honda Van.png`: **background
  removed** via border flood-fill (preserving chrome), rotated to portrait to
  match the fleet convention, and **red tail lights + warm headlights**
  composited in the house style (colors sampled from `van_top2.png`).

## 2. Live marker on the map (tag `v3.1`)
- **Backend proxy** `GET /api/fleet/real-van` — server-side fetch of the external
  `van_position.json` (the source bucket sends no CORS headers, so the browser
  can't read it directly).
- **Frontend**: `makeRealVanIcon`, marker lifecycle, heading derived from
  movement between polls.
- `deploy.sh` uploads `van_real.png`; added a Flask route for local dev.

## 3. Simulated / Real sub-tabs (tag `v3.2`)
- Fleet sidebar split into **Simulated** (existing schedule/replay + van list)
  and **Real** panes.
- **Real** tab lists real vans (one for now — **"Alex's Van"**): **editable name**
  (persisted to `localStorage`, mirrored on the marker tooltip) and live
  **street + city** (city derived via `vanCityAt` bbox lookup).

## 4. Recording + driving animation (tag `v3.3`)
- **DynamoDB `ada-real-van-positions`** — per-minute position time series, PK
  `van_id` / SK `ts`, ~70 min TTL.
- **`ada-real-van-ingest` Lambda** on a 1-min EventBridge schedule — **adaptive
  poller**: ACTIVE mode polls every ~5 s (catches network-delayed reports) and
  records movement; after 5 min idle drops to STATIONED (once-a-minute
  heartbeat). Treats a change in the feed's `Time` as movement.
- **`GET /api/fleet/real-van/history`** — returns the track.
- **Frontend** polls history and **animates the van driving along OSRM road
  routes** between points (speeds up when catching up); parked minutes render
  stationary.

## 5. Start/Pause tracking + Simulation (v3.4)
- **Per-van DB flags** (`tracking`, `sim`) in the STATE item; `GET/POST
  /api/fleet/real-van/status`.
- **Pause** → ingest Lambda no-ops (~ms, cost saving) and the on-map van
  freezes; **Resume** captures the current position instantly and continues.
- **Simulation** for validation (no real van yet): builds a loop
  **current → base → back using different streets** (OSRM, ~95% distinct return
  route, validated), stored in the DB; the Lambda drives it (~90 s/loop,
  repeats). Reuses the same table + map playback.
- **Coupling**: pause stops the sim; starting the sim forces tracking → Running.
  All statuses are DB-backed and polled every 5 s, so they're **visible to all
  users**. Concurrency-safe (Lambda re-reads/merges STATE before saving).

## 6. Precomputed-waypoint simulation (v3.5)
- The van appeared to **move back and forth** during simulation: the ingester
  emitted continuous positions and the frontend re-routed via OSRM between them,
  so consecutive legs disagreed about the path.
- **`sim_start` now precomputes ordered waypoints** (`SIM_STEP_M = 650`, ~1 min of
  city driving apart) along the loop, each tagged with its distance-along-route,
  and saves them plus the route polyline in the DB.
- **Ingester emits waypoints in strict index order** — `sim_index` is persisted
  and wraps at the loop end (`SIM_EMIT_SECONDS = 3`, accelerated playback).
- **`GET /api/fleet/real-van/sim-path`** returns the loop polyline.
- **Frontend** draws the route dashed while simulating and animates the van along
  the actual polyline slice between waypoints — exact on-route motion, no
  re-routing, no zig-zag. Real-feed mode still uses OSRM.
- **Residual back-and-forth**: two overlapping ingester invocations (a manual
  invoke plus the scheduled tick, or a run exceeding 60 s) each advanced their own
  `sim_index` and interleaved out-of-order waypoints. Fixed with
  `ReservedConcurrentExecutions: 1` so only one invocation runs at a time.

## 7. Icon direction of travel (v3.6)
- The real van icon was drawn pointing **opposite** to travel, so it looked like
  it was driving in reverse. `makeRealVanIcon` now flips the icon image 180° so it
  composes correctly with the heading rotation.

## 8. Local GPS simulator (tag `v3.7`)

Replaces the precomputed-waypoint simulation as the way to exercise the feature
without a van on the road. Rather than a table of coordinates inside the
ingester, the simulation now enters the system where the hardware does, so
everything downstream runs unmodified and is genuinely under test.

- **`gps_sim.py`** stands in for the u-blox receiver. It drives the real street
  geometry in `city_streets.json` and appends fixes to a rolling CSV with the
  same columns `video_collection/video_capture.py` writes from `$GNGGA`
  sentences, in the same raw `DDMM.MMMMM` form, at the same ~1 Hz. Blocks are
  chosen by a weighted draw at each junction: heading continuity, a destination
  1.5–4 km off (re-picked on arrival, like a delivery round), road class, and a
  penalty for blocks just driven. One-way streets are only entered at their
  first vertex. `--seed` makes a route repeatable.
- **`position_to_s3.py`** is the desk-side `position_to_aws.py`: last valid row
  → decimal degrees → street name → `van_position.json` → S3, once a minute.
  Two differences from the van's copy — the street comes from a nearest-segment
  search over `city_streets.json` rather than osmnx (no network call, no geo
  stack, and the names match what the web app already serves), and the
  hemisphere is read from the CSV's direction columns instead of assuming west.
- Run both with one command: `python gps_sim.py --publish`. `--dry-run` builds
  the payload without writing to S3; `--stop-every-min` exercises the
  ingester's STATIONED path.
- **Nothing else changed.** `ada-real-van-ingest` polls the feed in its ordinary
  ACTIVE mode and the map animates via OSRM, exactly as it does for the physical
  van. Tracking must be **Running** — the ingester no-ops while paused.

**Routing took three passes**, and the first two were wrong in ways only visible
on the real network:

1. Excluding only the block the van arrived on let it take a different way
   heading straight back. It circled — 2.1 km driven for 273 m of progress, with
   once-a-minute samples 33 m apart, which reads on the map as a van jittering in
   place. Fixed with heading continuity plus a destination.
2. Restarting the walk at a dead end teleported the van, and because the cursor
   interpolates between whatever points it is given it drew a straight line
   across the city — one jump of 5979 m, with ~600 fixes off-road, which is what
   published an empty `Street`. Fixed by turning around instead.
3. That turnaround then exposed a **one-way trap**: way #540, `CA 13`, a one-way
   `motorway_link`, dead-ends one end while its only neighbour at the other is
   another one-way that would have to be driven backwards. The van drove in and
   spent 70 km inside a 147 m box.

All three were the same underlying thing — the network contains states you can
enter but not leave. The van is now restricted to the largest
**strongly-connected** set of (way, direction) states, computed once at startup
with an iterative Kosaraju (4737 of 6427 states, 0.12 s). Every state in it has a
legal route onward and back, so stranding is impossible by construction and no
teleport is needed to recover; cul-de-sacs still work, since a U-turn is modelled
as an edge. Median minute now advances ~549 m of the ~590 m possible at 22 mph,
with 3% of minutes under 100 m.

`tests/test_gps_sim.py` pins all of it: no state without an exit, no route step
longer than the longest real block, half an hour of driving must roam more than a
kilometre, the NMEA round-trip (worst case ~1 cm), and the published payload's
four load-bearing keys.

**Caveat:** `template.yaml` hard-codes one `REAL_VAN_URL` for every stack, so the
simulator and the physical van share `van_position.json` — whichever writes last
wins. Production is unaffected today only because it is still v2.3 and has no
real-van feature at all. Give dev its own key before that changes.

## Infrastructure fixes along the way
- **`deploy.sh` zip-path bug** — the manual step was uploading stale Lambda code
  (`../../` vs `../../../`); the API silently ran old code until fixed.
- **Package bloat** — `CodeUri: .` was nesting the prior `lambda.zip` into each
  build (200 MB and climbing). Now removes the stale zip and prunes non-runtime
  files → **~21 MB, stable**.

## Key files
- `real_van_ingest.py` — adaptive poller + simulation driver (Lambda handler).
- `app.py` — `/api/fleet/real-van`, `.../history`, `.../status` (GET/POST),
  `.../sim-path`, OSRM loop-path builder + waypoint precomputation.
- `templates/index.html` — Simulated/Real sub-tabs, real-van list, marker
  playback + driving animation, status polling and controls.
- `template.yaml` — `RealVanPositionsTable`, `RealVanIngestFunction` + 1-min
  schedule, IAM.

## Version history
| Tag  | What |
|------|------|
| v3.1 | Real van icon + live GPS marker; `deploy.sh` zip-path fix |
| v3.2 | Simulated/Real fleet sub-tabs + real van list |
| v3.3 | Position recording + on-map driving animation |
| v3.4 | Start/pause tracking + validation simulation |
| v3.5 | Precomputed-waypoint simulation along a drawn route (+ single-concurrency ingester) |
| v3.6 | Real van icon faces its direction of travel |
| v3.7 | Local GPS simulator replaces the waypoint table; playback paced to the reporting interval; from/to street and position age on the card |
