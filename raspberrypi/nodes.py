'''
Python 3 running on Raspberry Pi 3B

nodes.json (at the repo root) is the one list of radio nodes and their
settings. This module loads and checks it, and turns a node's entry into
what each side needs:
  - main.py:       node IDs, sleep windows, which nodes keep a log to sync
  - CircuitPython: the node_config.py that node_setup.py puts on CIRCUITPY
  - Arduino:       -D build flags that override board_config_*.h's defaults

Written by Fletcher Meyers
October 2026
'''

import json
import re
from pathlib import Path

NODES_FILE = Path(__file__).parent.parent / "nodes.json"

FRAMEWORKS = ("circuitpython", "arduino")
STORAGE    = ("sd", "flash", "none")

# Boards a node can be, by the "board" value in nodes.json:
#   chip          — which UF2 bootloader drive it shows (BOOTLOADER_DRIVES)
#   circuitpython — its board ID on circuitpython.org, or None if not supported
#   arduino       — its PlatformIO environment (platformio.ini), or None until
#                   it has a board_config_*.h
BOARDS = {
    "pico":   {"chip": "rp2040", "circuitpython": "raspberry_pi_pico",    "arduino": "pico"},
    "picow":  {"chip": "rp2040", "circuitpython": "raspberry_pi_pico_w",  "arduino": "picow"},
    "pico2":  {"chip": "rp2350", "circuitpython": "raspberry_pi_pico2",   "arduino": "pico2"},
    "pico2w": {"chip": "rp2350", "circuitpython": "raspberry_pi_pico2_w", "arduino": "pico2w"},
    "feather_rp2040_adalogger": {"chip": "rp2040",
                                 "circuitpython": "adafruit_feather_rp2040_adalogger",
                                 "arduino": "feather_rp2040_adalogger"},
    # ESP32 boards are Arduino-only here: CircuitPython on them needs the
    # TinyUF2 bootloader installed first (not on every board as shipped).
    "feather_esp32s2":  {"chip": "esp32s2", "circuitpython": None,                       "arduino": None},
    "feather_esp32_v2": {"chip": "esp32",   "circuitpython": None,                       "arduino": None},
    "feather_m0":       {"chip": "samd21",  "circuitpython": None,                       "arduino": "feather_m0"},
}

# The drive a board shows while it waits in its UF2 bootloader, by volume label.
BOOTLOADER_DRIVES = {"RPI-RP2": "rp2040", "RP2350": "rp2350"}

# The CircuitPython that node_setup.py installs. Its major version must match
# the .mpy libraries in circuitpython/lib.
CIRCUITPYTHON_VERSION = "10.3.1"

# Pins each framework needs. CircuitPython pins are board pin names ("GP22");
# Arduino pins are numbers. Optional pins can be left out: sd_cs for a card,
# sd_sck/sd_mosi/sd_miso when the card has its own SPI bus (the Adalogger's
# built-in slot), i2c_scl/i2c_sda instead of the board's STEMMA QT port.
REQUIRED_PINS = {
    "circuitpython": ("spi_sck", "spi_mosi", "spi_miso", "radio_cs", "radio_rst"),
    "arduino":       ("radio_cs", "radio_irq", "radio_rst"),
}
OPTIONAL_PINS = ("spi_sck", "spi_mosi", "spi_miso",
                 "sd_cs", "sd_sck", "sd_mosi", "sd_miso", "i2c_scl", "i2c_sda")
RP2_CHIPS = ("rp2040", "rp2350")   # boards flashed by copying a UF2 to their bootloader drive
RTCS = ("pcf8523",)
_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class NodeConfigError(ValueError):
    pass


