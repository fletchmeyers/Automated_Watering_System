# tests/software_tests/test_soil_auto.py
# Automatic soil calibration (soil_calibration.py): spotting the watering,
# taking dry and wet from the readings around it, starting by itself for a
# soil sensor the Pi hasn't seen before.
import json
import sqlite3
from datetime import datetime, timedelta

import pytest

import nodes
from nodes import NodeConfigError, check_node, load_nodes
from soil_calibration import auto_progress, calibrate, start_new_sensors, update_auto
from test_node_list import Typing

T0 = datetime(2026, 10, 11, 12, 0, 0)


def minutes(*values, start=0):
    '''One reading a minute: [(time, raw)].'''
    return [(T0 + timedelta(minutes=start + i), v) for i, v in enumerate(values)]


def watered_at_10(soaked=980, dry=825):
    '''Dry for 10 minutes, watered, then 90 minutes of wet soil.'''
    return minutes(*[dry] * 10, *[1012] * 5, *[soaked] * 85)


# ── Reading the watering out of the readings ────────────────────────────────

def test_it_waits_until_the_sensor_is_watered():
    assert auto_progress(minutes(*[825] * 30), T0 + timedelta(minutes=30)) == {"state": "waiting"}


def test_noise_isnt_mistaken_for_a_watering():
    noisy = minutes(*[825, 880, 830, 822, 885, 828, 826, 890, 824, 827] * 6)   # single spikes
    assert auto_progress(noisy, T0 + timedelta(hours=1))["state"] == "waiting"


def test_after_watering_it_soaks_for_an_hour():
    progress = auto_progress(watered_at_10()[:40], T0 + timedelta(minutes=40))
    assert progress == {"state": "soaking", "watered": T0 + timedelta(minutes=10), "dry": 825}


def test_wet_is_the_median_half_an_hour_to_an_hour_after_watering():
    progress = auto_progress(watered_at_10(), T0 + timedelta(minutes=75))
    assert progress == {"state": "done", "watered": T0 + timedelta(minutes=10), "dry": 825, "wet": 980}


def test_missed_polls_still_finish_with_what_came_later():
    readings = watered_at_10()[:15] + minutes(978, 979, 980, start=95)   # nothing from 15 to 95 min
    progress = auto_progress(readings, T0 + timedelta(minutes=100))
    assert (progress["state"], progress["wet"]) == ("done", 979)


# ── nodes.json ───────────────────────────────────────────────────────────────

def esp32(**changes):
    node = {"name": "ESP32-V2 (Arduino)", "framework": "arduino", "board": "feather_esp32_v2",
            "link": "wifi", "sense_interval_s": 3, "log_interval_s": 60}
    node.update(changes)
    return node


def test_a_calibration_in_progress_is_valid():
    check_node(3, esp32(soil={"s1": {"auto": {"since": "2026-10-11T12:00:00", "watered": None}}}))
    check_node(3, esp32(soil={"s1": {"auto": {"since": "2026-10-11T12:00:00",
                                              "watered": "2026-10-11T12:10:00"}}}))


@pytest.mark.parametrize("auto", [{"since": "noon"}, {"since": "2026-10-11T12:00:00", "when": 1}, "yes"])
def test_a_garbled_calibration_in_progress_is_reported(auto):
    with pytest.raises(NodeConfigError, match="auto must be"):
        check_node(3, esp32(soil={"s1": {"auto": auto}}))


# ── Running it on the Pi ─────────────────────────────────────────────────────

@pytest.fixture
def pi(tmp_path):
    path = tmp_path / "nodes.json"
    data = json.loads(nodes.EXAMPLE_FILE.read_text())
    data["nodes"]["3"] = esp32()
    path.write_text(json.dumps(data))
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE readings (ts TEXT, node_id INTEGER, sensor_type TEXT, key TEXT, value REAL)")

    def add(readings, node_id=3, sensor="s1"):
        conn.executemany("INSERT INTO readings VALUES (?, ?, ?, 'm', ?)",
                         [(t.strftime("%Y-%m-%dT%H:%M:%S"), node_id, sensor, v) for t, v in readings])
    return path, conn, add, tmp_path


def soil(path, node_id=3):
    return load_nodes(path)[node_id].get("soil")


