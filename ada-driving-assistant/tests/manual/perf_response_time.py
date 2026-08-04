"""
perf_response_time.py — End-to-end response time measurement for /api/ask.

For each trial:
  1. Call /api/location/random to get a real Berkeley-area address.
  2. POST /api/session/new to create a session.
  3. POST /api/ask with a route-conditions question and time the round-trip.

Usage:
    py tests/perf_response_time.py [--trials N] [--api URL]

Default: 5 trials against the deployed prod API.
"""

import argparse
import json
import statistics
import time
import urllib.error
import urllib.request

DEFAULT_API = "https://27gaq0ssh5.execute-api.us-west-2.amazonaws.com/prod"

QUESTIONS = [
    "Any events or obstacles along my route?",
    "What hazards are ahead?",
    "Is the road clear?",
    "Any construction or accidents on my route?",
    "What should I watch out for?",
]


def _request(api_base, method, path, body=None):
    url  = api_base + path
    data = json.dumps(body).encode() if body else None
    req  = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def run_trial(api_base, n):
    print(f"\n-- Trial {n} {'-' * 50}")

    # 1. Random location
    try:
        loc = _request(api_base, "GET", "/api/location/random")
    except Exception as e:
        print(f"  [FAIL] /api/location/random: {e}")
        return None
    print(f"  Location : {loc.get('address', '?')}")
    print(f"             lat={loc.get('lat', 0):.5f}  lon={loc.get('lon', 0):.5f}")

    # 2. Create session
    try:
        session = _request(api_base, "POST", "/api/session/new", {
            "address": loc["address"],
            "lat":     loc["lat"],
            "lon":     loc["lon"],
            "bearing": loc.get("bearing", 0),
            "street":  loc.get("street", ""),
        })
    except Exception as e:
        print(f"  [FAIL] /api/session/new: {e}")
        return None
    session_id = session.get("id")
    print(f"  Session  : {session_id}")

    # 3. Ask question and time it
    question = QUESTIONS[(n - 1) % len(QUESTIONS)]
    print(f"  Question : \"{question}\"")

    t0 = time.perf_counter()
    try:
        result = _request(api_base, "POST", "/api/ask", {
            "session_id":      session_id,
            "question":        question,
            "current_lat":     loc["lat"],
            "current_lon":     loc["lon"],
            "current_bearing": loc.get("bearing", 0),
        })
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print(f"  [FAIL] /api/ask HTTP {e.code}: {body[:200]}")
        return None
    except Exception as e:
        print(f"  [FAIL] /api/ask: {e}")
        return None
    elapsed = time.perf_counter() - t0

    if "error" in result:
        print(f"  [ERROR] {result['error']}")
        return None

    answer  = result.get("answer", "(no answer)")
    usage   = result.get("usage", {})
    model   = usage.get("model", "?")
    in_tok  = usage.get("input_tokens", "?")
    out_tok = usage.get("output_tokens", "?")
    nearby  = result.get("nearby_count", "?")

    safe = answer.encode("ascii", errors="replace").decode("ascii")
    print(f"  Answer   : {safe[:120]}{'...' if len(safe) > 120 else ''}")
    print(f"  Model    : {model}  |  tokens in={in_tok} out={out_tok}  |  nearby={nearby}")
    print(f"  Time: {elapsed:.2f}s")

    return elapsed


def main():
    parser = argparse.ArgumentParser(description="Measure ADA /api/ask response times")
    parser.add_argument("--trials", type=int, default=5,  help="Number of trials (default: 5)")
    parser.add_argument("--api",    default=DEFAULT_API,  help="API base URL")
    args = parser.parse_args()

    api_base = args.api.rstrip("/")
    print(f"ADA response-time benchmark")
    print(f"API    : {api_base}")
    print(f"Trials : {args.trials}")

    times = []
    for i in range(1, args.trials + 1):
        t = run_trial(api_base, i)
        if t is not None:
            times.append(t)

    print(f"\n{'=' * 60}")
    print(f"Results  {len(times)}/{args.trials} successful")
    if times:
        print(f"  Min    : {min(times):.2f}s")
        print(f"  Max    : {max(times):.2f}s")
        print(f"  Mean   : {statistics.mean(times):.2f}s")
        if len(times) > 1:
            print(f"  Median : {statistics.median(times):.2f}s")
            print(f"  Stdev  : {statistics.stdev(times):.2f}s")
    else:
        print("  No successful trials.")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
