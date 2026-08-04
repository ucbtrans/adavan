"""
smoke_api.py — post-deploy smoke test against a deployed ADA API.

Exercises one endpoint per subsystem, with an emphasis on what v3.6 changed:
the SSM-sourced Anthropic key (/api/ask), real van tracking, and fleet
schedules. Read-only by default; --write adds the checks that create a session
and spend Anthropic tokens.

Usage:
    python tests/manual/smoke_api.py --api https://xxxx.execute-api.us-west-2.amazonaws.com/prod
    python tests/manual/smoke_api.py --api URL --write

Exit code is 0 only if every check passed. Run it against a dev stack: the
--write checks put rows in whichever tables the target stack is wired to.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

TIMEOUT = 100          # /api/ask can take tens of seconds on a cold Lambda


def _call(api, path, method="GET", body=None, timeout=TIMEOUT):
    """Return (status, parsed_body_or_text, elapsed_seconds)."""
    url  = api.rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req  = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw, status = r.read(), r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}", time.time() - start
    elapsed = time.time() - start
    text = raw.decode("utf-8", "replace")
    try:
        return status, json.loads(text), elapsed
    except json.JSONDecodeError:
        return status, text, elapsed


class Checks:
    def __init__(self):
        self.results = []

    def record(self, name, ok, detail=""):
        self.results.append((name, ok, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
        return ok

    def summary(self):
        failed = [n for n, ok, _ in self.results if not ok]
        print("\n" + "=" * 66)
        print(f"  {len(self.results) - len(failed)}/{len(self.results)} checks passed")
        for n in failed:
            print(f"    FAILED: {n}")
        print("=" * 66)
        return not failed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", required=True, help="API base URL including stage")
    ap.add_argument("--write", action="store_true",
                    help="also create a session and call /api/ask (spends tokens)")
    ap.add_argument("--expect-version", default="",
                    help="version label the served index.html must contain, e.g. v3.6")
    args = ap.parse_args()

    c = Checks()
    print(f"Smoke testing {args.api}\n")

    # ── Core plumbing ───────────────────────────────────────────────────────
    print("Core:")
    st, body, el = _call(args.api, "/")
    ok = c.record("GET / returns HTML", st == 200 and isinstance(body, str)
                  and "<html" in body.lower(), f"status={st} {el:.1f}s")
    if ok and args.expect_version:
        c.record(f"index.html labelled {args.expect_version}",
                 args.expect_version in body,
                 "" if args.expect_version in body else "label not found in HTML")

    st, body, el = _call(args.api, "/api/location/random")
    addr = body.get("address") if isinstance(body, dict) else None
    c.record("GET /api/location/random", st == 200 and bool(addr),
             f"status={st} {el:.1f}s" + (f" addr={addr!r}" if addr else f" body={body!r}"))

    st, body, el = _call(args.api, "/api/session/list")
    c.record("GET /api/session/list", st == 200 and isinstance(body, list),
             f"status={st} {el:.1f}s n={len(body) if isinstance(body, list) else '?'}")

    # ── Events (DynamoDB) ───────────────────────────────────────────────────
    print("\nEvents:")
    st, body, el = _call(args.api, "/api/fleet/events")
    evts = body.get("events") if isinstance(body, dict) else None
    c.record("GET /api/fleet/events", st == 200 and isinstance(evts, list),
             f"status={st} {el:.1f}s n={len(evts) if isinstance(evts, list) else '?'}")

    st, body, el = _call(args.api,
                         "/api/events/block?street=Telegraph+Ave&lat=37.866&lon=-122.259")
    c.record("GET /api/events/block", st == 200 and isinstance(body, dict)
             and "events" in body, f"status={st} {el:.1f}s")

    # ── Fleet (v3.0/v3.6) ───────────────────────────────────────────────────
    print("\nFleet:")
    st, body, el = _call(args.api, "/api/fleet/vans")
    vans = body.get("vans") if isinstance(body, dict) else None
    c.record("GET /api/fleet/vans", st == 200 and isinstance(vans, list) and bool(vans),
             f"status={st} {el:.1f}s vans={vans}")

    st, body, el = _call(args.api, "/api/fleet/schedule")
    scheds = body.get("schedules") if isinstance(body, dict) else None
    c.record("GET /api/fleet/schedule", st == 200 and isinstance(scheds, (list, dict)),
             f"status={st} {el:.1f}s n={len(scheds) if scheds is not None else '?'}")

    # ── Real van tracking (new in v3.6) ─────────────────────────────────────
    print("\nReal van (v3.6):")
    st, body, el = _call(args.api, "/api/fleet/real-van/status")
    c.record("GET /api/fleet/real-van/status", st == 200 and isinstance(body, dict)
             and "tracking" in body, f"status={st} {el:.1f}s body={body!r}")

    st, body, el = _call(args.api, "/api/fleet/real-van/history?mins=60")
    c.record("GET /api/fleet/real-van/history", st == 200 and isinstance(body, dict),
             f"status={st} {el:.1f}s")

    # The live feed depends on an external bucket; a 502 means upstream is down,
    # which is not a deploy regression, so report it without failing the run.
    st, body, el = _call(args.api, "/api/fleet/real-van")
    if st == 502:
        c.record("GET /api/fleet/real-van (upstream optional)", True,
                 f"upstream unavailable (502) — not a deploy failure: {body}")
    else:
        c.record("GET /api/fleet/real-van", st == 200 and isinstance(body, dict)
                 and body.get("lat") is not None, f"status={st} {el:.1f}s body={body!r}")

    # ── Anthropic via SSM (the v3.6 key change) ─────────────────────────────
    if args.write:
        print("\nQ&A (verifies SSM-sourced Anthropic key):")
        st, rnd, _ = _call(args.api, "/api/location/random")
        if st != 200 or not isinstance(rnd, dict):
            c.record("POST /api/session/new", False, "could not get a random location")
        else:
            st, sess, el = _call(args.api, "/api/session/new", "POST", {
                "address": rnd["address"], "lat": rnd["lat"], "lon": rnd["lon"],
                "bearing": rnd.get("bearing", 0),
            })
            sid = sess.get("id") if isinstance(sess, dict) else None
            c.record("POST /api/session/new", st == 200 and bool(sid),
                     f"status={st} {el:.1f}s id={sid}")
            if sid:
                st, ans, el = _call(args.api, "/api/ask", "POST", {
                    "session_id": sid,
                    "question": "Are road conditions clear ahead?",
                })
                answer = ans.get("answer") if isinstance(ans, dict) else None
                c.record("POST /api/ask returns an answer",
                         st == 200 and bool(answer),
                         f"status={st} {el:.1f}s "
                         + (f"answer={answer[:70]!r}" if answer else f"body={ans!r}"))
                if isinstance(ans, dict) and ans.get("usage"):
                    u = ans["usage"]
                    print(f"         model={u.get('model')} "
                          f"in={u.get('input_tokens')} out={u.get('output_tokens')}")
    else:
        print("\n(skipping /api/ask — pass --write to verify the Anthropic/SSM path)")

    return 0 if c.summary() else 1


if __name__ == "__main__":
    sys.exit(main())
