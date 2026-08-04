#!/usr/bin/env python
"""Build docs/simulation-diagram-v2.svg — an illustrated explanation of how the
ADA simulation works.

Everything is self-contained: the van icons and the logo are base64-embedded, and
the map inset is drawn from the project's own `city_streets.json` (Berkeley), so
the SVG needs no external files.

Render to PNG with headless Edge/Chrome:
    msedge --headless --force-device-scale-factor=2 --window-size=2000,1060 \
           --screenshot=simulation-diagram-v2.png file:///.../_wrap.html
"""
import base64
import io
import json
import math
import os

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs", "simulation-diagram-v2.svg")

# ---------------------------------------------------------------- palette ----
SIM = "#2563eb"        # simulated fleet
REAL = "#d97706"       # real van
DB = "#7c3aed"         # event store
MAPC = "#0d9488"       # map panel
RULE = "#dc2626"       # hard limit
INK = "#0f172a"
BODY = "#475569"
FAINT = "#64748b"
HAIR = "#e2e8f0"

W_CANVAS, H_CANVAS = 2000, 1060

# ------------------------------------------------------------ embed assets ---
def _b64_png(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def van_icon(path, flip=False, scale=6):
    """Load a 16x32 van sprite, optionally flip 180 deg, upscale for crisp downsampling."""
    im = Image.open(os.path.join(ROOT, path)).convert("RGBA")
    if flip:
        im = im.transpose(Image.ROTATE_180)
    im = im.resize((im.width * scale, im.height * scale), Image.NEAREST)
    return _b64_png(im)


SIM_VANS = [van_icon(f"van_{i}.png") for i in (2, 1, 3, 4, 5, 6, 7)]
# van_real*.png is drawn pointing *against* travel (see v3.6) - flip it so the
# van faces the way it is going, exactly as makeRealVanIcon does at runtime.
REAL_VAN = van_icon("van_real_16.png", flip=True)

_logo = Image.open(os.path.join(ROOT, "ada_logo.jpg")).convert("RGB")
_logo.thumbnail((220, 220), Image.LANCZOS)
LOGO = _b64_png(_logo)

# --------------------------------------------------------------- map inset ---
MAP_X, MAP_Y, MAP_W, MAP_H = 1544, 242, 392, 500
LAT0, LON0 = 37.8700, -122.2685          # downtown Berkeley
LAT_SPAN = 0.020
_COSL = math.cos(math.radians(LAT0))
LON_SPAN = LAT_SPAN * (MAP_W / MAP_H) / _COSL
S_, N_ = LAT0 - LAT_SPAN / 2, LAT0 + LAT_SPAN / 2
W_, E_ = LON0 - LON_SPAN / 2, LON0 + LON_SPAN / 2
METERS_PER_PX = (LON_SPAN * 111320 * _COSL) / MAP_W


def proj(lat, lon):
    x = MAP_X + (lon - W_) / (E_ - W_) * MAP_W
    y = MAP_Y + (N_ - lat) / (N_ - S_) * MAP_H
    return x, y


def bearing(p, q):
    """Compass bearing p->q in degrees clockwise from north."""
    dx = (q["lon"] - p["lon"]) * _COSL
    dy = q["lat"] - p["lat"]
    return math.degrees(math.atan2(dx, dy))


ROAD_STYLE = {           # (casing width, fill width)
    "motorway": (6.0, 4.4), "motorway_link": (3.0, 1.9),
    "primary": (5.6, 4.1), "primary_link": (3.0, 1.9),
    "secondary": (5.0, 3.6), "secondary_link": (3.0, 1.9),
    "tertiary": (4.6, 3.2), "tertiary_link": (3.0, 1.9),
    "residential": (3.2, 2.0), "unclassified": (3.2, 2.0),
}

streets = json.load(open(os.path.join(ROOT, "city_streets.json")))["streets"]

PAD = 0.0015  # small latitude/longitude pad so lines enter/leave the frame cleanly


def in_view(p):
    return (S_ - PAD) <= p["lat"] <= (N_ + PAD) and (W_ - PAD) <= p["lon"] <= (E_ + PAD)


def road_paths():
    """Two passes of SVG path data: darker casing underneath, white roadway on top."""
    buckets = {}
    for st in streets:
        hw = st.get("highway", "residential")
        if hw not in ROAD_STYLE:
            continue
        for seg in st["segments"]:
            run = []
            for p in seg:
                if in_view(p):
                    run.append(p)
                else:
                    if len(run) > 1:
                        buckets.setdefault(hw, []).append(run)
                    run = []
            if len(run) > 1:
                buckets.setdefault(hw, []).append(run)

    def d_for(runs):
        out = []
        for run in runs:
            pts = ["%.1f,%.1f" % proj(p["lat"], p["lon"]) for p in run]
            out.append("M" + " L".join(pts))
        return " ".join(out)

    order = ["residential", "unclassified", "tertiary_link", "secondary_link",
             "primary_link", "motorway_link", "tertiary", "secondary",
             "primary", "motorway"]
    casing, fill = [], []
    for hw in order:
        if hw not in buckets:
            continue
        cw, fw = ROAD_STYLE[hw]
        d = d_for(buckets[hw])
        casing.append(f'<path d="{d}" stroke="#c9d4de" stroke-width="{cw}" fill="none" '
                      f'stroke-linecap="round" stroke-linejoin="round"/>')
        fill.append(f'<path d="{d}" stroke="#ffffff" stroke-width="{fw}" fill="none" '
                    f'stroke-linecap="round" stroke-linejoin="round"/>')
    return "\n      ".join(casing), "\n      ".join(fill)


def place_label(name, occupied, margin=46, min_dist=32):
    """Pick a point on a street that is inside the frame and clear of icons/labels.

    Street labels otherwise collide with the van icons, which sit on the same
    geometry; this scores candidate points by clearance, preferring central ones.
    """
    best = None
    for st in streets:
        if st["name"] != name:
            continue
        for seg in st["segments"]:
            for i, p in enumerate(seg):
                if not in_view(p):
                    continue
                x, y = proj(p["lat"], p["lon"])
                if not (MAP_X + margin <= x <= MAP_X + MAP_W - margin
                        and MAP_Y + margin <= y <= MAP_Y + MAP_H - margin):
                    continue
                nxt = seg[i + 1] if i + 1 < len(seg) else (seg[i - 1] if i else None)
                if nxt is None:
                    continue
                clear = min((math.hypot(x - ox, y - oy) for ox, oy in occupied),
                            default=999.0)
                if clear < min_dist:
                    continue
                score = clear - 0.18 * math.hypot(x - (MAP_X + MAP_W / 2),
                                                  y - (MAP_Y + MAP_H / 2))
                brg = bearing(p, nxt) if i + 1 < len(seg) else bearing(nxt, p)
                if best is None or score > best[0]:
                    best = (score, x, y, brg)
    return best


def nearest_on(name, tlat, tlon):
    """Point on a named street closest to a target, plus its local bearing."""
    best = None
    for st in streets:
        if st["name"] != name:
            continue
        for seg in st["segments"]:
            for i, p in enumerate(seg):
                if not in_view(p):
                    continue
                d = ((p["lat"] - tlat) * 1.27) ** 2 + (p["lon"] - tlon) ** 2
                nxt = seg[i + 1] if i + 1 < len(seg) else (seg[i - 1] if i else None)
                if nxt is not None and (best is None or d < best[0]):
                    brg = bearing(p, nxt) if i + 1 < len(seg) else bearing(nxt, p)
                    best = (d, p, brg)
    if best is None:
        raise SystemExit(f"no in-view geometry for {name}")
    return best[1], best[2]


# --------------------------------------------------------- svg primitives ----
FONT = ("Segoe UI, system-ui, -apple-system, Helvetica Neue, Arial, sans-serif")


def txt(x, y, s, size=13, fill=BODY, weight=None, anchor=None, extra=""):
    a = f' text-anchor="{anchor}"' if anchor else ""
    w = f' font-weight="{weight}"' if weight else ""
    return f'<text x="{x}" y="{y}" font-size="{size}" fill="{fill}"{w}{a}{extra}>{s}</text>'


def van_img(href, cx, cy, h, brg=0.0, opacity=None):
    w = h / 2.0
    op = f' opacity="{opacity}"' if opacity else ""
    rot = f' transform="rotate({brg:.1f} {cx:.1f} {cy:.1f})"' if brg else ""
    return (f'<image href="{href}" x="{cx - w/2:.1f}" y="{cy - h/2:.1f}" '
            f'width="{w:.1f}" height="{h:.1f}"{rot}{op}/>')


def ev(x, y, kind="sim", size=9):
    """A map/diagram event marker: square, coloured by who owns it."""
    c, s = (SIM, "#1e40af") if kind == "sim" else (REAL, "#9a3412")
    return (f'<rect x="{x - size/2:.1f}" y="{y - size/2:.1f}" width="{size}" height="{size}" '
            f'rx="2" fill="{c}" stroke="{s}" stroke-width="1"/>')


def cross(x, y, r=7.5, color=RULE, wid=2.0):
    return (f'<path d="M{x-r:.1f},{y-r:.1f} L{x+r:.1f},{y+r:.1f} M{x+r:.1f},{y-r:.1f} '
            f'L{x-r:.1f},{y+r:.1f}" stroke="{color}" stroke-width="{wid}" '
            f'stroke-linecap="round" fill="none"/>')


def step_box(x, y, w, h, color, badge, title_lines, body_lines, graphic="",
             caption=None, fill="#ffffff", dashed=False,
             title_fill=INK, body_fill=BODY, cap_fill=FAINT, body_size=13):
    o = []
    dash = ' stroke-dasharray="6 4"' if dashed else ""
    o.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" fill="{fill}" '
             f'stroke="{color}" stroke-width="1.5"{dash}/>')
    o.append(f'<circle cx="{x+26}" cy="{y+26}" r="13" fill="{color}"/>')
    bs = 14 if len(badge) == 1 else 12
    o.append(txt(x + 26, y + 26 + (5 if len(badge) == 1 else 4), badge, bs,
                 "#ffffff", 700, "middle"))
    ty = y + 62
    for line in title_lines:
        o.append(txt(x + 16, ty, line, 16, title_fill, 700))
        ty += 18
    by = ty + 12
    for line in body_lines:
        o.append(txt(x + 16, by, line, body_size, body_fill))
        by += 18
    o.append(graphic)
    if caption:
        o.append(txt(x + w / 2, y + h - 18, caption, 11, cap_fill, None, "middle"))
    return "\n  ".join(o)


