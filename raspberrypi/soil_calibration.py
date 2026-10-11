'''
Python 3 running on Raspberry Pi 3B

Calibrating a soil sensor: the raw reading it gives in dry soil and in
just-watered soil, which the dashboard then shows as 0% and 100%. Saved in
the Pi's nodes.json as "soil": {"s1": {"dry": 940, "wet": 610}}.

    python3 node_setup.py calibrate 3 s1 dry            — from the last hour's readings
    python3 node_setup.py calibrate 3 s1 wet --minutes 20
    python3 node_setup.py calibrate 3 s1 dry --until 18:30   — the hour before 18:30
    python3 node_setup.py calibrate 3 s1 dry --value 940     — a number you already know
    python3 node_setup.py calibrate 3 s1 clear          — back to the usual scale
    python3 node_setup.py calibrate 3 s1 auto           — let the Pi do it (below)

The value is the median of the sensor's readings in sensors.db over the
window, so a few wild ones don't move it. Take "dry" while the soil is due
for watering, and "wet" 30-60 minutes after watering, once the water has
soaked in. Nothing is saved until you say yes to the numbers shown.

Automatic calibration: put the sensor in dry soil, and after a few minutes
water it thoroughly. The Pi (garden-api, every few minutes: update_auto)
sees the watering as a jump in the readings: dry is the median just before
it, and wet the median from 30 to 60 minutes after it, once the water has
soaked in. It starts by itself for a soil sensor the Pi hasn't seen before
(start_new_sensors), on a new node or a new seesaw on an existing one.
Meanwhile the dashboard shows the sensor as calibrating.

Written by Fletcher Meyers
October 2026
'''

import json
import sqlite3
import statistics
from datetime import datetime, timedelta
from pathlib import Path

from nodes import NodeConfigError, SOIL_SENSOR, check_node, read_node_file, save_node_file

ISO_FORMAT = "%Y-%m-%dT%H:%M:%S"
SATURATED  = 1010   # the seesaw sensor tops out around 1015-1023: wetter soil can't read higher
NOISY      = 60     # readings this far apart (10th to 90th percentile) are worth a warning
MIN_READINGS = 5


def parse_until(text, now=None):
    '''"HH:MM" (today, or yesterday if that's still to come) or a full
    YYYY-MM-DDTHH:MM[:SS]; None means now.'''
    now = now or datetime.now()
    if not text:
        return now
    try:
        hm = datetime.strptime(text, "%H:%M")
        at = now.replace(hour=hm.hour, minute=hm.minute, second=0, microsecond=0)
        return at - timedelta(days=1) if at > now else at
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        raise NodeConfigError(f'--until "{text}" should look like 18:30 or 2026-10-10T18:30')


def readings(db_file, node_id, sensor, start, end):
    '''The sensor's moisture readings from start to end, oldest first.'''
    conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True, timeout=10)
    try:
        return [v for (v,) in conn.execute(
            "SELECT value FROM readings WHERE node_id = ? AND sensor_type = ? AND key = 'm' "
            "AND ts > ? AND ts <= ? ORDER BY ts",
            (node_id, sensor, start.strftime(ISO_FORMAT), end.strftime(ISO_FORMAT)))]
    finally:
        conn.close()


def summary(values):
    '''Median, the 10th-90th percentile spread and the typical jump between readings.'''
    ordered = sorted(values)
    def pct(p):
        return ordered[min(len(ordered) - 1, round(p * (len(ordered) - 1)))]
    jumps = [abs(b - a) for a, b in zip(values, values[1:])]
    return {"count": len(values), "median": round(statistics.median(values)),
            "low": round(pct(0.1)), "high": round(pct(0.9)),
            "jump": round(statistics.median(jumps)) if jumps else 0}


