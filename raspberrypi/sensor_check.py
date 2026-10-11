'''
Python 3 running on Raspberry Pi 3B

A hardware check for a node plugged into the Pi by USB: poll it a few
times, list every sensor that answered with its readings, and say which
look wrong (out of range, stuck, not answering, or jumping about). Soil
sensors that aren't calibrated yet can then have their dry or wet reading
taken from live readings (see soil_calibration.py).

node_setup.py runs it after setting a board up, or on its own:

    python3 node_setup.py check

To check another kind of sensor, add a row to CHECKS.

Written by Fletcher Meyers
October 2026
'''

import re
import statistics
import time
from datetime import datetime

import soil_calibration
from nodes import read_node_file

# What a sensor's readings should look like: its "t" tag (a regex) -> what
# it is, and {key: (lowest, highest) plausible value}. Keys not listed are
# shown but not checked.
CHECKS = [
    (r"s\d",  "soil moisture (seesaw)", {"m": (300, 1009), "tmp": (-10, 50)}),
    (r"sht",  "temperature/humidity (SHT4x)", {"tmp": (-20, 60), "rh": (0, 100)}),
    (r"uv",   "light/UV (LTR390)", {"lux": (0, 150000), "uvi": (0, 15)}),
    (r"voc",  "air quality (SGP40)", {"voc": (1, 65534)}),
    (r"batt", "battery gauge (MAX17048)", {"v": (3.0, 4.35), "soc": (0, 110)}),
    (r"vbat", "battery voltage", {"v": (3.0, 4.35)}),
    (r"pw\d", "power monitor (INA238)", {"v": (0, 36), "ma": (-10000, 10000)}),
    (r"rt",   "radio temperature", {"tmp": (-20, 80)}),
]
SOIL_TOP = 1010       # at or above: the seesaw's top, wetter soil can't read higher
SOIL_AIR = 300        # below: about what it reads in air
SOIL_JUMPY = 30       # readings this far apart over a few seconds: noisy power?
NO_ANSWER = 65535     # what the seesaw library returns when the sensor doesn't answer
META = {"t", "q", "n", "ts"}


def check_for(sensor):
    return next(((what, limits) for pattern, what, limits in CHECKS if re.fullmatch(pattern, sensor)),
                ("sensor", {}))