def block_strip(x, y, w, h=34):
    """A stylised city block: pavement with a road down the middle."""
    return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="6" fill="#eef2f6" '
            f'stroke="#dbe3ea" stroke-width="1"/>'
            f'<path d="M{x+6},{y+h/2} H{x+w-6}" stroke="#ffffff" stroke-width="5" '
            f'stroke-linecap="round"/>')


# ================================================================== layout ===
P = []          # svg body pieces
A = P.append

A(f'<rect width="{W_CANVAS}" height="{H_CANVAS}" fill="#ffffff"/>')

# ---- title -----------------------------------------------------------------
A(txt(40, 58, "How the ADA simulation works", 30, INK, 700))
A(txt(40, 88, "A simulated fleet and the real van drive the same streets and share one event "
              "store &#8212; with different permissions.", 16, FAINT))
A(f'<image href="{LOGO}" x="1836" y="30" width="124" height="77" opacity="0.95"/>')

# ---- simulated lane --------------------------------------------------------
A(f'<rect x="40" y="130" width="1040" height="340" rx="16" fill="#f8fafc" '
  f'stroke="{HAIR}" stroke-width="1.5"/>')
A(f'<rect x="64" y="150" width="196" height="30" rx="15" fill="{SIM}"/>')
A(txt(162, 170, "SIMULATED FLEET", 13, "#ffffff", 700, "middle", ' letter-spacing="1.2"'))

