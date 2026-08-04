# Running the fleet — a guide for fleet managers

Somebody has to decide where the vans go today, notice when one of them needs to come
off the road, and answer the awkward question of what they actually got up to out
there. That somebody is you, and this guide is about the controls you'll use to do it.

There's a companion picture, [`simulation-diagram-v2.png`](simulation-diagram-v2.png),
which shows how the system works under the bonnet. This is the other half of the
story: what *you* do, which button does it, and the handful of places where the
software will surprise you if nobody warns you first. Where a section lines up with a
block in that diagram, it's flagged, so you can hold the two side by side.

All of it lives behind the **🚐 Fleet** tab. Open it and you'll find a map taking up
most of the window, a column of van cards down the left, and a thin strip of counters
along the bottom. The sidebar splits into **Simulated** and **Real**, because the
fleet is really two fleets: a crowd of simulated vans that go where you tell them, and
one physical van that goes where it likes and tells *you* about it afterwards.

You'll spend almost all your time on the Simulated side. That's where the work is.

---

## 1. Giving the fleet its day's work

*(diagram block ① — Schedule upload)*

A van with nothing scheduled just sits at base, and the sidebar doesn't hide the fact:
its card reads **No schedule** and offers exactly one button, **↑ Import CSV**. Fairly
broad hint.

There are three ways to fill a day, and they're good for quite different things.

### Import a CSV

Expand the van's card, hit **↑ Import**, pick a file, and that van's day is rebuilt
from it. Here's what the file needs to contain:

| Column | What goes in it |
|---|---|
| `seq` | Stop number — 1, 2, 3… |
| `start_time` | When the ride should begin, as `HH:MM` |
| `from_address` | Pickup address |
| `to_address` | Drop-off address |
| `from_lat`, `from_lon`, `to_lat`, `to_lon` | Coordinates — optional, and see below |

Keep the header row: the importer reads column *names*, not positions, so the order
doesn't matter but the spelling does. Rows missing both a `from_address` and a
`to_address` are skipped without complaint, which turns out to be a tidy way of
leaving a blank line between shifts.

One thing that catches people out. **Those coordinate columns are ignored.** The
importer geocodes the addresses itself, every single time, no matter what's in the lat
and lon fields. They're there so an exported file survives a round trip, not so you
can steer a van by decimal degrees. If a van turns up somewhere baffling, the address
is what to go and look at.

### Export, edit, import back

The **↓ CSV** button hands you the van's current day as something like
`van_3_2026-08-01.csv`, same columns, properly quoted. This is far and away the
easiest route to a working import file: take a van that already has a sensible day,
open it in a spreadsheet, change the addresses, send it back. Much less painful than
composing one from scratch and discovering your header row was wrong.

### Or let the system make one up

The **⚡** button at the top of the sidebar builds a full day for every active van out
of the built-in address pool. Useful when you want traffic on the map and genuinely
don't care who goes where.

Two caveats keep **⚡** from being as useful as it looks. It's strictly a
fill-in-the-blanks button — if the date already has schedules, it does nothing
whatsoever rather than replacing them. And it's frequently unnecessary anyway, because
opening a date with no schedules generates them for you. In day-to-day use you'll
reach for it rarely; importing and hand-editing are the real tools.

---

## 2. Watching them drive

*(diagram block ② — Fleet drives the schedule)*

Once there are schedules, the vans get on with it and your job becomes watching.

Each one shows up as a coloured icon on the map, nose pointed the way it's heading,
with a matching card in the sidebar. The chip on that card is the quickest read on
what a van is up to:

- **Ride 3** — carrying a passenger, on the third scheduled ride
- **Deadhead** — driving empty towards the next pickup
- **Returning** — heading home to base
- **At Base** — parked, nothing on
- **Paused** — you stopped it, which is §4's business

Expand a card and the whole day unfolds as a table that keeps pace with the clock: the
ride in progress is highlighted, the ones already done fade into the background. It
answers "how far through the day is Van 5?" without you having to work anything out.

**The clock is yours to push around.** Watching a day of driving at life speed is a
poor use of an afternoon, so there are **1×** and **10×** buttons, plus **↺** to snap
back to the real current time. Ten-times is how most people watch a full day — it
takes about as long as a coffee.

**And you can go back.** The Replay controls take a past date and an hour and play it
back, again at 1× or 10×, with a reset button and **✕ Live** to return to the present
when you've seen enough. Worth being clear about what this is: replay works from what
was actually recorded at the time, so it answers "what on earth happened at two
o'clock yesterday" rather than running the day again and hoping it repeats.

---

## 3. Changing one van's day

Plans change, and you shouldn't have to build a CSV over a single wrong address.

Expand the van's card and the schedule table is editable where it sits. Three fields
per row — **time**, **from**, **to** — and a **Save** button underneath. Addresses are
geocoded when you save, so "2150 Shattuck Ave, Berkeley" is a perfectly good thing to
type; you don't need coordinates.

Nothing else in the fleet is affected. Editing Van 4 is an operation on Van 4 and
nobody else.

Two neighbouring buttons round this out. **＋ Van** adds a van: it picks a free number,
generates a schedule, and drops the van wherever it ought to be given the time of day,
rather than parking a fresh one at base at three in the afternoon. **🗑** removes a
van, schedule and all.

---

## 4. Taking a van off the road — and putting it back

Every card has a **⏸ Pause** button. Press it and the van stops exactly where it is,
its chip flips to **Paused**, and the rest of the fleet carries on without noticing.

Restarting is where it gets interesting, and it's worth understanding before you need
it. Press **▶ Restart** and the van does *not* resume as though no time had passed —
the clock kept running while it stood there, so its old timings are now fiction. The
system instead rebuilds the remainder of the day around two facts: where the van is,
and what time it is. In practice that means:

