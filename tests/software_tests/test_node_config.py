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
    (lambda n: n.update(board="esp32"), "unknown board"),
    (lambda n: n.update(board="feather_esp32_v2"), "arduino on feather_esp32_v2 isn.t supported yet"),
    (lambda n: n.update(color="pink"), "color must look like"),
    (lambda n: n.update(battery="pw0"), "battery must be"),
    (lambda n: n["pins"].update(sd_sck=1), "all of"),
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


# ── Installing CircuitPython ─────────────────────────────────────────────────

def test_uf2_comes_from_circuitpython_org_for_the_nodes_board():
    from nodes import circuitpython_uf2_url, CIRCUITPYTHON_VERSION
    assert circuitpython_uf2_url(real()[1]) == (
        "https://downloads.circuitpython.org/bin/raspberry_pi_pico2_w/en_US/"
        f"adafruit-circuitpython-raspberry_pi_pico2_w-en_US-{CIRCUITPYTHON_VERSION}.uf2")
    assert CIRCUITPYTHON_VERSION.startswith("10.")   # must match the .mpy libraries


def test_every_bootloader_drive_maps_to_a_known_chip():
    from nodes import BOARDS, BOOTLOADER_DRIVES
    chips = {b["chip"] for b in BOARDS.values() if b["circuitpython"]}
    assert chips == set(BOOTLOADER_DRIVES.values())


def test_uf2_is_downloaded_once_then_cached(tmp_path, monkeypatch):
    fetched = []

    class Response:
        def __init__(self, url):
            fetched.append(url)
            self.data = [b"UF2 bytes"]
        def read(self, n=-1):
            return self.data.pop() if self.data else b""
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass

    monkeypatch.setattr(node_setup, "UF2_CACHE", tmp_path)
    monkeypatch.setattr(node_setup.urllib.request, "urlopen", lambda req, timeout: Response(req.full_url))
    first = node_setup.download_uf2(real()[1])
    again = node_setup.download_uf2(real()[1])
    assert first == again and first.read_bytes() == b"UF2 bytes"
    assert len(fetched) == 1 and first.name.endswith(".uf2")


def test_boot_py_change_restarts_the_board_from_its_console(tmp_path, monkeypatch):
    typed = []
    monkeypatch.setattr(node_setup, "run_on_console", lambda lines: typed.extend(lines) or True)

    @node_setup.contextmanager
    def fake_mount(dev, where, read_only=False):
        yield tmp_path
    monkeypatch.setattr(node_setup, "mounted", fake_mount)

    assert node_setup.update_circuitpython(1, real()[1], dry_run=False)   # fresh drive: boot.py is new
    assert typed == ["import microcontroller", "microcontroller.reset()"]

    typed.clear()
    changed = copy.deepcopy(real()[1])
    changed["log_interval_s"] = 30
    assert node_setup.update_circuitpython(1, changed, dry_run=False)     # only node_config.py
    assert typed == []                                                   # auto-reload is enough


def test_a_bare_board_still_starts(monkeypatch):
    '''No STEMMA QT port, unusable SPI pins, no radio, no PCF8523 (a plain Pico on the bench).'''
    from unittest.mock import MagicMock
    import hardware_setup_garden
    monkeypatch.setattr(sys.modules["board"], "STEMMA_I2C", MagicMock(side_effect=ValueError("no STEMMA")))
    monkeypatch.setattr(sys.modules["busio"], "SPI", MagicMock(side_effect=ValueError("pin in use")))
    monkeypatch.setattr(sys.modules["adafruit_rfm69"], "RFM69", MagicMock(side_effect=RuntimeError("no radio")))
    monkeypatch.setattr(sys.modules["adafruit_pcf8523.pcf8523"], "PCF8523",
                        MagicMock(side_effect=AttributeError("no I2C")))
    try:
        hw = importlib.reload(hardware_setup_garden)
        assert hw.i2c is None and hw.spi is None and hw.rfm69 is None
        assert hw.rtc is sys.modules["rtc"].RTC.return_value     # the chip's own clock
    finally:
        monkeypatch.undo()
        importlib.reload(hardware_setup_garden)


def test_libraries_needed_by_other_libraries_are_included():
    needed = node_setup.required_libs()
    assert "adafruit_ina228.mpy" in needed        # imported inside adafruit_ina23x, not by our code
    assert {"adafruit_rfm69.mpy", "adafruit_register", "adafruit_bus_device"} <= needed
    assert not needed & {"adafruit_display_text", "adafruit_ssd1306.mpy", "adafruit_motor"}