def test_the_pi_finishes_a_calibration_and_saves_it(pi):
    path, conn, add, _ = pi
    calibrate(path, 3, "s1", "auto", Typing().wizard(), None)          # nothing to confirm
    data = json.loads(path.read_text())
    data["nodes"]["3"]["soil"]["s1"]["auto"]["since"] = T0.strftime("%Y-%m-%dT%H:%M:%S")
    path.write_text(json.dumps(data))
    add(watered_at_10())
    said = []

    assert update_auto(conn, path, now=T0 + timedelta(minutes=5), say=said.append) == []
    assert soil(path)["s1"]["auto"]["watered"] is None              # not watered yet

    assert update_auto(conn, path, now=T0 + timedelta(minutes=20), say=said.append) == []
    assert soil(path)["s1"]["auto"]["watered"] == "2026-10-11T12:10:00"

    assert update_auto(conn, path, now=T0 + timedelta(minutes=75), say=said.append) == [(3, "s1")]
    assert soil(path) == {"s1": {"dry": 825, "wet": 980}}
    assert any("calibrated: dry 825, wet 980" in s for s in said)


def test_a_watering_that_drains_away_is_waited_out(pi):
    path, conn, add, _ = pi
    data = json.loads(path.read_text())
    data["nodes"]["3"]["soil"] = {"s1": {"auto": {"since": T0.strftime("%Y-%m-%dT%H:%M:%S"), "watered": None}}}
    path.write_text(json.dumps(data))
    add(minutes(*[825] * 10, *[1012] * 5, *[835] * 85))             # back to dry an hour later
    said = []
    assert update_auto(conn, path, now=T0 + timedelta(minutes=80), say=said.append) == []
    auto = soil(path)["s1"]["auto"]
    assert auto == {"since": "2026-10-11T13:20:00", "watered": None}  # starts over from now
    assert any("didn't stay" in s for s in said)


def fields(*pairs):
    return [{"node_id": n, "sensor_type": t, "key": k} for n, t in pairs for k in ("m", "tmp")]


def test_sensors_already_there_the_first_time_are_left_alone(pi):
    path, _, _, tmp_path = pi
    seen = tmp_path / "seen.json"
    assert start_new_sensors(fields((1, "s0"), (1, "s2")), path, seen, say=lambda m: None) == []
    assert json.loads(seen.read_text()) == [[1, "s0"], [1, "s2"]]
    assert soil(path, 1) is None


def test_a_new_soil_sensor_starts_calibrating(pi):
    path, _, _, tmp_path = pi
    seen = tmp_path / "seen.json"
    seen.write_text(json.dumps([[1, "s0"], [1, "s2"]]))
    said = []
    started = start_new_sensors(fields((1, "s0"), (1, "s2"), (3, "s1"), (99, "s0"), (3, "uv")),
                                path, seen, now=T0, say=said.append)
    assert started == [(3, "s1")]                                    # node 99 isn't in the list
    assert soil(path) == {"s1": {"auto": {"since": "2026-10-11T12:00:00", "watered": None}}}
    assert [3, "s1"] in json.loads(seen.read_text()) and [99, "s0"] in json.loads(seen.read_text())
    # Seen now: not started again.
    assert start_new_sensors(fields((3, "s1")), path, seen, say=lambda m: None) == []


def test_an_already_calibrated_new_sensor_isnt_restarted(pi):
    path, _, _, tmp_path = pi
    seen = tmp_path / "seen.json"
    seen.write_text("[]")
    data = json.loads(path.read_text())
    data["nodes"]["3"]["soil"] = {"s1": {"dry": 825, "wet": 984}}
    path.write_text(json.dumps(data))
    assert start_new_sensors(fields((3, "s1")), path, seen, say=lambda m: None) == []
    assert soil(path) == {"s1": {"dry": 825, "wet": 984}}


def test_the_hardware_check_can_start_it(pi):
    from sensor_check import offer_soil_calibration
    from test_sensor_check import FakeUsbNode, readings_of
    path, _, _, tmp_path = pi
    t = Typing("1")                      # automatically
    offer_soil_calibration(path, 3, FakeUsbNode(), readings_of({"s1": {"m": 825, "tmp": 20}}),
                           t.wizard(), tmp_path / "none.db", sleep=lambda s: None)
    assert "auto" in soil(path)["s1"]
    t = Typing()                         # already calibrating: no question
    offer_soil_calibration(path, 3, FakeUsbNode(), readings_of({"s1": {"m": 825, "tmp": 20}}),
                           t.wizard(), tmp_path / "none.db", sleep=lambda s: None)
    assert any("calibrating itself" in s for s in t.said)