BY, BH = 196, 250

# 1 - schedule upload (user icon + schedule card)
g = []
ax, ay = 108, BY + 192
g.append(f'<circle cx="{ax}" cy="{ay}" r="18" fill="#dbeafe" stroke="{SIM}" stroke-width="1.5"/>')
g.append(f'<circle cx="{ax}" cy="{ay-5}" r="5.6" fill="{SIM}"/>')
g.append(f'<path d="M{ax-9},{ay+11} C{ax-9},{ay+1} {ax+9},{ay+1} {ax+9},{ay+11} Z" fill="{SIM}"/>')
g.append(f'<path d="M{ax+24},{ay} H{ax+40}" stroke="{SIM}" stroke-width="2" '
         f'marker-end="url(#aBlue)" fill="none"/>')
cx0, cy0 = ax + 46, BY + 166
g.append(f'<rect x="{cx0}" y="{cy0}" width="102" height="52" rx="6" fill="#ffffff" '
         f'stroke="#bfdbfe" stroke-width="1.5"/>')
for i in range(3):
    ry = cy0 + 13 + i * 13
    g.append(f'<circle cx="{cx0+11}" cy="{ry}" r="3.2" fill="{SIM}" opacity="0.85"/>')
    g.append(f'<rect x="{cx0+20}" y="{ry-2.5}" width="26" height="5" rx="2.5" fill="#cbd5e1"/>')
    g.append(f'<rect x="{cx0+50}" y="{ry-2.5}" width="40" height="5" rx="2.5" fill="#e2e8f0"/>')