def test_console_text_loses_terminal_codes():
    raw = (b"\x1b]0;\xf0\x9f\x90\x8dcode.py | 10.3.1\x1b\Traceback (most recent call last):\r\n"
           b"ImportError: no module named 'adafruit_ina228'\r\n\x1b[2K\x1b[0GCode done running.\r\n")
    text = node_setup._clean(raw)
    assert text == ("Traceback (most recent call last):\n"
                    "ImportError: no module named 'adafruit_ina228'\nCode done running.\n")


def test_console_keeps_trying_while_the_board_is_busy_reloading(monkeypatch):
    '''Just after files are written the board is reloading and ignores Ctrl-C
    for a moment: the console command must wait for the prompt, not give up.'''
    import types
    opened, typed = [], []

    class FakeSerial:
        def __init__(self, path, baud, timeout):
            opened.append(path)
            self.busy = len(opened) <= 2           # the first two tries hit a reload
            self.out = b""
        def reset_input_buffer(self):
            pass
        def write(self, data):
            if data == b"\r" and not self.busy:
                self.out += b"\r\n>>> "
            elif data.endswith(b"\r") and data != b"\r":
                typed.append(data[:-1].decode())
        def close(self):
            pass

    fake = types.SimpleNamespace(Serial=FakeSerial, SerialException=OSError)
    monkeypatch.setitem(sys.modules, "serial", fake)
    monkeypatch.setattr(node_setup, "console_ports", lambda: ["/dev/ttyACM0"])
    monkeypatch.setattr(node_setup, "_read_for", lambda s, seconds: (s.out, setattr(s, "out", b""))[0])
    monkeypatch.setattr(node_setup.time, "sleep", lambda s: None)

    assert node_setup.run_on_console(["import microcontroller", "microcontroller.reset()"]) is not None
    assert len(opened) == 3
    assert typed == ["import microcontroller", "microcontroller.reset()"]


# ── Arduino on RP2040 / RP2350 boards ────────────────────────────────────────

def rp2_node(**changes):
    node = {"name": "Bench Pico", "framework": "arduino", "board": "pico",
            "sense_interval_s": 3, "log_interval_s": 60, "storage": "none",
            "pins": {"radio_cs": 17, "radio_irq": 21, "radio_rst": 20}}
    node.update(changes)
    return node


def test_rp2_arduino_flags_cover_their_own_spi_buses_and_rtc():
    from nodes import arduino_env
    adalogger = rp2_node(board="feather_rp2040_adalogger", storage="sd", rtc="pcf8523",
                         pins={"radio_cs": 10, "radio_irq": 6, "radio_rst": 11,
                               "sd_cs": 23, "sd_sck": 18, "sd_mosi": 19, "sd_miso": 20})
    check_node(12, adalogger)
    flags = arduino_build_flags(12, adalogger)
    assert arduino_env(adalogger) == "feather_rp2040_adalogger"
    assert {"-D LOG_BACKEND=LOG_SD", "-D SD_CS=23", "-D SD_SPI_SCK=18", "-D SD_SPI_MOSI=19",
            "-D SD_SPI_MISO=20", "-D BOARD_HAS_PCF8523"} <= set(flags)
    assert not any("RADIO_SPI" in f for f in flags)          # radio on the board's default SPI

    moved = rp2_node(pins={"radio_cs": 13, "radio_irq": 21, "radio_rst": 20,
                           "spi_sck": 10, "spi_mosi": 11, "spi_miso": 12})
    check_node(9, moved)
    assert {"-D RADIO_SPI_SCK=10", "-D RADIO_SPI_MOSI=11", "-D RADIO_SPI_MISO=12"} <= set(
        arduino_build_flags(9, moved))


@pytest.mark.parametrize("node, message", [
    (rp2_node(storage="flash"), 'only for Arduino SAMD boards'),
    (rp2_node(rtc="ds3231"), "rtc must be one of"),
    (rp2_node(pins={"radio_cs": 17, "radio_irq": 21, "radio_rst": 20, "spi_sck": 10}), 'all of "spi_sck"'),
])
def test_rp2_mistakes_are_reported(node, message):
    with pytest.raises(NodeConfigError, match=message):
        check_node(9, node)


def test_custom_spi_pins_are_rp2_only():
    m0 = copy.deepcopy(real()[2])
    m0["pins"].update(spi_sck=24, spi_mosi=23, spi_miso=22)
    with pytest.raises(NodeConfigError, match="only supported on RP2040/RP2350"):
        check_node(2, m0)