- Rides it had already completed stay completed, and are skipped.
- The ride it was halfway through is finished from wherever it froze, straight to that
  ride's drop-off.
- Everything still to come is re-timed from that point, in order.

The upside is that a van which sat out an hour returns with a day it can actually
achieve, instead of an hour of impossible catching up. The cost is worth saying
plainly: **restarting rewrites that van's remaining schedule.** The original times for
rides it hadn't started are gone, and there's no undo. If those times matter to you,
export the CSV before you pause — it takes two seconds and saves an argument.

---

## 5. What the vans did out there

*(diagram block ③ — Per-block sweep)*

This part of the system isn't really about driving at all. As the vans move, they
inspect the blocks they pass along and maintain the shared event store — the potholes,
blockages and obstacles that make up the world the assistant reasons about. It's the
fleet's real output, and there are three ways to look at it, in rising order of
detail.

**The counters**, along the bottom: **events total**, **checked**, **cleared**,
**created**. Total is what's live on the map this moment; the other three tally what
the fleet has done since you loaded the page. When `cleared` climbs and `total` holds
steady, your vans are tidying up roughly as fast as the world makes mess.

**The map** shows events as markers that come and go as the vans work — a new one
flashes as it lands, a cleared one simply disappears. The map is the fastest way to
see *where* the fleet is having an effect. For *what*, you want the log.

**The log** sits behind the **📋 Log** button: a running transcript, one line per
action, filterable across the top.

- **Checked** — *Van 3 on "University Avenue": found 2 event(s): Pothole(4m12s), …*
- **Created** — *Van 3 created pothole on "University Avenue"*
- **Cleared** — *Van 3 cleared Pothole on "University Avenue"*
- **All** — everything above, plus schedule loads, replay activity and errors

There's a per-van dropdown too, so you can follow one van through its whole day, and
**Copy** to lift the transcript straight into a bug report. **Clear** empties it when
the noise gets ahead of you.

Three things about events that the interface doesn't tell you, and all three explain
something you'd otherwise puzzle over.

**Vans create as well as clear.** Block ③ of the diagram describes the sweep — vans
deleting events as they pass — but a van also has a small random chance of *seeding* a
brand-new event on its own route as it drives. That's why there's a **Created** filter
in the log and a `created` column in the counters. Without it the world would
gradually empty out and stay empty.

**Vans only touch their own route.** A van considers events on the streets its current
route actually covers and ignores everything else, so two vans working the same
neighbourhood don't end up squabbling over the same pothole.

**Every event gets a minute's grace.** A newly created event can't be removed for its
first 60 seconds, no matter who drives past. After that, the next van down the block
clears it. One honest footnote: block ③ of the diagram says events go "on expiry or by
probability", which is the intended design, but the current build simply clears
anything past the 60-second grace period. If you've been wondering why events never
seem to linger, that's why.

---

## 6. The one real van

*(diagram blocks ④, ⑤a, ⑤b)*

The **Real** sub-tab is a shorter story, because you don't dispatch a physical van from
a web page. You watch it, and you decide whether it's recording.

It lists the van — **Alex's Van** unless you rename it, and you can, right there in the
list — along with the street and city it's on at the moment. On the map it's the grey
van icon, deliberately unlike the coloured simulated fleet, so you never mistake one
for the other.

There are two controls. **Start / Pause tracking** does what it says: pausing freezes
the van on the map and stops the recorder doing any work at all, which makes it the
cheap state to leave things in overnight. Resume and it grabs the current position
straight away rather than waiting for the next cycle. **Simulation**, meanwhile, walks
a van around a precomputed loop of real streets when no physical van is out driving —
it's how you check the whole record-and-play-back path still works. The two are wired
together sensibly: starting the simulation switches tracking on, and pausing tracking
stops the simulation.

Both settings live in the database rather than in your browser, so everyone looking at
the system sees the same thing, and a page reload doesn't quietly undo your decision.

What the real van does to the event store is the mirror image of the simulated fleet.
It clears out simulated objects on the blocks it covers, then records what's genuinely
there — blocks ⑤a and ⑤b. And the rule in the diagram's red box holds everywhere: a
simulated van will never remove or alter something the real van recorded. Where the
two disagree, the real van wins.

---

## Five things that will surprise you

**The log lives in your browser tab, not on the server.** Reload and it's gone. If
you're looking at something interesting, hit **Copy** while it's still on screen.

**The counters reset with the page too.** They measure this session, not all time.

**Import replaces, it doesn't merge.** Bringing in a CSV rewrites that van's entire
day. No undo. Export first if the current schedule is worth anything to you.

**Restart rewrites the remaining schedule.** Covered in §4, repeated here because it's
the one that catches people.

**Schedules are per van, per date.** Today's careful edits don't follow a van into
tomorrow, and opening a fresh date will generate a new day from the address pool
rather than leaving the fleet standing idle.

---

## The short version

| You want to… | Go to |
|---|---|
| Give a van a day's work | Van card → **↑ Import** (or **⚡** for the whole fleet) |
| Save a schedule to edit offline | Van card → **↓ CSV** |
| Fix one stop | Expand van card → edit the row → **Save** |
| Add or remove a van | **＋ Van** / **🗑** on the card |
| Stop a van where it stands | **⏸ Pause** on the card |
| Put it back to work | **▶ Restart** — rebuilds the rest of its day |
| Watch the day go by faster | **1× / 10×** speed buttons |
| Look at a past day | Replay controls → date + hour → **▶ Start Replay** |
| See what the fleet did to the map | **📋 Log**, filtered by Checked / Created / Cleared |
| Watch the physical van | **Real** sub-tab |
