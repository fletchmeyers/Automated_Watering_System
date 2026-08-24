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

import os
import subprocess
import threading
import time
from pathlib import Path
import db
import requests

from flask import Flask, jsonify, request
from flask_cors import CORS

from sync_indoor import (
    request_poll,
    wait_for_poll_result,
    sensor_health_report,
    request_set_interval,
    wait_for_interval_ack,
    request_ping_test,
    wait_for_ping_result,
    get_ping_progress,
)

# ── Config ────────────────────────────────────────────────────────────────────

# Restrict CORS to the actual dashboard origin — GitHub Pages serves this repo.
ALLOWED_ORIGIN = "https://fletchmeyers.github.io"

POLL_COOLDOWN   = 30   # seconds between allowed manual polls
HEALTH_COOLDOWN = 15   # seconds between allowed health report recomputes
PING_COOLDOWN   = 10   # seconds between allowed ping tests — short, since a
                        # test itself only takes a few seconds, but still
                        # enough to stop rapid button-mashing from repeatedly
                        # tying up main.py's loop back-to-back

# Garden location, same coordinates weather.js uses — kept independently
# rather than shared from a config file since this is the only other place
# that needs them.
GARDEN_LAT = 40.4406
GARDEN_LON = -79.9959

# Weather API keys — kept server-side and never shipped to the browser.
# This is the whole reason this proxy exists: the dashboard is a static
# site with no backend of its own, so any key baked into its JS would be
# public. Set these as real environment variables on the Pi (e.g. in the
# systemd unit or a .env loaded before gunicorn starts) — do not hardcode
# a key here. A source with no key set returns 501 rather than crashing,
# so the dashboard can show "not configured yet" instead of a raw error.
WEATHER_API_KEYS = {
    "weatherapi":     os.environ.get("WEATHERAPI_KEY"),
    "openweathermap": os.environ.get("OPENWEATHERMAP_KEY"),
    "tomorrowio":      os.environ.get("TOMORROWIO_KEY"),
}

# Shared cache, one slot per source — a forecast doesn't need to be
# per-visitor fresh, and every provider here has a free-tier call cap, so
# every dashboard viewer within this window gets the same cached response
# instead of triggering their own upstream call.
WEATHER_CACHE_TTL = 600  # 10 minutes
_weather_lock  = threading.Lock()
_weather_cache = {}  # source -> {"ts": monotonic, "result": dict}

# Path to push_data.sh so a manual poll can publish immediately instead of
# waiting for the next 5-minute cron tick.
PUSH_SCRIPT = Path(__file__).parent.parent / "push_data.sh"

app = Flask(__name__)
CORS(app, origins=[ALLOWED_ORIGIN])

_poll_lock = threading.Lock()
_last_poll = {"ts": 0.0, "result": None}

_health_lock = threading.Lock()
_last_health = {"ts": 0.0, "result": None}

_ping_lock = threading.Lock()
_last_ping = {"ts": 0.0, "result": None}


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
        packets = wait_for_poll_result(timeout=90)
        result = {
            "status":  "ok" if packets else "timeout",
            "packets": packets or [],
        }

        # Publish to GitHub in the background — this benefits anyone loading
        # the dashboard passively and feeds the archive, but the response to
        # this click no longer waits on git/GitHub/the Pages CDN at all, since
        # the actual sensor values are already in `result` above.
        if packets and PUSH_SCRIPT.exists():
            def _publish():
                try:
                    result = subprocess.run(
                        ["bash", str(PUSH_SCRIPT)],
                        timeout=30, check=False,
                        capture_output=True, text=True,
                    )
                    # check=False means a non-zero exit doesn't raise, so it
                    # has to be checked explicitly — previously a failing
                    # push_data.sh (bad git state, network hiccup, etc.) just
                    # went silent since only a failure to *spawn* the process
                    # hit the except block below.
                    if result.returncode != 0:
                        detail = (result.stderr or result.stdout).strip()
                        print(f"[API] push_data.sh exited {result.returncode}: {detail}")
                except Exception as e:
                    print(f"[API] push_data.sh failed to run: {e}")
            threading.Thread(target=_publish, daemon=True).start()

        _last_poll["ts"]     = time.monotonic()
        _last_poll["result"] = result

    return jsonify(result)


@app.route("/api/ping_progress", methods=["GET"])
def api_ping_progress():
    # Cheap, uncached, no cooldown — meant to be polled every ~300ms while a
    # ping test is in flight to show a live "x/y pong" readout.
    progress = get_ping_progress()
    if progress is None:
        return jsonify({"status": "idle"})
    return jsonify({"status": "running", **progress})


@app.route("/api/ping_test", methods=["POST"])
def api_ping_test():
    now = time.monotonic()
    with _ping_lock:
        elapsed = now - _last_ping["ts"]
        if _last_ping["result"] is not None and elapsed < PING_COOLDOWN:
            return jsonify({
                "status": "cooldown",
                "retry_after": round(PING_COOLDOWN - elapsed, 1),
                "last_result": _last_ping["result"],
            }), 429

        request_ping_test(count=10)
        result = wait_for_ping_result(timeout=20)

        if result is None:
            response = {"status": "timeout"}
        else:
            response = {"status": "ok", **result}

        _last_ping["ts"]     = time.monotonic()
        _last_ping["result"] = response

    return jsonify(response)