A(step_box(70, BY, 210, BH, SIM, "1", ["Schedule upload"],
           ["The user uploads a", "schedule of addresses",
            "with times for each", "simulated van."],
           "\n  ".join(g), "one row per stop: time + address"))

# 2 - the fleet drives it
g = []
g.append(f'<path d="M350,{BY+216} H532" stroke="#cbd5e1" stroke-width="2" '
         f'stroke-dasharray="6 5" fill="none"/>')
for i, href in enumerate(SIM_VANS[:5]):
    g.append(van_img(href, 354 + 18 + i * 36, BY + 190, 48))
A(step_box(336, BY, 210, BH, SIM, "2", ["Fleet drives", "the schedule"],
           ["Vans set off address to", "address at the scheduled",
            "times; every position is", "drawn on the map."],
           "\n  ".join(g), "one icon per simulated van"))

# 3 - per-block sweep
g = [block_strip(618, BY + 170, 178)]
g.append(ev(650, BY + 187, "sim"))
g.append(ev(707, BY + 187, "sim"))
g.append(ev(764, BY + 187, "sim"))
g.append(cross(707, BY + 187))
g.append(f'<circle cx="650" cy="{BY+187}" r="10" fill="none" stroke="{RULE}" '
         f'stroke-width="1.6" stroke-dasharray="3 3"/>')
A(step_box(602, BY, 210, BH, SIM, "3", ["Per-block sweep"],
           ["On every block a van", "checks each simulated",
            "event and deletes it on", "expiry or by probability."],
           "\n  ".join(g), "expired or unlucky → removed"))

# hard limit
g = [f'<rect x="920" y="{BY+168}" width="76" height="52" rx="8" fill="#ffffff" '
     f'stroke="#fecaca" stroke-width="1.5"/>']
g.append(ev(958, BY + 194, "real", 13))
g.append(f'<circle cx="958" cy="{BY+194}" r="19" fill="none" stroke="{RULE}" stroke-width="2.5"/>')
g.append(f'<path d="M945,{BY+207} L971,{BY+181}" stroke="{RULE}" stroke-width="2.5" '
         f'stroke-linecap="round"/>')
A(step_box(868, BY, 180, BH, RULE, "!", ["Hard limit"],
           ["A simulated van may", "never remove or alter",
            "a real event made by", "the real van."],
           "\n  ".join(g), "real events are read-only",
           fill="#fef2f6", dashed=True, title_fill="#991b1b",
           body_fill="#7f1d1d", cap_fill="#b91c1c", body_size=12.5))

for x0 in (280, 546, 812):
    A(f'<path d="M{x0},{BY+125} H{x0+50}" stroke="{SIM}" stroke-width="2" fill="none" '
      f'marker-end="url(#aBlue)"/>')

# ---- middle note -----------------------------------------------------------
A(f'<rect x="40" y="500" width="1040" height="76" rx="12" fill="#f8fafc" stroke="#cbd5e1" '
  f'stroke-width="1.5" stroke-dasharray="7 5"/>')
A(txt(560, 530, "Both fleets read and write the same event store &#8212; the difference is "
                "what each is allowed to change.", 15, "#334155", None, "middle"))
A(txt(560, 554, "Simulated vans only tidy up their own objects; the real van is the source "
                "of truth.", 13.5, FAINT, None, "middle"))

# ---- real lane -------------------------------------------------------------
A(f'<rect x="40" y="610" width="1040" height="340" rx="16" fill="#f8fafc" '
  f'stroke="{HAIR}" stroke-width="1.5"/>')
A(f'<rect x="64" y="630" width="140" height="30" rx="15" fill="{REAL}"/>')
A(txt(134, 650, "REAL VAN", 13, "#ffffff", 700, "middle", ' letter-spacing="1.2"'))

RY = 676

# 4 - real van reports in
g = [f'<circle cx="118" cy="{RY+190}" r="28" fill="{REAL}" opacity="0.08"/>']
g.append(van_img(REAL_VAN, 118, RY + 190, 52))
for i, r in enumerate((24, 34, 44)):
    g.append(f'<path d="M{144},{RY+190-r*0.62} A{r},{r} 0 0 1 {144},{RY+190+r*0.62}" '
             f'fill="none" stroke="{REAL}" stroke-width="2" opacity="{0.75 - i*0.2:.2f}" '
             f'stroke-linecap="round"/>')
