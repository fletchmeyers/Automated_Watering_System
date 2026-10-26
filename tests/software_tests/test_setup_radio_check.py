# tests/software_tests/test_setup_radio_check.py
"""Setting a node up: changing its pins (node_setup.py pins), and checking the
Pi can reach it over the radio afterwards (node_setup.check_radio)."""
import json

import pytest

import node_setup
import node_wizard
from nodes import NodeConfigError


@pytest.fixture
def node_file(tmp_path):
    path = tmp_path / "nodes.json"
    path.write_text(json.dumps({"nodes": {
        "5": {"name": "Adalogger", "framework": "arduino", "board": "feather_rp2040_adalogger",
              "sense_interval_s": 3, "log_interval_s": 60,
              "pins": {"radio_cs": 10, "radio_irq": 6, "radio_rst": 11, "sd_cs": 23}},
        "1": {"name": "Pico", "framework": "circuitpython", "board": "pico2w",
              "sense_interval_s": 3, "log_interval_s": 60,
              "pins": {"spi_sck": "GP18", "spi_mosi": "GP19", "spi_miso": "GP16",
                       "radio_cs": "GP22", "radio_rst": "GP27", "sd_cs": "GP17"}}}}))
    return path


def test_arduino_pins_are_numbers(node_file):
    node = node_wizard.set_pins(node_file, 5, {"radio_cs": "6", "radio_irq": "11", "radio_rst": "10"})
    assert node["pins"] == {"radio_cs": 6, "radio_irq": 11, "radio_rst": 10, "sd_cs": 23}
    assert json.loads(node_file.read_text())["nodes"]["5"]["pins"]["radio_cs"] == 6


def test_circuitpython_pins_are_names(node_file):
    node = node_wizard.set_pins(node_file, 1, {"radio_cs": "GP21"})
    assert node["pins"]["radio_cs"] == "GP21"


@pytest.mark.parametrize("node_id, pins, message", [
    (5, {"radio_cs": "D6"}, "must be a number"),
    (5, {"radoi_cs": "6"}, "unknown pin"),
    (9, {"radio_cs": "6"}, "isn't in"),
])
def test_pin_mistakes(node_file, node_id, pins, message):
    with pytest.raises(NodeConfigError, match=message):
        node_wizard.set_pins(node_file, node_id, pins)
    assert json.loads(node_file.read_text())["nodes"]["5"]["pins"]["radio_cs"] == 10   # nothing saved


def ping(result):
    asked = []

    def request(node_id, count):
        asked.append((node_id, count))

    return asked, request, (lambda node_id, timeout: result)


def test_a_node_that_answers_over_the_radio(capsys):
    asked, request, wait = ping({"count": 5, "hits": 5, "avg_rssi_dbm": -42})
    assert node_setup.check_radio(5, request, wait) == 5
    assert asked == [(5, node_setup.RADIO_CHECK_PINGS)]
    assert "Node 5 answers over the radio: 5/5 pongs, -42 dBm." in capsys.readouterr().out


def test_a_node_that_doesnt_says_how_to_fix_its_pins(capsys):
    _, request, wait = ping({"count": 5, "hits": 0, "avg_rssi_dbm": None})
    assert node_setup.check_radio(5, request, wait) == 0
    out = capsys.readouterr().out
    assert "didn't answer over the radio" in out
    assert "    python3 node_setup.py pins 5\n" in out
    assert "    python3 node_setup.py pins 5 radio_cs=6 radio_irq=11 radio_rst=10\n" in out


def test_no_radio_loop_running(capsys):
    _, request, wait = ping(None)
    assert node_setup.check_radio(5, request, wait) is None
    assert "sudo systemctl restart garden-radio" in capsys.readouterr().out
