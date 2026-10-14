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
        GET  /api/nodes           (names/colors from the Pi's nodes.json; no pins)
        POST /api/poll            ({"node_id": N}: radio nodes through main.py,
        POST /api/ping_test        Wi-Fi nodes through garden-wifi)
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
import threading
import time
import db
import requests

from flask import Flask, jsonify, request
from flask_cors import CORS

import soil_calibration
from links import active_link
from nodes import NodeConfigError, link_plan, load_nodes, public_nodes
from wifi_nodes import ask_wifi_service

from sync_indoor import (
    request_poll,
    wait_for_poll_result,
    sensor_health_report,
    request_set_interval,
    wait_for_interval_ack,
    request_ping_test,
    wait_for_ping_result,
    get_ping_progress,
    get_node_info,
    get_poll_progress,
    poll_progress_failed,
    recorded_wake,
    start_poll_progress,
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
# public. On the Pi they come from /etc/garden-api.env, which
# garden-api.service loads (deploy/install.sh creates it) — do not hardcode
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

app = Flask(__name__)
CORS(app, origins=[ALLOWED_ORIGIN])

_poll_lock = threading.Lock()
_last_poll = {}   # node ID -> {"ts", "result"}

_health_lock = threading.Lock()
_last_health = {"ts": 0.0, "result": None}

_ping_lock = threading.Lock()
_last_ping = {}


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


def is_wifi_node(node_id):
    '''Whether node_id is reached through garden-wifi (wifi_nodes.py) right
    now rather than main.py's radio: a Wi-Fi node, or one with both whose
    Wi-Fi is the link in use (links.py).'''
    try:
        node = load_nodes(skip_invalid=True).get(node_id)
    except NodeConfigError:
        return False
    return node is not None and active_link(node_id, node) == "wifi"


def requested_node():
    body = request.get_json(silent=True) or {}
    try:
        return int(body.get("node_id", 1))
    except (TypeError, ValueError):
        return 1


# Why a Wi-Fi poll came back without an answer, for the dashboard's steps.
WIFI_FAILURES = {
    "offline":     "not connected to the Pi over Wi-Fi",
    "unavailable": "the Pi's Wi-Fi service (garden-wifi) isn't running",
    "timeout":     "no answer over Wi-Fi",
}


@app.route("/api/poll", methods=["POST"])
def api_poll():
    # Poll one node now (body: {"node_id": N, "rid": <the dashboard's id for
    # this Refresh>}) and return what it sent. Each step along the way is
    # written to POLL_PROGRESS_FILE for /api/poll_progress.
    node_id = requested_node()
    body = request.get_json(silent=True) or {}
    request_id = str(body.get("rid", ""))[:40] or None
    now = time.monotonic()
    with _poll_lock:
        state = _last_poll.setdefault(node_id, {"ts": 0.0, "result": None})
        elapsed = now - state["ts"]
        if state["result"] is not None and elapsed < POLL_COOLDOWN:
            return jsonify({
                "status": "cooldown",
                "retry_after": round(POLL_COOLDOWN - elapsed, 1),
                "last_result": state["result"],
            }), 429

        wifi = is_wifi_node(node_id)
        start_poll_progress(node_id, "wifi" if wifi else "radio", request_id)

        wake = recorded_wake(node_id)
        if wake is not None:
            # Asleep: the services don't send it anything until it wakes.
            poll_progress_failed(node_id, f"asleep until {wake.strftime('%H:%M')}")
            return jsonify({"status": "asleep", "until": wake.strftime("%Y-%m-%dT%H:%M:%S"), "packets": []})

        if wifi:
            reply = ask_wifi_service({"t": "poll", "n": node_id}, timeout=30)
            status = reply.get("status", "error")
            if status != "ok":
                poll_progress_failed(node_id, WIFI_FAILURES.get(status, f"Wi-Fi service: {status}"))
            result = {"status": status, "packets": reply.get("packets", [])}
        else:
            try:
                node = load_nodes(skip_invalid=True).get(node_id, {})
            except NodeConfigError:
                node = {}
            request_poll(node_id, link_plan=link_plan(node))
            packets = wait_for_poll_result(timeout=90, node_id=node_id)
            if packets is None:   # (unless main.py already said why)
                poll_progress_failed(node_id, "no answer: is the radio loop (garden-sensor) running?")
            result = {
                "status":  "ok" if packets else ("empty" if packets == [] else "timeout"),
                "packets": packets or [],
            }

        state["ts"]     = time.monotonic()
        state["result"] = result

    return jsonify(result)


@app.route("/api/poll_progress", methods=["GET"])
def api_poll_progress():
    # The Refresh in progress, step by step (sync_indoor.POLL_STEPS): cheap,
    # uncached, polled every ~400 ms by the dashboard while it waits.
    progress = get_poll_progress()
    if progress is None:
        return jsonify({"status": "idle"})
    progress.pop("started", None)
    return jsonify({"status": "running" if not progress.get("end") else "finished", **progress})


@app.route("/api/ping_progress", methods=["GET"])
def api_ping_progress():
    # Cheap, uncached, no cooldown — meant to be polled every ~300ms while a
    # ping test is in flight to show a live "x/y pong" readout.
    progress = get_ping_progress()
    if progress is None:
        return jsonify({"status": "idle"})
    return jsonify({"status": "running", **progress})


@app.route("/api/node_info", methods=["GET"])
def api_node_info():
    # Each node's last storage report (bytes used/free/total and when it was
    # reported) — just a file read, main.py refreshes it hourly.
    return jsonify({"status": "ok", "nodes": get_node_info()})


@app.route("/api/nodes", methods=["GET"])
def api_nodes():
    # How the dashboard shows each node (name, short name, color, battery
    # label, link) — read fresh each time, so a node added with
    # node_setup.py shows up on the next page load. "link" is the one in
    # use right now; "links" all it has, in order of preference.
    try:
        nodes = load_nodes(skip_invalid=True)
    except NodeConfigError as e:
        return jsonify({"status": "error", "error": str(e)}), 500
    shown = public_nodes(nodes)
    for n, node in nodes.items():
        shown[n]["link"] = active_link(n, node)
    return jsonify({"status": "ok", "nodes": {str(n): v for n, v in shown.items()}})


@app.route("/api/ping_test", methods=["POST"])
def api_ping_test():
    node_id = requested_node()

    now = time.monotonic()
    with _ping_lock:
        state = _last_ping.setdefault(node_id, {"ts": 0.0, "result": None})
        elapsed = now - state["ts"]
        if state["result"] is not None and elapsed < PING_COOLDOWN:
            return jsonify({
                "status": "cooldown",
                "retry_after": round(PING_COOLDOWN - elapsed, 1),
                "last_result": state["result"],
            }), 429

        if is_wifi_node(node_id):
            # Over Wi-Fi: the same test, through garden-wifi. "offline" (not
            # connected) or "unavailable" (service down) come back as is.
            response = ask_wifi_service({"t": "ping", "n": node_id, "count": 10}, timeout=60)
        else:
            request_ping_test(node_id=node_id, count=10)
            result = wait_for_ping_result(node_id=node_id, timeout=70)
            response = {"status": "timeout"} if result is None else {"status": "ok", **result}

        state["ts"]     = time.monotonic()
        state["result"] = response

    return jsonify(response)


@app.route("/api/data", methods=["GET"])
def api_data():
    minutes     = request.args.get("minutes", default=15, type=int)
    start       = request.args.get("start", type=str)
    end         = request.args.get("end", type=str)
    sensor_type = request.args.get("sensor_type", type=str)
    node_id     = request.args.get("node_id", type=int)
    # after: the "cursor" from an earlier reply — just the rows added since
    # then, whatever their time (the dashboard's 10 s refresh).
    after       = request.args.get("after", type=int)

    # start/end (or after) override the minutes default if given
    use_minutes = minutes if (start is None and end is None and after is None) else None

    try:
        conn = db.get_connection()
        try:
            cursor = db.latest_rowid(conn)   # before the query, so nothing added meanwhile is skipped next time
            rows = db.query_readings(
                conn,
                minutes=use_minutes,
                start=start,
                end=end,
                sensor_type=sensor_type,
                node_id=node_id,
                after=after,
                upto=cursor if after is not None else None,
            )
            packets = db.pivot_to_packets(rows)
        finally:
            conn.close()
        return jsonify({"status": "ok", "packets": packets, "cursor": cursor})
    except Exception as e:
        print(f"[API] /api/data query failed: {e}")
        return jsonify({"status": "error", "error": str(e)}), 500

_available_fields = db.AvailableFields()


SOIL_CHECK_EVERY = 300   # seconds between automatic soil calibration steps


def _background():
    '''Every few minutes: keep the available-fields list current (the first
    time at startup, so a page load right after a restart doesn't wait for
    its slow scan), start calibrating any new soil sensor, and move the
    automatic calibrations on (soil_calibration.py).'''
    while True:
        try:
            conn = db.get_connection()
            try:
                fields = _available_fields.get(conn)
                soil_calibration.start_new_sensors(fields)
                soil_calibration.update_auto(conn)
            finally:
                conn.close()
        except Exception as e:
            print(f"[API] Background check failed: {e}")
        time.sleep(SOIL_CHECK_EVERY)


threading.Thread(target=_background, daemon=True).start()


@app.route("/api/available_fields", methods=["GET"])
def api_available_fields():
    # Lets the dashboard hide analysis-panel checkboxes for sensor+field
    # combos that have never actually logged data. Kept between requests
    # (db.AvailableFields): the full scan behind it takes ~20 s on the Pi, so
    # it's done once, at startup, and after that only new rows are looked at.
    try:
        conn = db.get_connection()
        try:
            fields = _available_fields.get(conn)
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