A(step_box(70, RY, 210, BH, REAL, "4", ["Real van reports in"],
           ["The physical van drives", "and reports its own",
            "location, shown live on", "the same map."],
           "\n  ".join(g), "one live GPS marker"))

# 5a - clear simulated objects
g = [block_strip(352, RY + 170, 178)]
for x0 in (384, 441, 498):
    g.append(ev(x0, RY + 187, "sim"))
    g.append(cross(x0, RY + 187, 7.5, RULE, 1.8))
A(step_box(336, RY, 210, BH, REAL, "5a", ["Clear simulated", "objects"],
           ["On the blocks it covers", "it removes every object",
            "left by the simulated", "vans."],
           "\n  ".join(g), "all simulated events cleared"))

# 5b - record real events
g = [block_strip(618, RY + 170, 178)]
for x0 in (660, 717, 774):
    g.append(ev(x0, RY + 187, "real"))
g.append(f'<circle cx="644" cy="{RY+187}" r="9" fill="{REAL}"/>')
g.append(f'<path d="M639,{RY+187} H649 M644,{RY+182} V{RY+192}" stroke="#ffffff" '
         f'stroke-width="2" stroke-linecap="round"/>')
A(step_box(602, RY, 210, BH, REAL, "5b", ["Record real events"],
           ["It identifies the actual", "objects and events",
            "around it and adds them", "to the database."],
           "\n  ".join(g), "real events written"))

# ground truth
g = [f'<rect x="920" y="{RY+168}" width="76" height="52" rx="8" fill="#ffffff" '
     f'stroke="#fed7aa" stroke-width="1.5"/>']
g.append(ev(958, RY + 194, "real", 13))
g.append(f'<circle cx="958" cy="{RY+194}" r="19" fill="none" stroke="{REAL}" '
         f'stroke-width="2" stroke-dasharray="3 3"/>')
g.append(f'<circle cx="974" cy="{RY+178}" r="9" fill="#15803d"/>')
g.append(f'<path d="M970,{RY+178} l3,3 l6,-6.5" stroke="#ffffff" stroke-width="2.2" '
         f'fill="none" stroke-linecap="round" stroke-linejoin="round"/>')
A(step_box(868, RY, 180, BH, REAL, "&#10003;", ["Ground truth"],
           ["Real events outrank", "simulated ones, and",
            "only the real van may", "edit them."],
           "\n  ".join(g), "real events: full control",
           fill="#fffbeb", dashed=True, title_fill="#92400e",
           body_fill="#78350f", cap_fill="#b45309", body_size=12.5))

for x0 in (280, 546, 812):
    A(f'<path d="M{x0},{RY+125} H{x0+50}" stroke="{REAL}" stroke-width="2" fill="none" '
      f'marker-end="url(#aAmber)"/>')

# ---- line-style legend -----------------------------------------------------
A(f'<rect x="1220" y="170" width="240" height="110" rx="12" fill="#f8fafc" '
  f'stroke="{HAIR}" stroke-width="1.5"/>')
A(txt(1240, 192, "LEGEND", 11, "#94a3b8", 700, None, ' letter-spacing="1.2"'))
A(f'<path d="M1240,214 H1278" stroke="{SIM}" stroke-width="2" fill="none" '
  f'marker-end="url(#aBlue)"/>')
A(txt(1294, 219, "sequence of steps", 12.5))
A(f'<path d="M1242,242 H1276" stroke="{FAINT}" stroke-width="2" fill="none" '
  f'marker-start="url(#aSlate)" marker-end="url(#aSlate)"/>')
A(txt(1294, 247, "database read / write", 12.5))
A(f'<path d="M1240,270 H1278" stroke={chr(34)}{SIM}{chr(34)} stroke-width="2" fill="none" '
  f'stroke-dasharray="7 5" marker-end="url(#aBlue)"/>')
A(txt(1294, 275, "drawn on the fleet map", 12.5))

# ---- event database --------------------------------------------------------
A(f'<rect x="1220" y="350" width="240" height="380" rx="16" fill="#ffffff" '
  f'stroke="{DB}" stroke-width="2"/>')
A(txt(1240, 392, "Event database", 18, INK, 700))
A(txt(1240, 414, "one shared store", 13, FAINT))

