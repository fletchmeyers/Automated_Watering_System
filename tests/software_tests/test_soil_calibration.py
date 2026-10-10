# tests/software_tests/test_soil_calibration.py
# Soil sensor calibration: nodes.json "soil", node_setup.py calibrate taking
# the median of a sensor's readings in sensors.db, and what the dashboard gets.
import json
import sqlite3
import sys
from datetime import datetime, timedelta

import pytest

import node_setup
import nodes
import soil_calibration
from nodes import NodeConfigError, check_node, load_nodes, public_nodes
from soil_calibration import calibrate, parse_until, summary
from test_node_list import Typing

NOW = datetime.now().replace(microsecond=0)


def esp32(**changes):
    node = {"name": "ESP32-V2 (Arduino)", "framework": "arduino", "board": "feather_esp32_v2",
            "link": "wifi", "sense_interval_s": 3, "log_interval_s": 60}
    node.update(changes)
    return node


# ── nodes.json ───────────────────────────────────────────────────────────────

def test_a_calibration_can_have_either_end_or_both():
    check_node(3, esp32(soil={"s1": {"dry": 940, "wet": 610}, "s0": {"dry": 900}}))


@pytest.mark.parametrize("soil, message", [
    ([940], "soil must be"),
    ({"soil1": {"dry": 940}}, 'named "s0"'),
    ({"s1": {"dry": "940"}}, '"dry": <raw reading>'),
    ({"s1": {"dry": 940, "damp": 700}}, '"dry": <raw reading>'),
    ({"s1": {"dry": True}}, '"dry": <raw reading>'),
    ({"s1": {"dry": 700, "wet": 700}}, "must be different"),
])
def test_calibration_mistakes_are_reported(soil, message):
    with pytest.raises(NodeConfigError, match=message):
        check_node(3, esp32(soil=soil))


def test_the_dashboard_is_given_the_calibration():
    assert public_nodes({3: esp32(soil={"s1": {"dry": 940}})})[3]["soil"] == {"s1": {"dry": 940}}


# ── Working out a reading ────────────────────────────────────────────────────

def test_the_median_shrugs_off_wild_readings():
    s = summary([940, 945, 938, 1013, 942, 810, 941])
    assert s["median"] == 941 and s["count"] == 7


def test_until_takes_a_time_today_or_yesterday():
    now = datetime(2026, 10, 10, 19, 0)
    assert parse_until("18:30", now) == datetime(2026, 10, 10, 18, 30)
    assert parse_until("23:15", now) == datetime(2026, 10, 9, 23, 15)   # still to come today
    assert parse_until("2026-10-08T07:00", now) == datetime(2026, 10, 8, 7, 0)
    assert parse_until(None, now) == now
    with pytest.raises(NodeConfigError, match="should look like"):
        parse_until("teatime", now)


# ── node_setup.py calibrate ──────────────────────────────────────────────────

@pytest.fixture
def setup(tmp_path):
    '''A node list with node 3, and a sensors.db with an hour of its s1 readings.'''
    path = tmp_path / "nodes.json"
    data = json.loads(nodes.EXAMPLE_FILE.read_text())
    data["nodes"]["3"] = esp32()
    path.write_text(json.dumps(data))
    db_file = tmp_path / "sensors.db"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE readings (ts TEXT, node_id INTEGER, sensor_type TEXT, key TEXT, value REAL)")
    conn.commit()
    conn.close()

    def add(values, minutes_ago_first, node_id=3, sensor="s1", key="m"):
        conn = sqlite3.connect(db_file)
        conn.executemany("INSERT INTO readings VALUES (?, ?, ?, ?, ?)", [
            ((NOW - timedelta(minutes=minutes_ago_first - i)).strftime("%Y-%m-%dT%H:%M:%S"),
             node_id, sensor, key, v) for i, v in enumerate(values)])
        conn.commit()
        conn.close()
    return path, db_file, add


def soil_of(path, node_id=3):
    return load_nodes(path)[node_id].get("soil")