def load_nodes(path=NODES_FILE):
    '''Return {node_id: settings} from nodes.json, checked; raises NodeConfigError.'''
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError) as e:
        raise NodeConfigError(f"Could not read {path}: {e}")
    raw = data.get("nodes") if isinstance(data, dict) else None
    if not isinstance(raw, dict) or not raw:
        raise NodeConfigError(f'{path} has no "nodes" section')

    nodes = {}
    for key, node in raw.items():
        try:
            node_id = int(key)
        except ValueError:
            raise NodeConfigError(f'Node ID "{key}" is not a number')
        if not 1 <= node_id <= 254:
            raise NodeConfigError(f"Node ID {node_id} must be 1-254")
        check_node(node_id, node)
        nodes[node_id] = node
    return dict(sorted(nodes.items()))


def check_node(node_id, node):
    '''Raise NodeConfigError if one node's settings are incomplete or contradictory.'''
    def fail(msg):
        raise NodeConfigError(f"Node {node_id}: {msg}")

    if not isinstance(node, dict):
        fail("settings must be an object")
    for field in ("name", "framework", "board"):
        if not isinstance(node.get(field), str) or not node[field]:
            fail(f'needs a "{field}"')
    framework = node["framework"]
    if framework not in FRAMEWORKS:
        fail(f'framework must be one of {", ".join(FRAMEWORKS)}, not "{framework}"')
    board = BOARDS.get(node["board"])
    if board is None:
        fail(f'unknown board "{node["board"]}" (known: {", ".join(BOARDS)})')
    if board[framework] is None:
        able = [name for name, b in BOARDS.items() if b[framework]]
        fail(f'{framework} on {node["board"]} isn\'t supported yet (it is on: {", ".join(able)})')

    if "color" in node and not (isinstance(node["color"], str) and _COLOR.match(node["color"])):
        fail('color must look like "#79c0ff"')
    battery = node.get("battery")
    if battery is not None and not (isinstance(battery, dict)
                                    and isinstance(battery.get("type"), str)
                                    and isinstance(battery.get("label"), str)):
        fail('battery must be {"type": <sensor type, e.g. "pw0">, "label": <text>}')

    for field in ("sense_interval_s", "log_interval_s"):
        value = node.get(field)
        if not isinstance(value, int) or value <= 0:
            fail(f'"{field}" must be a whole number of seconds above 0')

    storage = node.get("storage", "sd")
    if storage not in STORAGE:
        fail(f'storage must be one of {", ".join(STORAGE)}, not "{storage}"')
    if storage == "flash" and (framework != "arduino" or board["chip"] != "samd21"):
        fail('storage "flash" is only for Arduino SAMD boards (feather_m0)')
    if "rtc" in node and node["rtc"] not in RTCS:
        fail(f'rtc must be one of {", ".join(RTCS)}, or left out for no RTC chip')

    pins = node.get("pins")
    if not isinstance(pins, dict):
        fail('needs a "pins" section')
    for pin in REQUIRED_PINS[framework]:
        if pin not in pins:
            fail(f'needs pin "{pin}"')
    if storage == "sd" and "sd_cs" not in pins:
        fail('storage "sd" needs pin "sd_cs"')
    for pin, value in pins.items():
        if pin not in REQUIRED_PINS[framework] and pin not in OPTIONAL_PINS:
            fail(f'unknown pin "{pin}"')
        if framework == "arduino" and not isinstance(value, int):
            fail(f'Arduino pin "{pin}" must be a number, not {value!r}')
        if framework == "circuitpython" and not isinstance(value, str):
            fail(f'CircuitPython pin "{pin}" must be a board pin name like "GP22", not {value!r}')
    if ("i2c_scl" in pins) != ("i2c_sda" in pins):
        fail('give both "i2c_scl" and "i2c_sda", or neither (STEMMA QT port)')
    sd_bus = [p for p in ("sd_sck", "sd_mosi", "sd_miso") if p in pins]
    if sd_bus and len(sd_bus) != 3:
        fail('give all of "sd_sck", "sd_mosi", "sd_miso" (a separate SD bus), or none')
    radio_bus = [p for p in ("spi_sck", "spi_mosi", "spi_miso") if p in pins]
    if framework == "arduino":
        if radio_bus and len(radio_bus) != 3:
            fail('give all of "spi_sck", "spi_mosi", "spi_miso", or none (the board\'s default SPI pins)')
        if (radio_bus or sd_bus) and board["chip"] not in RP2_CHIPS:
            fail("custom SPI pins are only supported on RP2040/RP2350 Arduino boards")

    window = node.get("sleep_window")
    if window is not None:
        if (not isinstance(window, list) or len(window) != 2
                or not all(isinstance(t, str) and _HHMM.match(t) for t in window)):
            fail('sleep_window must be ["HH:MM", "HH:MM"]')


