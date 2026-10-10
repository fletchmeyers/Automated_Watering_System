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

The value is the median of the sensor's readings in sensors.db over the
window, so a few wild ones don't move it. Take "dry" while the soil is due
for watering, and "wet" 30-60 minutes after watering, once the water has
soaked in. Nothing is saved until you say yes to the numbers shown.

Written by Fletcher Meyers
October 2026
'''

import sqlite3
import statistics
from datetime import datetime, timedelta

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
    '''Set (or with point "clear", remove) one end of a soil sensor's scale.
    Returns True if nodes.json was changed.'''
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
    if not wizard.yes(f"Save this for node {node_id} {sensor}?", default=True):
        return False
    data["nodes"][str(node_id)] = changed
    save_node_file(data, path)
    wizard.say("[SOIL] Saved. The dashboard uses it from its next page load.")
    return True