def test_dry_is_the_median_of_the_last_hour(setup):
    path, db_file, add = setup
    add([600] * 10, 150)                                    # long before the window
    add([940, 945, 938, 1013, 942, 810, 941, 939, 944, 940], 30)
    add([500] * 5, 30, sensor="s0")                         # another sensor
    add([22.0] * 5, 30, key="tmp")                          # its temperature
    t = Typing("")                                          # save? yes
    assert calibrate(path, 3, "s1", "dry", t.wizard(), db_file)
    assert soil_of(path) == {"s1": {"dry": 940}}
    assert any("10 readings, median 940" in s for s in t.said)
    assert any("wet isn't set yet" in s for s in t.said)


def test_wet_from_an_earlier_window_keeps_dry(setup):
    path, db_file, add = setup
    add([610, 612, 608, 611, 609], 100)
    calibrate(path, 3, "s1", "dry", Typing("").wizard(), db_file, value=940)
    until = (NOW - timedelta(minutes=90)).strftime("%Y-%m-%dT%H:%M:%S")
    t = Typing("")
    calibrate(path, 3, "s1", "wet", t.wizard(), db_file, minutes=20, until=until)
    assert soil_of(path) == {"s1": {"dry": 940, "wet": 610}}
    assert any("wet = 610, dry = 940" in s for s in t.said)


def test_spread_out_or_topped_out_readings_are_warned_about(setup):
    path, db_file, add = setup
    add([1013, 1015, 950, 1016, 930, 1015, 900, 1014, 1015], 20)
    t = Typing("n")
    assert not calibrate(path, 3, "s1", "wet", t.wizard(), db_file)
    assert any("spread out" in s for s in t.said)
    assert any("sensor's top" in s for s in t.said)
    assert soil_of(path) is None                            # said no: nothing saved


def test_too_few_readings_says_how_to_pick_another_window(setup):
    path, db_file, add = setup
    add([940, 941], 10)
    with pytest.raises(NodeConfigError, match=r"has 2 moisture reading\(s\).*--until/--minutes"):
        calibrate(path, 3, "s1", "dry", Typing().wizard(), db_file)


def test_the_same_reading_for_both_ends_is_refused(setup):
    path, db_file, _ = setup
    calibrate(path, 3, "s1", "dry", Typing("").wizard(), db_file, value=700)
    with pytest.raises(NodeConfigError, match="must be different"):
        calibrate(path, 3, "s1", "wet", Typing("").wizard(), db_file, value=700)


def test_clear_goes_back_to_the_usual_scale(setup):
    path, db_file, _ = setup
    calibrate(path, 3, "s1", "dry", Typing("").wizard(), db_file, value=940)
    assert calibrate(path, 3, "s1", "clear", Typing("").wizard(), db_file)
    assert "soil" not in load_nodes(path)[3]
    t = Typing()
    assert not calibrate(path, 3, "s1", "clear", t.wizard(), db_file)
    assert any("isn't calibrated" in s for s in t.said)


@pytest.mark.parametrize("node_id, sensor, message", [(99, "s1", "isn't in"), (3, "soil1", "named s0")])
def test_bad_node_or_sensor(setup, node_id, sensor, message):
    path, db_file, _ = setup
    with pytest.raises(NodeConfigError, match=message):
        calibrate(path, node_id, sensor, "dry", Typing().wizard(), db_file, value=900)


def test_the_command_line(setup, monkeypatch):
    path, db_file, _ = setup
    called = {}
    monkeypatch.setattr(soil_calibration, "calibrate",
                        lambda *a, **kw: called.update(args=a[:4], **kw))
    monkeypatch.setattr(sys, "argv", ["node_setup.py", "calibrate", "3", "s1", "wet", "--minutes", "20",
                                      "--until", "18:30", "--nodes", str(path)])
    node_setup.main()
    assert called["args"] == (str(path), 3, "s1", "wet")
    assert (called["minutes"], called["until"], called["value"]) == (20, "18:30", None)

    monkeypatch.setattr(sys, "argv", ["node_setup.py", "calibrate", "3", "s1", "damp"])
    with pytest.raises(SystemExit):
        node_setup.main()
