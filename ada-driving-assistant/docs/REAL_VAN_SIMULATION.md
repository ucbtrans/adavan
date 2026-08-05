# Driving the real van from your desk

There is one physical van in this project, and it is usually not moving. It might be
parked, the onboard computer might be off, or it might simply be a Tuesday. Meanwhile
the software that follows it around — the live marker, the recorded track, the driving
animation, the street names in the sidebar — needs to be looked at, demonstrated and
occasionally fixed.

So we simulate the van. Not the software that watches it: the **van itself**, or more
precisely the GPS receiver bolted into it. A script on your laptop pretends to be that
receiver, invents a plausible drive around Berkeley, and reports it to exactly the
place the real receiver reports to. Everything downstream then does its ordinary job,
none the wiser.

That last part is the whole point, and it's worth being clear about why.

---

## Why it works this way

The previous version of this simulation lived *inside* AWS. It precomputed a loop of
coordinates, stashed them in a database table, and had the poller read them out
instead of reading the van's feed. It moved a van across a map convincingly enough,
but it skipped the entire real pipeline: the file the receiver writes, the conversion
from raw satellite coordinates, the upload, the fetch, the "has it moved?" decision.
All of that was bypassed, which meant none of it was ever really tested. Twice we
found out the hard way that something in the skipped part was broken.

The simulation now enters the system at the earliest possible point — where the
hardware plugs in — and everything after that is the real thing:

```
   ┌─────────────────────────────────────────┐
   │  YOUR LAPTOP                            │
   │                                         │
   │   gps_sim.py          "the receiver"    │
   │      │  writes a fix every second       │
   │      ▼                                  │
   │   van_gps.csv                           │
   │      │                                  │
   │      ▼  reads the newest fix each minute │
   │   position_to_s3.py   "the uploader"    │
   └──────┼──────────────────────────────────┘
          │
          ▼
      S3: van_position.json      ← the physical van writes here too
          │
          ▼
      ada-real-van-ingest        Lambda, wakes every minute,
          │                      decides whether the van moved
          ▼
      DynamoDB track
          │
          ▼
      Fleet tab → Real           marker, animation, street, timings
```

On the van, those first two boxes are `video_capture.py` (reading the receiver over a
serial cable) and `position_to_aws.py` (uploading). Our two scripts write the same
file, in the same format, to the same place. Swap the real receiver back in and
nothing else needs to change.

---

## Running it

One command, from the project folder:

```powershell
cd C:\Users\agode\Documents\Berkeley\ada\adavan\ada-driving-assistant
.\.venv\Scripts\python.exe gps_sim.py --publish --seed 42
```

Use the virtual environment's Python — the system one doesn't have the AWS library.
`--publish` runs the uploader alongside the receiver, so one command does both jobs.

You'll see it start driving:

```
gps_sim: 3825 named blocks, 2524 junctions
         writing van_gps.csv at 1.0 Hz, 22.0 mph, seed 42
         publishing to s3://ada-van-service-data/van_position.json every 60.0s
  147 fixes | 1.44 km | 37.852410,-122.285370 | Ashby Avenue
```

Then open the **🚐 Fleet** tab, switch the sidebar to **Real**, and watch Alex's Van
get on with its day.

**Give it two minutes before you show anyone.** The map animates the van *between*
two reported positions, so until the second one arrives there is nothing to animate
and the van sits still. Start the script, let it publish twice, then open the map.

To stop it: **Ctrl-C**. It stops on the first press.

---

## What you should be seeing

The van card gives you four things:

> **Alex's Van** — Running
> Sacramento Street → Ashby Avenue
> Berkeley
> last position 23s ago

The street line reads **from → to** while the van is between two reported positions,
and settles to a single street once it arrives. If both ends of a leg are on the same
road it just shows the one name, because "Ashby Avenue → Ashby Avenue" tells you
nothing.

The **last position** counter is the honest one. It climbs to roughly your reporting
interval and resets each time a new position lands. If it keeps climbing past a
minute or two, the feed has stopped — the script has died, the network is out, or you
stopped it and forgot. It is the quickest way to tell a parked van from a broken
pipeline.

On the map the van should move steadily for most of each minute rather than darting
to its next spot and then standing still. It follows real roads, turns at real
junctions, and prefers arterials, because a delivery van spends its day on main roads
rather than exploring cul-de-sacs.

---

## Knobs worth knowing

| Flag | What it does |
|---|---|
| `--seed 42` | Makes the route repeatable. Seed 42 runs Sacramento → Ashby → CA 13, a clean westward run that reads well on a map. Without a seed you get a different drive every time. |
| `--speed-mph 35` | Faster driving, so more ground per reported position. |
| `--interval 20` | Report three times a minute instead of once. Livelier movement; useful for a demo, not what the real van does. |
| `--stop-every-min 3` | Park the van now and then, so you can watch it drop to its idle state and wake up again. |
| `--dry-run` | Do everything except the upload. Good for rehearsing offline. |
| `--dropout-chance 0.02` | Occasionally log a fix as `Null`, the way a receiver does under a bridge. |

`--help` lists the rest.

---

## Things that will surprise you if nobody says

**Restarting teleports the van.** Each run begins somewhere random (or wherever the
seed says), so the first upload after a restart jumps the van across town. That's the
restart, not a bug. It settles from the second position onward.

**Don't press "▶ Start simulation".** That button is the *old* simulation, still
present on the card. Pressing it switches the poller to the coordinate table in the
database, and your GPS script is quietly ignored until you press it again. Nothing
warns you.

**Tracking has to be Running.** Paused makes the poller a no-op — deliberately, it's
the cheap state — so a paused van will not record anything however keenly your script
publishes. The card says which it is.

**The simulator and the real van share one file.** Both write
`van_position.json`, so whichever wrote last wins. Harmless while the van is off the
road; worth sorting out before it comes back.

**Some minutes look slow.** In the tight grid up by the hills the van can drive its
full distance and end up a couple of hundred metres from where it started, because it
turned a lot. It is driving properly; the straight-line distance just doesn't show it.
About one minute in thirty looks like this.

---

## When it isn't working

| What you see | What it usually is |
|---|---|
| Van frozen, counter climbing past a minute | Script stopped, or lost its network. Check the terminal. |
| Van frozen, counter resetting normally | Tracking is **Paused**, or the van is in a `--stop-every-min` stop. |
| Nothing on the map at all | No recorded track yet. Give it two reporting intervals. |
| Street shows "Unknown street" | The published position had no street name. Shouldn't happen now — worth reporting if it does. |
| Van jumps a long way once | A restart. Expected. |
| Van jumps repeatedly | Not expected. Say something. |
| Card shows an old street | Hard-reload the page (Ctrl-Shift-R); the browser may be holding an old copy. |

---

## The files

| File | Job |
|---|---|
| `gps_sim.py` | Pretends to be the GPS receiver. Drives Berkeley's real street map and writes a fix a second. |
| `position_to_s3.py` | Pretends to be the van's uploader. Publishes the newest fix to S3 once a minute. |
| `van_gps.csv` | The fix log, same columns the van's own logger writes. Trimmed automatically; safe to delete. |
| `van_position.json` | The last thing published. What the rest of the system reads. |
| `tests/test_gps_sim.py` | The tests that keep the van on the road and out of dead ends. |

`docs/REAL_VAN.md` has the engineering account of all of this, including the three
attempts it took to make the van drive sensibly and what was wrong with the first two.