def calibrate(path, node_id, sensor, point, wizard, db_file, minutes=60, until=None, value=None):
    '''Set (or with point "clear", remove) one end of a soil sensor's scale,
    or with point "auto" start calibrating it automatically. Returns True if
    nodes.json was changed.'''
    if not SOIL_SENSOR.match(sensor):
        raise NodeConfigError(f'Soil sensors are named s0, s1, s2 (as on the dashboard), not "{sensor}".')
    data = read_node_file(path)
    node = data["nodes"].get(str(node_id))
    if node is None:
        raise NodeConfigError(f"Node {node_id} isn't in {path}.")
    soil = dict(node.get("soil") or {})
    points = dict(soil.get(sensor) or {})

    if point == "clear":
        if sensor not in soil:
            wizard.say(f"[SOIL] Node {node_id} {sensor} isn't calibrated; nothing to clear.")
            return False
        del soil[sensor]
    elif point == "auto":
        soil[sensor] = {"auto": {"since": datetime.now().strftime(ISO_FORMAT), "watered": None}}
        wizard.say(f"[SOIL] Node {node_id} {sensor} will calibrate itself: with it in dry soil, wait "
                   "a few minutes, then water thoroughly. About an hour later the Pi saves the dry "
                   "and wet readings; meanwhile the dashboard shows it as calibrating.")
    else:
        if value is None:
            end = parse_until(until)
            start = end - timedelta(minutes=minutes)
            values = readings(db_file, node_id, sensor, start, end)
            if len(values) < MIN_READINGS:
                raise NodeConfigError(
                    f"Node {node_id} {sensor} has {len(values)} moisture reading(s) from "
                    f"{start:%H:%M} to {end:%H:%M} on {end:%b %d}; at least {MIN_READINGS} are needed. "
                    "Pick another window with --until/--minutes, or give the number with --value.")
            s = summary(values)
            wizard.say(f"[SOIL] Node {node_id} {sensor}, {start:%b %d %H:%M} to {end:%H:%M}: "
                       f"{s['count']} readings, median {s['median']} "
                       f"(most between {s['low']} and {s['high']}; typical jump {s['jump']}).")
            if s["high"] - s["low"] > NOISY:
                wizard.say("[SOIL] They're spread out: the soil may have been changing (still soaking "
                           "in?), or the sensor was noisy. A shorter or later window may be better.")
            if s["median"] >= SATURATED:
                wizard.say(f"[SOIL] That's at the sensor's top ({SATURATED}+), so it can't tell this "
                           "from anything wetter.")
            value = s["median"]
        points[point] = int(value)
        soil[sensor] = points
        other = "wet" if point == "dry" else "dry"
        wizard.say(f"[SOIL] {point} = {value}" + (f", {other} = {points[other]}" if other in points
                   else f"; {other} isn't set yet (the dashboard's usual scale fills in)."))

    changed = {**node, "soil": soil} if soil else {k: v for k, v in node.items() if k != "soil"}
    check_node(node_id, changed)
    if point != "auto" and not wizard.yes(f"Save this for node {node_id} {sensor}?", default=True):
        return False
    data["nodes"][str(node_id)] = changed
    save_node_file(data, path)
    wizard.say("[SOIL] Saved. The dashboard uses it from its next page load.")
    return True


# ── Automatic calibration ────────────────────────────────────────────────────

JUMP       = 60   # the readings rising this much at once means it was watered
BEFORE     = 5    # readings before a possible watering to compare with (dry)
AFTER      = 3    # readings after it that must all be up
SOAK_FROM  = timedelta(minutes=30)   # wet: the median from here...
SOAK_UNTIL = timedelta(minutes=60)   # ...to here after the watering
SEEN_FILE  = Path(__file__).parent / "soil_sensors_seen.json"   # the Pi's own; not in git


def auto_progress(readings, now):
    """Where an automatic calibration has got to, from the sensor's readings
    since it started ([(datetime, raw)], oldest first): {"state": "waiting"}
    for a watering, {"state": "soaking", "watered", "dry"}, or
    {"state": "done", "watered", "dry", "wet"}."""
    values = [v for _, v in readings]
    for i in range(3, len(values) - AFTER + 1):
        before = statistics.median(values[max(0, i - BEFORE):i])
        if min(values[i:i + AFTER]) - before < JUMP:
            continue
        watered = readings[i][0]
        dry = round(before)
        after = [(t, v) for t, v in readings if t >= watered + SOAK_FROM]
        wet = [v for t, v in after if t <= watered + SOAK_UNTIL]
        if now < watered + SOAK_UNTIL:
            return {"state": "soaking", "watered": watered, "dry": dry}
        if len(wet) < 3:
            wet = [v for _, v in after]      # the node missed polls: take what came after
        if len(wet) < 3:
            return {"state": "soaking", "watered": watered, "dry": dry}
        return {"state": "done", "watered": watered, "dry": dry, "wet": round(statistics.median(wet))}
    return {"state": "waiting"}