A(f'<rect x="1240" y="434" width="200" height="102" rx="10" fill="#eff6ff" '
  f'stroke="#bfdbfe" stroke-width="1.5"/>')
A(ev(1252, 458, "sim", 12))
A(txt(1268, 463, "Simulated events", 14, "#1e40af", 700))
A(txt(1256, 486, "Created and deleted", 12.5))
A(txt(1256, 503, "by the simulated vans.", 12.5))
A(txt(1256, 520, "Wiped by the real van.", 12.5))

A(f'<rect x="1240" y="552" width="200" height="102" rx="10" fill="#fff7ed" '
  f'stroke="#fed7aa" stroke-width="1.5"/>')
A(ev(1252, 576, "real", 12))
A(txt(1268, 581, "Real events", 14, "#9a3412", 700))
A(f'<rect x="1392" y="568" width="40" height="16" rx="8" fill="#fed7aa"/>')
A(txt(1412, 580, "LOCKED", 8.5, "#9a3412", 700, "middle", ' letter-spacing="0.6"'))
A(txt(1256, 604, "Only the real van may", 12.5))
A(txt(1256, 621, "create, alter or delete", 12.5))
A(txt(1256, 638, "these.", 12.5))

A(f'<circle cx="1247" cy="678" r="7" fill="none" stroke="{FAINT}" stroke-width="1.6"/>')
A(f'<path d="M1247,674 V678 L1250,681" stroke="{FAINT}" stroke-width="1.6" fill="none" '
  f'stroke-linecap="round"/>')
A(txt(1262, 682, "Every event carries an expiry;", 12.5))
A(txt(1240, 702, "the map shows it until then.", 12.5))

# ---- fleet map panel -------------------------------------------------------
A(f'<rect x="1520" y="160" width="440" height="780" rx="16" fill="#ffffff" '
  f'stroke="{MAPC}" stroke-width="2"/>')
A(txt(1544, 200, "Fleet map", 18, INK, 700))
A(txt(1544, 222, "downtown Berkeley &#8212; what the user sees", 13, FAINT))

casing, fill = road_paths()
A(f'<clipPath id="mapClip"><rect x="{MAP_X}" y="{MAP_Y}" width="{MAP_W}" '
  f'height="{MAP_H}" rx="10"/></clipPath>')
A(f'<g clip-path="url(#mapClip)">')
A(f'  <rect x="{MAP_X}" y="{MAP_Y}" width="{MAP_W}" height="{MAP_H}" fill="#e9eff4"/>')
A(f'      {casing}')
A(f'      {fill}')

# --- work out where everything sits before drawing, so the street labels can
# --- be placed in whatever space the icons leave free.
FLEET = [
    ("University Avenue", 37.8721, -122.2762, 0),
    ("Shattuck Avenue", 37.8659, -122.2676, 1),
    ("Bancroft Way", 37.8681, -122.2620, 2),
    ("Martin Luther King Junior Way", 37.8766, -122.2708, 3),
    ("Hearst Avenue", 37.8752, -122.2620, 4),
]
fleet_at = []
for name, tlat, tlon, vi in FLEET:
    p, brg = nearest_on(name, tlat, tlon)
    x, y = proj(p["lat"], p["lon"])
    fleet_at.append((x, y, brg, vi))

SIM_EVENTS = [
    ("University Avenue", 37.8721, -122.2705),
    ("Shattuck Avenue", 37.8690, -122.2678),
    ("Milvia Street", 37.8703, -122.2718),
    ("Dwight Way", 37.8630, -122.2660),
    ("Oxford Street", 37.8735, -122.2600),
    ("Addison Street", 37.8705, -122.2745),
]
def pt_on(name, tlat, tlon):
    p, _brg = nearest_on(name, tlat, tlon)
    return proj(p["lat"], p["lon"])


sim_ev_at = [pt_on(n, la, lo) for n, la, lo in SIM_EVENTS]

REAL_EVENTS = [("Shattuck Avenue", 37.8722, -122.2685),
               ("Center Street", 37.8706, -122.2695),
               ("Shattuck Avenue", 37.8700, -122.2680)]
real_ev_at = [pt_on(n, la, lo) for n, la, lo in REAL_EVENTS]

_p, real_brg = nearest_on("Shattuck Avenue", 37.8712, -122.2682)
rvx, rvy = proj(_p["lat"], _p["lon"])

