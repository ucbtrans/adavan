# Real Van Feature — Summary

Everything added since introducing the real (physical) van alongside the
simulated fleet. Current version: **v3.6**.

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