def _series(conn, node_id, sensor, since, until):
    rows = conn.execute(
        "SELECT ts, value FROM readings WHERE node_id = ? AND sensor_type = ? AND key = 'm' "
        "AND ts >= ? AND ts <= ? ORDER BY ts",
        (node_id, sensor, since, until.strftime(ISO_FORMAT))).fetchall()
    out = []
    for ts, value in rows:
        try:
            out.append((datetime.strptime(ts, ISO_FORMAT), value))
        except (TypeError, ValueError):
            pass
    return out


def update_auto(conn, path=None, now=None, say=print):
    """Move every automatic calibration on: note when its sensor was
    watered, and once the water has soaked in save its dry and wet readings.
    Returns the (node, sensor) pairs that finished."""
    now = now or datetime.now()
    data = read_node_file(path)
    finished, changed = [], False
    for node_key, node in data["nodes"].items():
        for sensor, points in (node.get("soil") or {}).items():
            auto = points.get("auto") if isinstance(points, dict) else None
            if not auto:
                continue
            progress = auto_progress(_series(conn, int(node_key), sensor, auto["since"], now), now)
            if progress["state"] == "waiting":
                continue
            if progress["state"] == "soaking":
                watered = progress["watered"].strftime(ISO_FORMAT)
                if auto.get("watered") != watered:
                    auto["watered"] = watered
                    changed = True
                    say(f"[SOIL] Node {node_key} {sensor} was watered at {watered[11:16]} "
                        f"(dry reading {progress['dry']}); its wet reading comes an hour later.")
                continue
            if progress["wet"] - progress["dry"] < JUMP // 2:
                # It dried out again before soaking in: wait for another watering.
                auto["since"], auto["watered"] = now.strftime(ISO_FORMAT), None
                changed = True
                say(f"[SOIL] Node {node_key} {sensor}: the watering didn't stay in the soil "
                    f"(dry {progress['dry']}, an hour later {progress['wet']}); waiting for another.")
                continue
            node["soil"][sensor] = {"dry": progress["dry"], "wet": progress["wet"]}
            finished.append((int(node_key), sensor))
            changed = True
            say(f"[SOIL] Node {node_key} {sensor} is calibrated: dry {progress['dry']}, "
                f"wet {progress['wet']}.")
    if changed:
        save_node_file(data, path)
    return finished


def start_new_sensors(fields, path=None, seen_file=SEEN_FILE, now=None, say=print):
    """Start an automatic calibration for each soil sensor in fields (from
    db.AvailableFields) the Pi hasn't seen before, if its node is in the list
    and it isn't calibrated. The first time (no seen_file yet), every sensor
    there is already counts as seen: nothing starts for sensors already in
    the ground. Returns the (node, sensor) pairs started."""
    sensors = sorted({(f["node_id"], f["sensor_type"]) for f in fields
                      if f["key"] == "m" and SOIL_SENSOR.match(str(f["sensor_type"]))})
    seen_file = Path(seen_file)
    try:
        seen = {tuple(s) for s in json.loads(seen_file.read_text())}
    except (OSError, ValueError):
        seen = None
    started = []
    if seen is not None:
        new = [s for s in sensors if s not in seen]
        if new:
            data = read_node_file(path)
            stamp = (now or datetime.now()).strftime(ISO_FORMAT)
            for node_id, sensor in new:
                node = data["nodes"].get(str(node_id))
                if node is None or sensor in (node.get("soil") or {}):
                    continue
                node.setdefault("soil", {})[sensor] = {"auto": {"since": stamp, "watered": None}}
                started.append((node_id, sensor))
                say(f"[SOIL] New soil sensor: node {node_id} {sensor}. Calibrating it: water it "
                    "thoroughly once it has been in its dry soil a few minutes.")
            if started:
                save_node_file(data, path)
    if seen is None or set(sensors) - seen:
        try:
            seen_file.write_text(json.dumps(sorted(set(sensors) | (seen or set()))))
        except OSError as e:
            say(f"[SOIL] Could not write {seen_file}: {e}")
    return started