NORTH_AT = (MAP_X + MAP_W - 26, MAP_Y + 30)
SCALE_AT = (MAP_X + 16 + (300 / METERS_PER_PX) / 2, MAP_Y + MAP_H - 18)
occupied = [(x, y) for x, y, _b, _i in fleet_at] + sim_ev_at + real_ev_at + [
    (rvx, rvy), (rvx + 54, rvy - 26),                 # real van + its name chip
    (MAP_X + MAP_W / 2, MAP_Y + 38),                  # DOWNTOWN BERKELEY
    NORTH_AT, SCALE_AT,
]

# street labels, rotated to run along their street
LABELS = [
    ("Shattuck Avenue", "Shattuck Ave"),
    ("University Avenue", "University Ave"),
    ("Martin Luther King Junior Way", "M L King Jr Way"),
    ("Hearst Avenue", "Hearst Ave"),
    ("Bancroft Way", "Bancroft Way"),
    ("Dwight Way", "Dwight Way"),
]
for name, label in LABELS:
    spot = place_label(name, occupied) or place_label(name, occupied, 34, 22)
    if spot is None:
        print(f"  ! no clear spot for {label}, skipped")
        continue
    _s, x, y, brg = spot
    rot = brg - 90.0
    if rot < -90 or rot > 90:
        rot += 180
    A(f'  <text x="{x:.1f}" y="{y:.1f}" font-size="10.5" fill="#64748b" '
      f'text-anchor="middle" transform="rotate({rot:.1f} {x:.1f} {y:.1f})" '
      f'paint-order="stroke" stroke="#ffffff" stroke-width="3.2" stroke-linejoin="round" '
      f'letter-spacing="0.3">{label}</text>')
    occupied.append((x, y))          # keep later labels off this one

A(f'  <text x="{MAP_X+MAP_W/2:.0f}" y="{MAP_Y+38}" font-size="12" fill="#94a3b8" '
  f'text-anchor="middle" font-weight="700" letter-spacing="2.4" paint-order="stroke" '
  f'stroke="#e9eff4" stroke-width="4">DOWNTOWN BERKELEY</text>')

# events first, then vans on top of them
for x, y in sim_ev_at:
    A("  " + ev(x, y, "sim", 8))
for x, y in real_ev_at:
    A("  " + ev(x, y, "real", 8))

for x, y, brg, vi in fleet_at:
    A(f'  <ellipse cx="{x:.1f}" cy="{y+13:.1f}" rx="9" ry="3" fill="#0f172a" opacity="0.13"/>')
    A("  " + van_img(SIM_VANS[vi], x, y, 30, brg))

# the real van: haloed so it reads as the one physical vehicle, and named the way
# the Real tab names it in the UI
A(f'  <circle cx="{rvx:.1f}" cy="{rvy:.1f}" r="26" fill="{REAL}" opacity="0.10"/>')
A(f'  <circle cx="{rvx:.1f}" cy="{rvy:.1f}" r="17" fill="{REAL}" opacity="0.16"/>')
A(f'  <ellipse cx="{rvx:.1f}" cy="{rvy+14:.1f}" rx="10" ry="3.4" fill="#0f172a" opacity="0.16"/>')
A("  " + van_img(REAL_VAN, rvx, rvy, 34, real_brg))
A(f'  <path d="M{rvx+9:.1f},{rvy-11:.1f} L{rvx+21:.1f},{rvy-22:.1f}" stroke="{REAL}" '
  f'stroke-width="1.2" fill="none"/>')
A(f'  <rect x="{rvx+20:.1f}" y="{rvy-36:.1f}" width="70" height="20" rx="10" fill="#ffffff" '
  f'stroke="{REAL}" stroke-width="1.2"/>')
A(f'  <text x="{rvx+55:.1f}" y="{rvy-22:.1f}" font-size="10.5" fill="#9a3412" '
  f'font-weight="700" text-anchor="middle">Alex&#8217;s Van</text>')

# north arrow + scale bar
nx, ny = MAP_X + MAP_W - 26, MAP_Y + 30
A(f'  <circle cx="{nx}" cy="{ny}" r="15" fill="#ffffff" opacity="0.86"/>')
A(f'  <path d="M{nx},{ny-11} L{nx+5},{ny+4} L{nx},{ny+1} L{nx-5},{ny+4} Z" fill="#475569"/>')
A(f'  <text x="{nx}" y="{ny+13}" font-size="7.5" fill="#475569" text-anchor="middle" '
  f'font-weight="700">N</text>')

