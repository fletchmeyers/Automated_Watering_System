'''
Python 3 running on Raspberry Pi 3B

Flask API exposed through the Cloudflare Tunnel so the dashboard (and later,
watering controls) can be reached from outside the local network.

This runs as its OWN process, separate from main.py. It never talks to the
radio directly — it just writes to COMMAND_FILE and polls DATA_FILE the same
way sync_indoor.py's CLI entry point already does. main.py (already running
via garden-sensor.service) is what actually forwards commands over the radio.

Two-tier access model:
  - Public (no login), rate-limited by cooldown, not auth:
        GET  /api/health
        POST /api/poll
  - Gated behind Cloudflare Access (configured in the Zero Trust dashboard,
    not in this file):
        POST /api/set_interval
        POST /api/set_threshold   (stub — no watering logic yet)
        POST /api/water           (stub — no watering hardware yet)

Run for real with gunicorn, not `python3 flask_api.py`:
    gunicorn -w 1 --threads 4 -b 127.0.0.1:5000 flask_api:app

(1 worker, several threads: the poll/interval handlers block for a few
seconds waiting on the Pico, but they're I/O-bound, not CPU-bound, and we
want the in-memory cooldown state shared across requests rather than
sharded across separate worker processes.)
'''

import subprocess
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, request
from flask_cors import CORS

from sync_indoor import (
    request_poll,
    wait_for_new_data,
    sensor_health_report,
    request_set_interval,
    wait_for_interval_ack,
)

# ── Config ────────────────────────────────────────────────────────────────────

# Restrict CORS to the actual dashboard origin — GitHub Pages serves this repo.
ALLOWED_ORIGIN = "https://fletchmeyers.github.io"

POLL_COOLDOWN   = 30   # seconds between allowed manual polls
HEALTH_COOLDOWN = 15   # seconds between allowed health report recomputes

# Path to push_data.sh so a manual poll can publish immediately instead of
# waiting for the next 5-minute cron tick.
PUSH_SCRIPT = Path(__file__).parent.parent / "push_data.sh"

app = Flask(__name__)
CORS(app, origins=[ALLOWED_ORIGIN])

_poll_lock = threading.Lock()
_last_poll = {"ts": 0.0, "result": None}

_health_lock = threading.Lock()
_last_health = {"ts": 0.0, "result": None}


# ── Public, cooldown-limited endpoints ───────────────────────────────────────

@app.route("/api/health", methods=["GET"])
def api_health():
    now = time.monotonic()
    with _health_lock:
        if _last_health["result"] is not None and now - _last_health["ts"] < HEALTH_COOLDOWN:
            return jsonify({**_last_health["result"], "cached": True})

        report = sensor_health_report()
        _last_health["ts"] = now
        _last_health["result"] = report

    return jsonify({**report, "cached": False})


@app.route("/api/poll", methods=["POST"])
def api_poll():
    now = time.monotonic()
    with _poll_lock:
        elapsed = now - _last_poll["ts"]
        if _last_poll["result"] is not None and elapsed < POLL_COOLDOWN:
            return jsonify({
                "status": "cooldown",
                "retry_after": round(POLL_COOLDOWN - elapsed, 1),
                "last_result": _last_poll["result"],
            }), 429

        request_poll()
        got_data = wait_for_new_data(timeout=90)
        result = {"status": "ok" if got_data else "timeout"}

        # Publish immediately rather than waiting for the next cron tick,
        # so a manual refresh actually shows fresh data promptly.
        if got_data and PUSH_SCRIPT.exists():
            try:
                subprocess.run(["bash", str(PUSH_SCRIPT)], timeout=30, check=False)
            except Exception as e:
                print(f"[API] push_data.sh failed to run: {e}")

        _last_poll["ts"]     = time.monotonic()
        _last_poll["result"] = result

    return jsonify(result)


# ── Gated endpoints (Cloudflare Access enforces login before these are hit) ──

@app.route("/api/set_interval", methods=["POST"])
def api_set_interval():
    body = request.get_json(silent=True) or {}
    seconds = body.get("seconds")
    if not isinstance(seconds, int) or seconds <= 0:
        return jsonify({"error": "seconds must be a positive integer"}), 400

    request_set_interval(seconds)
    acked = wait_for_interval_ack(timeout=90)
    return jsonify({"status": "ok" if acked else "timeout", "seconds": seconds})


@app.route("/api/set_threshold", methods=["POST"])
def api_set_threshold():
    # No watering/threshold logic exists yet — this is wired up as a stub
    # so the dashboard and Access policy can be built against it now.
    return jsonify({"error": "not implemented — no watering logic yet"}), 501


@app.route("/api/water", methods=["POST"])
def api_water():
    # No watering hardware exists yet.
    return jsonify({"error": "not implemented — no watering hardware yet"}), 501


if __name__ == "__main__":
    # Dev-only entry point. Use gunicorn (see module docstring) for real use.
    app.run(host="127.0.0.1", port=5000)