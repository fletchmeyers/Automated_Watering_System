# tests/software_tests/test_sensor_check.py
# The hardware check after setting a node up (sensor_check.py): polling it
# over USB, spotting readings that look wrong, and calibrating soil sensors
# from live readings.
import json

import pytest

import nodes
import sensor_check
from nodes import load_nodes
from sensor_check import offer_soil_calibration, problems, report, take_readings
from test_node_list import Typing


class FakeUsbNode:
    '''Answers each poll with the next of the given readings ({sensor: {key: value}}),
    as the firmware does: a ts header, one packet per sensor, then batch_end.'''
    def __init__(self, *polls):
        self.polls = list(polls)
        self.sent = []

    def send(self, command):
        self.sent.append(command)

    def packets(self, timeout):
        if self.sent[-1]["t"] != "poll" or not self.polls:
            return
        yield {"t": "ts", "v": self.sent[-1]["ts"], "q": 0, "n": 3}
        for q, (sensor, values) in enumerate(self.polls.pop(0).items(), 1):
            yield {"t": sensor, "q": q, "n": 3, **values}
        yield {"t": "batch_end", "q": 99, "n": 3, "exp": 1, "snt": 1}


def no_wait(_):
    pass


def readings_of(*polls):
    return take_readings(FakeUsbNode(*polls), 3, len(polls), 0, no_wait)


# ── Spotting problems ────────────────────────────────────────────────────────

def test_good_readings_pass():
    readings = readings_of(*[{"s1": {"m": 825, "tmp": 21.5}, "sht": {"tmp": 21.0, "rh": 45.0},
                              "vbat": {"v": 4.1}}] * 3)
    said = []
    assert report(readings, said.append) == {"s1": [], "sht": [], "vbat": []}
    assert said[-1] == "[CHECK] All 3 sensor(s) look fine."
    assert any("s1" in s and "m 825 825 825" in s for s in said)


@pytest.mark.parametrize("values, expected", [
    ([1015, 1016, 1014], "top of its range"),
    ([210, 205, 208], "pushed into the soil"),
    ([800, 870, 830], "jumping about"),
    ([825, 65535, 826], "stopped answering"),
])
def test_soil_problems(values, expected):
    readings = readings_of(*[{"s0": {"m": v, "tmp": 20.0}} for v in values])
    [problem] = problems("s0", readings["s0"])
    assert expected in problem


def test_out_of_range_and_missing_values_are_reported():
    readings = readings_of({"sht": {"tmp": 21.0, "rh": 120.0}, "batt": {"v": 4.0}})
    assert problems("sht", readings["sht"]) == ["rh 120–120 is outside 0–100"]
    assert problems("batt", readings["batt"]) == ["no soc reading"]


def test_a_sensor_without_a_check_is_still_listed():
    said = []
    assert report(readings_of({"lidar": {"d": 3}}), said.append) == {"lidar": []}
    assert any("lidar" in s and "d 3" in s for s in said)


def test_no_sensors_says_where_to_look():
    said = []
    assert report(readings_of({}), said.append) == {}
    assert "STEMMA QT" in said[0]


def test_a_node_that_stops_answering_just_gives_fewer_readings():
    readings = take_readings(FakeUsbNode({"s1": {"m": 825, "tmp": 20}}), 3, 3, 0, no_wait)
    assert len(readings["s1"]) == 1


# ── Calibrating from live readings ───────────────────────────────────────────

@pytest.fixture
def node_file(tmp_path):
    path = tmp_path / "nodes.json"
    data = json.loads(nodes.EXAMPLE_FILE.read_text())
    data["nodes"]["3"] = {"name": "ESP32-V2 (Arduino)", "framework": "arduino",
                          "board": "feather_esp32_v2", "link": "wifi",
                          "sense_interval_s": 3, "log_interval_s": 60}
    path.write_text(json.dumps(data))
    return path


def test_dry_is_taken_from_live_readings_and_wet_is_explained(node_file, tmp_path):
    first = readings_of({"s1": {"m": 826, "tmp": 20}})
    live = FakeUsbNode(*[{"s1": {"m": m, "tmp": 20}} for m in [825, 826, 825, 824, 825, 900, 825, 826, 825, 825]])
    t = Typing("1", "")                   # dry, then save
    offer_soil_calibration(node_file, 3, live, first, t.wizard(), tmp_path / "none.db", gap=0, sleep=no_wait)
    assert load_nodes(node_file)[3]["soil"] == {"s1": {"dry": 825}}
    assert len(live.sent) == 10
    assert any("calibrate 3 s1 wet" in s for s in t.said)


def test_skipping_changes_nothing(node_file, tmp_path):
    t = Typing("")                        # "Not now" is the default
    offer_soil_calibration(node_file, 3, FakeUsbNode(), readings_of({"s1": {"m": 825, "tmp": 20}}),
                           t.wizard(), tmp_path / "none.db", sleep=no_wait)
    assert "soil" not in load_nodes(node_file)[3]


def test_a_calibrated_sensor_isnt_asked_about(node_file, tmp_path):
    data = json.loads(node_file.read_text())
    data["nodes"]["3"]["soil"] = {"s1": {"dry": 825, "wet": 984}}
    node_file.write_text(json.dumps(data))
    t = Typing()                          # any question would fail the test
    offer_soil_calibration(node_file, 3, FakeUsbNode(),
                           readings_of({"s1": {"m": 825, "tmp": 20}, "sht": {"tmp": 20, "rh": 40}}),
                           t.wizard(), tmp_path / "none.db", sleep=no_wait)
    assert t.prompts == []


def test_only_the_missing_end_is_asked_for(node_file, tmp_path):
    data = json.loads(node_file.read_text())
    data["nodes"]["3"]["soil"] = {"s1": {"dry": 825}}
    node_file.write_text(json.dumps(data))
    live = FakeUsbNode(*[{"s1": {"m": 984, "tmp": 20}}] * 10)
    t = Typing("2", "")                   # wet, then save
    offer_soil_calibration(node_file, 3, live, readings_of({"s1": {"m": 984, "tmp": 20}}),
                           t.wizard(), tmp_path / "none.db", gap=0, sleep=no_wait)
    assert load_nodes(node_file)[3]["soil"] == {"s1": {"dry": 825, "wet": 984}}
    assert any("has no wet reading yet" in s for s in t.said)
    assert not any("calibrate 3 s1" in s for s in t.said)   # nothing left to do later