bar_m = 300
bar_px = bar_m / METERS_PER_PX
bx, by = MAP_X + 16, MAP_Y + MAP_H - 18
A(f'  <path d="M{bx},{by} H{bx+bar_px:.1f}" stroke="#334155" stroke-width="2.5" '
  f'stroke-linecap="butt"/>')
A(f'  <path d="M{bx},{by-4} V{by+4} M{bx+bar_px:.1f},{by-4} V{by+4}" stroke="#334155" '
  f'stroke-width="2.5"/>')
A(f'  <text x="{bx + bar_px/2:.1f}" y="{by-8}" font-size="10" fill="#334155" '
  f'text-anchor="middle" font-weight="600" paint-order="stroke" stroke="#e9eff4" '
  f'stroke-width="3">{bar_m} m</text>')
A("</g>")
A(f'<rect x="{MAP_X}" y="{MAP_Y}" width="{MAP_W}" height="{MAP_H}" rx="10" fill="none" '
  f'stroke="#cbd5e1" stroke-width="1.5"/>')

# map key
ky = 782
A(van_img(SIM_VANS[0], 1556, ky, 26))
A(txt(1580, ky + 5, "Simulated van positions", 14, "#334155"))
A(van_img(REAL_VAN, 1556, ky + 36, 26))
A(txt(1580, ky + 41, "Real van live marker", 14, "#334155"))
A(ev(1556, ky + 72, "sim", 11))
A(txt(1580, ky + 77, "Simulated events", 14, "#334155"))
A(ev(1556, ky + 104, "real", 11))
A(txt(1580, ky + 109, "Real events", 14, "#334155"))
A(txt(1580, ky + 127, "both shown until they expire", 12, FAINT))

# ---- connectors ------------------------------------------------------------
A(f'<path d="M1048,{BY+125} H1320 Q1340,{BY+125} 1340,{BY+145} V342" stroke="{FAINT}" '
  f'stroke-width="2" fill="none" marker-start="url(#aSlate)" marker-end="url(#aSlate)"/>')
A(txt(1184, BY + 115, "reads the block, deletes simulated only", 12.5, BODY, None, "middle"))

A(f'<path d="M1048,{RY+125} H1320 Q1340,{RY+125} 1340,{RY+105} V738" stroke="{FAINT}" '
  f'stroke-width="2" fill="none" marker-start="url(#aSlate)" marker-end="url(#aSlate)"/>')
A(txt(1184, RY + 145, "clears simulated, inserts real events", 12.5, BODY, None, "middle"))

A(f'<path d="M1460,540 H1512" stroke="{DB}" stroke-width="2.5" fill="none" '
  f'marker-end="url(#aViolet)"/>')

A(f'<path d="M441,{BY} V138 Q441,118 461,118 H1720 Q1740,118 1740,138 V152" stroke="{SIM}" '
  f'stroke-width="2" fill="none" stroke-dasharray="7 5" marker-end="url(#aBlue)"/>')
A(txt(1010, 108, "simulated van positions", 12.5, SIM, None, "middle"))

A(f'<path d="M175,{RY+BH} V980 Q175,1000 195,1000 H1720 Q1740,1000 1740,980 V948" '
  f'stroke="{REAL}" stroke-width="2" fill="none" stroke-dasharray="7 5" '
  f'marker-end="url(#aAmber)"/>')
A(txt(1010, 990, "real van live position", 12.5, REAL, None, "middle"))

# =================================================================== emit ====
def marker(mid, color):
    return (f'<marker id="{mid}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" '
            f'markerHeight="6" orient="auto-start-reverse">'
            f'<path d="M0,0 L10,5 L0,10 z" fill="{color}"/></marker>')


svg = (f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
       f'width="{W_CANVAS}" height="{H_CANVAS}" viewBox="0 0 {W_CANVAS} {H_CANVAS}" '
       f'font-family="{FONT}">\n<defs>\n'
       + marker("aSlate", FAINT) + "\n" + marker("aBlue", SIM) + "\n"
       + marker("aAmber", REAL) + "\n" + marker("aViolet", DB) + "\n</defs>\n"
       + "\n".join(P) + "\n</svg>\n")

with open(OUT, "w", encoding="utf-8") as fh:
    fh.write(svg)
print(f"wrote {OUT}  ({len(svg)/1024:.0f} KB)")
print(f"map scale: 1 px = {METERS_PER_PX:.2f} m; bbox lat {S_:.4f}..{N_:.4f} "
      f"lon {W_:.4f}..{E_:.4f}")