@app.route("/api/data", methods=["GET"])
def api_data():
    minutes     = request.args.get("minutes", default=15, type=int)
    start       = request.args.get("start", type=str)
    end         = request.args.get("end", type=str)
    sensor_type = request.args.get("sensor_type", type=str)
    node_id     = request.args.get("node_id", type=int)

    # start/end override the minutes default if either is explicitly given
    use_minutes = minutes if (start is None and end is None) else None

    try:
        conn = db.get_connection()
        try:
            rows = db.query_readings(
                conn,
                minutes=use_minutes,
                start=start,
                end=end,
                sensor_type=sensor_type,
                node_id=node_id,
            )
            packets = db.pivot_to_packets(rows)
        finally:
            conn.close()
        return jsonify({"status": "ok", "packets": packets})
    except Exception as e:
        print(f"[API] /api/data query failed: {e}")
        return jsonify({"status": "error", "error": str(e)}), 500

@app.route("/api/available_fields", methods=["GET"])
def api_available_fields():
    # Cheap SELECT DISTINCT, no cooldown needed (same reasoning as /api/data:
    # it's a read against a local SQLite file, not something that hits the
    # radio or blocks on the Pico). Lets the dashboard hide analysis-panel
    # checkboxes for sensor+field combos that have never actually logged data.
    try:
        conn = db.get_connection()
        try:
            fields = db.get_available_fields(conn)
        finally:
            conn.close()
        return jsonify({"status": "ok", "fields": fields})
    except Exception as e:
        print(f"[API] /api/available_fields query failed: {e}")
        return jsonify({"status": "error", "error": str(e)}), 500

@app.route("/api/weather/<source>", methods=["GET"])
def api_weather(source):
    '''
    Thin server-side proxy for weather sources that require an API key.
    The dashboard's weather.js calls this instead of the third-party API
    directly, so the key never appears in anything shipped to the browser.
    Returns each upstream API's response basically as-is (or, for
    OpenWeatherMap, both calls it needs bundled together) — the parsing
    and normalizing into the dashboard's common shape stays in weather.js
    alongside the key-less Open-Meteo/NWS adapters, so this endpoint
    doesn't need to change every time a source's response format does.
    '''
    if source not in WEATHER_API_KEYS:
        return jsonify({"error": f"unknown weather source: {source}"}), 404

    key = WEATHER_API_KEYS[source]
    if not key:
        return jsonify({"error": f"no API key configured on the server for {source}"}), 501

    now = time.monotonic()
    with _weather_lock:
        cached = _weather_cache.get(source)
        if cached and now - cached["ts"] < WEATHER_CACHE_TTL:
            return jsonify(cached["result"])

    try:
        if source == "weatherapi":
            resp = requests.get(
                "https://api.weatherapi.com/v1/forecast.json",
                params={"q": f"{GARDEN_LAT},{GARDEN_LON}", "days": 3, "key": key},
                timeout=10,
            )
            resp.raise_for_status()
            result = resp.json()

        elif source == "openweathermap":
            # Free tier only exposes "current weather" and a separate
            # 3-hour/5-day forecast as two distinct calls — bundle both
            # into one response so the dashboard only makes one request.
            cur_resp = requests.get(
                "https://api.openweathermap.org/data/2.5/weather",
                params={"lat": GARDEN_LAT, "lon": GARDEN_LON, "units": "metric", "appid": key},
                timeout=10,
            )
            fc_resp = requests.get(
                "https://api.openweathermap.org/data/2.5/forecast",
                params={"lat": GARDEN_LAT, "lon": GARDEN_LON, "units": "metric", "appid": key},
                timeout=10,
            )
            cur_resp.raise_for_status()
            fc_resp.raise_for_status()
            result = {"current": cur_resp.json(), "forecast": fc_resp.json()}

        elif source == "tomorrowio":
            resp = requests.get(
                "https://api.tomorrow.io/v4/weather/forecast",
                params={
                    "location": f"{GARDEN_LAT},{GARDEN_LON}",
                    "units": "metric",
                    "timesteps": "1h,1d",
                    "apikey": key,
                },
                timeout=10,
            )
            resp.raise_for_status()
            result = resp.json()

        else:
            # Shouldn't happen — every key in WEATHER_API_KEYS is handled
            # above — but fail loudly rather than silently if it drifts.
            return jsonify({"error": f"source {source} has a key slot but no handler"}), 500

    except requests.RequestException as e:
        print(f"[WEATHER] {source} request failed: {e}")
        return jsonify({"error": f"upstream request to {source} failed: {e}"}), 502

    with _weather_lock:
        _weather_cache[source] = {"ts": time.monotonic(), "result": result}

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