# ── What main.py needs ───────────────────────────────────────────────────────

def sleep_windows(nodes):
    return {n: tuple(node["sleep_window"]) for n, node in nodes.items() if node.get("sleep_window")}


def sync_node_ids(nodes):
    '''Nodes that keep a reading log for the Pi to pull.'''
    return [n for n, node in nodes.items() if node.get("storage", "sd") != "none"]


# ── What the nodes need ──────────────────────────────────────────────────────

def circuitpython_config(node_id, node):
    '''The node_config.py that hardware_setup_garden.py imports on a CircuitPython node.'''
    pins = ",\n".join(f"    {k!r}: {v!r}" for k, v in node["pins"].items())
    return (
        f"# node_config.py: written by node_setup.py on the Pi from nodes.json\n"
        f"# ({node['name']}). Change nodes.json and run node_setup.py again rather\n"
        f"# than editing this file, or the next update will overwrite it.\n"
        f"NODE_ID = {node_id}\n"
        f"SENSE_INTERVAL = {node['sense_interval_s']}\n"
        f"LOG_INTERVAL = {node['log_interval_s']}\n"
        f"USE_SD = {node.get('storage', 'sd') == 'sd'}\n"
        f"PINS = {{\n{pins},\n}}\n"
    )


def circuitpython_uf2_url(node, version=CIRCUITPYTHON_VERSION):
    '''Where circuitpython.org publishes the UF2 for this node's board.'''
    board = BOARDS[node["board"]]["circuitpython"]
    return (f"https://downloads.circuitpython.org/bin/{board}/en_US/"
            f"adafruit-circuitpython-{board}-en_US-{version}.uf2")


def arduino_env(node):
    return BOARDS[node["board"]]["arduino"]


def arduino_build_flags(node_id, node):
    '''-D flags for PlatformIO, overriding board_config_*.h's defaults for this node.'''
    pins = node["pins"]
    storage = node.get("storage", "sd")
    flags = [
        f"-D NODE_ID={node_id}",
        f"-D RFM69_CS={pins['radio_cs']}",
        f"-D RFM69_INT={pins['radio_irq']}",
        f"-D RFM69_RST={pins['radio_rst']}",
        f"-D DEFAULT_SENSE_INTERVAL_MS={node['sense_interval_s'] * 1000}UL",
        f"-D LOG_INTERVAL_MS={node['log_interval_s'] * 1000}UL",
        f"-D LOG_BACKEND={ {'sd': 'LOG_SD', 'flash': 'LOG_FLASH_SAMD', 'none': 'LOG_NONE'}[storage] }",
    ]
    if storage == "sd":
        flags.append(f"-D SD_CS={pins['sd_cs']}")
        if "sd_sck" in pins:      # a card on its own bus (the Adalogger's slot)
            flags += [f"-D SD_SPI_SCK={pins['sd_sck']}", f"-D SD_SPI_MOSI={pins['sd_mosi']}",
                      f"-D SD_SPI_MISO={pins['sd_miso']}"]
    if "spi_sck" in pins:         # radio on other pins than the board's default SPI ones
        flags += [f"-D RADIO_SPI_SCK={pins['spi_sck']}", f"-D RADIO_SPI_MOSI={pins['spi_mosi']}",
                  f"-D RADIO_SPI_MISO={pins['spi_miso']}"]
    if node.get("rtc") == "pcf8523":
        flags.append("-D BOARD_HAS_PCF8523")
    return flags