class Recorder:
    '''Stands in for the flashing functions and records which one run() chose.'''
    def __init__(self, monkeypatch, board, nodes):
        self.calls = []
        monkeypatch.setattr(node_setup, "load_nodes", lambda *a, **k: nodes)
        monkeypatch.setattr(node_setup, "detect", lambda args: board)
        for name in ("update_circuitpython", "install_circuitpython", "flash_arduino_uf2", "update_arduino"):
            monkeypatch.setattr(node_setup, name, self._record(name))
        monkeypatch.setattr(node_setup, "confirm_running", lambda *a, **k: self.calls.append("confirm"))

    def _record(self, name):
        def call(*args, **kwargs):
            self.calls.append(name)
            return True
        return call


def args(**kw):
    import types
    return types.SimpleNamespace(**{"node": None, "install": False, "port": None, "dry_run": False,
                                    "nodes": "nodes.json", **kw})


def test_a_circuitpython_pico_set_to_arduino_is_flashed_by_uf2(monkeypatch):
    nodes = {9: rp2_node()}
    r = Recorder(monkeypatch, node_setup.Board("circuitpython", 9), nodes)
    node_setup.run(args())
    assert r.calls == ["flash_arduino_uf2", "confirm"]


def test_an_arduino_pico_set_to_circuitpython_gets_circuitpython_installed(monkeypatch):
    nodes = {9: dict(real()[1], board="pico")}
    r = Recorder(monkeypatch, node_setup.Board("arduino", 9, port="/dev/ttyACM0"), nodes)
    node_setup.run(args())
    assert r.calls == ["install_circuitpython", "confirm"]


def test_a_circuitpython_node_is_just_updated(monkeypatch):
    r = Recorder(monkeypatch, node_setup.Board("circuitpython", 1), real())
    node_setup.run(args())
    assert r.calls == ["update_circuitpython", "confirm"]


def test_the_m0_is_still_uploaded_over_serial(monkeypatch):
    r = Recorder(monkeypatch, node_setup.Board("arduino", 2, port="/dev/ttyACM0"), real())
    node_setup.run(args())
    assert r.calls == ["update_arduino", "confirm"]


def test_a_board_in_its_bootloader_needs_to_be_told_which_node_it_is(monkeypatch):
    Recorder(monkeypatch, node_setup.Board("bootloader", label="RPI-RP2"), real())
    with pytest.raises(node_setup.SetupError, match="--node"):
        node_setup.run(args())


def test_entering_the_bootloader_uses_the_console_or_a_1200_baud_touch(monkeypatch):
    typed, touched = [], []
    monkeypatch.setattr(node_setup, "run_on_console", lambda lines: typed.extend(lines) or "")
    monkeypatch.setattr(node_setup, "touch_1200", touched.append)
    monkeypatch.setattr(node_setup, "wait_until", lambda check, timeout: "RPI-RP2")
    monkeypatch.setattr(node_setup, "bootloader_drive", lambda: None)

    assert node_setup.enter_bootloader(node_setup.Board("circuitpython", 9)) == "RPI-RP2"
    assert "microcontroller.on_next_reset(microcontroller.RunMode.UF2)" in typed and not touched

    assert node_setup.enter_bootloader(node_setup.Board("arduino", 9, port="/dev/ttyACM0")) == "RPI-RP2"
    assert touched == ["/dev/ttyACM0"]


def test_a_uf2_for_the_wrong_chip_is_refused(tmp_path):
    with pytest.raises(node_setup.SetupError, match="rp2350"):
        node_setup.copy_uf2("RP2350", tmp_path / "firmware.uf2", 9, rp2_node())


# ── A bad entry, and the bench files ─────────────────────────────────────────

def test_a_bad_entry_is_skipped_for_main_py_but_reported_by_default(tmp_path):
    path = tmp_path / "nodes.json"
    bad = dict(real()[1], board="feather_esp32s2")          # CircuitPython isn't supported there
    path.write_text(json.dumps({"nodes": {"1": real()[1], "13": bad}}))
    with pytest.raises(NodeConfigError, match="Node 13"):
        load_nodes(path)
    assert list(load_nodes(path, skip_invalid=True)) == [1]   # the radio loop keeps node 1


def test_bench_files_are_valid_and_match_each_other():
    from pathlib import Path
    folder = Path(__file__).parent.parent / "hardware_tests"
    arduino = load_nodes(folder / "bench_nodes_arduino.json")
    circuitpython = load_nodes(folder / "bench_nodes_circuitpython.json")
    assert list(arduino) == list(circuitpython)
    assert all(arduino[n]["board"] == circuitpython[n]["board"] for n in arduino)
    assert not set(arduino) & set(real())                    # no clash with real nodes