def poll_usb(usb_node, node_id, timeout=5):
    '''One poll over USB: {sensor tag: packet}, or None if it didn't answer.'''
    usb_node.send({"t": "poll", "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), "n": node_id})
    got = {}
    for packet in usb_node.packets(timeout):
        kind = packet.get("t")
        if kind == "batch_end":
            return got
        if kind not in ("ts", "hello") and "q" in packet:
            got[kind] = packet
    return got or None


def take_readings(usb_node, node_id, count, gap, sleep=time.sleep):
    '''count polls, gap seconds apart (the node only reads its sensors every
    sense interval): {sensor tag: [packet, ...]}.'''
    readings = {}
    for i in range(count):
        if i:
            sleep(gap)
        for sensor, packet in (poll_usb(usb_node, node_id) or {}).items():
            readings.setdefault(sensor, []).append(packet)
    return readings


def problems(sensor, packets):
    '''What looks wrong with one sensor's readings (empty: looks fine).'''
    _, limits = check_for(sensor)
    found = []
    for key, (low, high) in limits.items():
        values = [p[key] for p in packets if isinstance(p.get(key), (int, float))]
        if not values:
            found.append(f"no {key} reading")
            continue
        if sensor.startswith("s") and key == "m":
            found += soil_problems(values)
            continue
        if any(not low <= v <= high for v in values):
            found.append(f"{key} {_fmt(min(values))}–{_fmt(max(values))} is outside {low}–{high}")
    return found


def soil_problems(values):
    middle = statistics.median(values)
    if any(v >= NO_ANSWER for v in values):
        return ["the sensor stopped answering (65535): check its cable"]
    if middle >= SOIL_TOP:
        return [f"at the top of its range ({_fmt(middle)}): in water, or soil wetter than it can "
                "tell apart"]
    if middle < SOIL_AIR:
        return [f"very low ({_fmt(middle)}): is it pushed into the soil? (it reads about 200-300 in air)"]
    if max(values) - min(values) > SOIL_JUMPY:
        return [f"jumping about ({_fmt(min(values))}–{_fmt(max(values))} in a few seconds): "
                "a noisy power supply, or a loose cable?"]
    return []


def _fmt(v):
    return f"{v:g}" if isinstance(v, (int, float)) else str(v)


def report(readings, say=print):
    '''Print one line per sensor; return {sensor: [problems]}.'''
    found = {}
    if not readings:
        say("[CHECK] No sensors answered. Check the I2C/STEMMA QT cables (and that the "
            "board powers its STEMMA QT port).")
        return found
    for sensor in sorted(readings):
        packets = readings[sensor]
        what, _ = check_for(sensor)
        keys = [k for k in packets[-1] if k not in META]
        shown = ", ".join(f"{k} {' '.join(_fmt(p.get(k)) for p in packets)}" if k == "m"
                          else f"{k} {_fmt(packets[-1].get(k))}" for k in keys)
        found[sensor] = problems(sensor, packets)
        say(f"  {sensor:<5} {what:<30} {shown}")
        for problem in found[sensor]:
            say(f"        ! {problem}")
    bad = [s for s, p in found.items() if p]
    say("[CHECK] " + (f"Worth a look: {', '.join(bad)}." if bad else
                      f"All {len(found)} sensor(s) look fine."))
    return found


def offer_soil_calibration(path, node_id, usb_node, readings, wizard, db_file, gap=3,
                           samples=10, sleep=time.sleep):
    '''For each soil sensor that answered but isn't fully calibrated, offer
    to take its dry or wet reading now from live readings.'''
    node = read_node_file(path)["nodes"].get(str(node_id), {})
    for sensor in sorted(s for s in readings if re.fullmatch(r"s\d", s)):
        have = (node.get("soil") or {}).get(sensor, {})
        missing = [p for p in ("dry", "wet") if p not in have]
        if not missing:
            continue
        if "auto" in have:
            wizard.say(f"  Soil sensor {sensor} is calibrating itself: water it thoroughly once "
                       "it has been in its dry soil a few minutes.")
            continue
        wizard.say(f"  Soil sensor {sensor} has no {' or '.join(missing)} reading yet "
                   "(the dashboard's percent needs both).")
        point = wizard.choose("Calibrate it now?", [
            ("auto", "Automatically: it's in dry soil; I'll water it in a few minutes"),
            ("dry", "Take its dry reading: it's in its pot, and the soil is due for watering"),
            ("wet", "Take its wet reading: it was watered 30-60 minutes ago"),
            ("skip", "Not now")], "skip")
        if point == "skip":
            continue
        if point == "auto":
            soil_calibration.calibrate(path, node_id, sensor, "auto", wizard, db_file)
            continue
        wizard.say(f"[CHECK] Reading {sensor} {samples} times over {gap * (samples - 1)} s...")
        values = [p["m"] for p in take_readings(usb_node, node_id, samples, gap, sleep).get(sensor, [])
                  if isinstance(p.get("m"), (int, float))]
        if len(values) < soil_calibration.MIN_READINGS:
            wizard.say(f"[CHECK] Only {len(values)} reading(s) came back; skipping {sensor}.")
            continue
        s = soil_calibration.summary(values)
        wizard.say(f"[CHECK] Median {s['median']} (from {min(values):g} to {max(values):g}).")
        for problem in soil_problems(values):
            wizard.say(f"        ! {problem}")
        soil_calibration.calibrate(path, node_id, sensor, point, wizard, db_file, value=s["median"])
        other = "wet" if point == "dry" else "dry"
        if other in missing:
            when = ("30-60 minutes after watering" if other == "wet"
                    else "when the soil is next due for watering")
            wizard.say(f"[CHECK] For the {other} reading, {when} run:\n"
                       f"          python3 node_setup.py calibrate {node_id} {sensor} {other} --minutes 20")
