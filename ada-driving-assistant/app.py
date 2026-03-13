"""
ADA Driving Assistant – Flask web dashboard.
Polls local data files and shows Claude-generated advisories.
"""

import os
import json
import time
import threading
from dotenv import load_dotenv
from flask import Flask, render_template, jsonify
from assistant import get_advisory

load_dotenv()

app = Flask(__name__)

# Paths to data files produced by the main pipeline
POSITION_PATH = os.environ.get("POSITION_PATH", "../van_position.json")
DETECTIONS_PATH = os.environ.get("DETECTIONS_PATH", "../models/experiments/latest_detections.json")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "5"))  # seconds

# Shared state updated by background thread
_state = {
    "advisory": "Initializing…",
    "position": {},
    "detections": [],
    "last_updated": None,
    "error": None,
}
_lock = threading.Lock()


def load_data():
    position = {}
    detections = []

    if os.path.exists(POSITION_PATH):
        with open(POSITION_PATH) as f:
            position = json.load(f)

    if os.path.exists(DETECTIONS_PATH):
        with open(DETECTIONS_PATH) as f:
            detections = json.load(f)

    return position, detections


def poll_loop():
    while True:
        try:
            position, detections = load_data()
            advisory = get_advisory(position, detections)
            with _lock:
                _state["advisory"] = advisory
                _state["position"] = position
                _state["detections"] = detections
                _state["last_updated"] = time.strftime("%H:%M:%S")
                _state["error"] = None
        except Exception as e:
            with _lock:
                _state["error"] = str(e)
                _state["last_updated"] = time.strftime("%H:%M:%S")
        time.sleep(POLL_INTERVAL)


@app.route("/")
def index():
    return render_template("index.html", poll_interval=POLL_INTERVAL)


@app.route("/api/state")
def api_state():
    with _lock:
        return jsonify(dict(_state))


if __name__ == "__main__":
    # Start background polling thread
    t = threading.Thread(target=poll_loop, daemon=True)
    t.start()
    app.run(debug=False, host="0.0.0.0", port=5001)
