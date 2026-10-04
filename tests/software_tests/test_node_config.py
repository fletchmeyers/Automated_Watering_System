# tests/software_tests/test_node_config.py
# nodes.json as the one source of node settings, and node_setup.py's updates.
import copy
import importlib
import json
import sys

import pytest

import node_setup
from nodes import (NodeConfigError, load_nodes, check_node, sleep_windows, sync_node_ids,
                   circuitpython_config, arduino_build_flags, arduino_env)


def real():
    return load_nodes()


# ── nodes.json ───────────────────────────────────────────────────────────────

def test_repo_nodes_json_matches_the_running_setup():
    n = real()
    assert list(n) == [1, 2]
    assert sleep_windows(n) == {2: ("19:00", "07:00")}
    assert sync_node_ids(n) == [1, 2]
    assert n[1]["framework"] == "circuitpython" and n[2]["framework"] == "arduino"


def test_arduino_flags_reproduce_the_m0_header_defaults():
    flags = arduino_build_flags(2, real()[2])
    assert arduino_env(real()[2]) == "feather_m0"
    assert flags == ["-D NODE_ID=2", "-D RFM69_CS=9", "-D RFM69_INT=6", "-D RFM69_RST=11",
                     "-D DEFAULT_SENSE_INTERVAL_MS=3000UL", "-D LOG_INTERVAL_MS=300000UL",
                     "-D LOG_BACKEND=LOG_SD", "-D SD_CS=10"]


def test_flash_storage_has_no_sd_pin_flag():
    node = copy.deepcopy(real()[2])
    node["storage"] = "flash"
    del node["pins"]["sd_cs"]
    flags = arduino_build_flags(5, node)
    assert "-D LOG_BACKEND=LOG_FLASH_SAMD" in flags
    assert not any("SD_CS" in f for f in flags)


@pytest.mark.parametrize("change, message", [
    (lambda n: n.update(framework="micropython"), "framework must be one of"),
    (lambda n: n.update(board="esp32"), "no Arduino board config"),
    (lambda n: n["pins"].pop("radio_irq"), 'needs pin "radio_irq"'),
    (lambda n: n["pins"].update(radio_cs="D9"), "must be a number"),
    (lambda n: n["pins"].pop("sd_cs"), 'storage "sd" needs pin "sd_cs"'),
    (lambda n: n["pins"].update(i2c_scl=3), 'both "i2c_scl" and "i2c_sda"'),
    (lambda n: n["pins"].update(radio_led=13), 'unknown pin "radio_led"'),
    (lambda n: n.update(log_interval_s=0), "log_interval_s"),
    (lambda n: n.update(sleep_window=["7pm", "07:00"]), "sleep_window"),
    (lambda n: n.update(storage="cloud"), "storage must be one of"),
])
def test_mistakes_in_a_node_are_reported_clearly(change, message):
    node = copy.deepcopy(real()[2])
    change(node)
    with pytest.raises(NodeConfigError, match=message):
        check_node(2, node)


def test_flash_storage_is_arduino_only():
    node = copy.deepcopy(real()[1])
    node["storage"] = "flash"
    with pytest.raises(NodeConfigError, match="only for Arduino"):
        check_node(1, node)


def test_bad_node_ids_and_files(tmp_path):
    path = tmp_path / "nodes.json"
    path.write_text(json.dumps({"nodes": {"abc": real()[1]}}))
    with pytest.raises(NodeConfigError, match="not a number"):
        load_nodes(path)
    path.write_text(json.dumps({"nodes": {"300": real()[1]}}))
    with pytest.raises(NodeConfigError, match="1-254"):
        load_nodes(path)
    path.write_text("{not json")
    with pytest.raises(NodeConfigError, match="Could not read"):
        load_nodes(path)


# ── The Pico reading its generated node_config.py ────────────────────────────

def test_pico_picks_up_its_generated_config(tmp_path, monkeypatch):
    node = copy.deepcopy(real()[1])
    node.update(sense_interval_s=7, log_interval_s=120)
    node["pins"]["radio_cs"] = "GP5"
    (tmp_path / "node_config.py").write_text(circuitpython_config(3, node))
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("node_config", None)
    import hardware_setup_garden
    try:
        hw = importlib.reload(hardware_setup_garden)
        assert (hw.NODE_ID, hw.SENSE_INTERVAL, hw.LOG_INTERVAL, hw.USE_SD) == (3, 7, 120, True)
        assert hw.PINS["radio_cs"] == "GP5"
        assert hw.pin("radio_cs") is getattr(hw.board, "GP5")
    finally:
        sys.modules.pop("node_config", None)
        monkeypatch.undo()
        hw = importlib.reload(hardware_setup_garden)   # back to the built-in defaults
    assert hw.NODE_ID == 1


# ── Updating a CircuitPython drive ───────────────────────────────────────────

def test_node_id_is_read_from_the_drive(tmp_path):
    assert node_setup.node_id_on_drive(tmp_path) is None
    (tmp_path / "hardware_setup_garden.py").write_text("import board\n\n# CONFIG\nNODE_ID = 1\n")
    assert node_setup.node_id_on_drive(tmp_path) == 1            # set up by hand, before node_config.py
    (tmp_path / "node_config.py").write_text(circuitpython_config(4, real()[1]))
    assert node_setup.node_id_on_drive(tmp_path) == 4


def test_update_copies_only_what_differs(tmp_path):
    drive = tmp_path
    node = real()[1]
    first = node_setup.plan_circuitpython(drive, 1, node)
    names = {str(rel).replace("\\", "/") for rel, _ in first}
    assert {"code.py", "boot.py", "node_config.py", "lib/adafruit_rfm69.mpy"} <= names
    assert not any(n.startswith("lib/adafruit_display_text") for n in names)   # unused, not added
    assert str(first[-1][0]) == "code.py"                                      # code.py goes last

    node_setup.apply_plan(drive, first)
    assert node_setup.plan_circuitpython(drive, 1, node) == []                # nothing left to do
    assert node_setup.node_id_on_drive(drive) == 1

    changed = copy.deepcopy(node)
    changed["log_interval_s"] = 30
    assert [str(rel) for rel, _ in node_setup.plan_circuitpython(drive, 1, changed)] == ["node_config.py"]


def test_libraries_already_on_the_drive_are_kept_up_to_date(tmp_path):
    lib = tmp_path / "lib" / "adafruit_display_text"
    lib.mkdir(parents=True)
    (lib / "label.mpy").write_bytes(b"old version")
    names = {str(rel).replace("\\", "/") for rel, _ in node_setup.plan_circuitpython(tmp_path, 1, real()[1])}
    assert "lib/adafruit_display_text/label.mpy" in names
