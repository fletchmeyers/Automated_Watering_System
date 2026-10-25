'''
Python 3 running on Raspberry Pi 3B

nodes.json is the one list of nodes and their settings. It lives on the
Pi only (raspberrypi/nodes.json, next to sensors.db; never in git, but in
the backups), and `python3 node_setup.py add` adds to it. The first time
it's needed it's started from the nodes.json that used to sit at the repo
root if that's still there, or else from nodes.example.json.

This module loads and checks it, and turns a node's entry into what each
side needs:
  - radio_nodes.py:       node IDs, sleep windows, which nodes keep a log to sync
  - wifi_nodes.py: which nodes may connect over Wi-Fi
  - flask_api.py:  names, colors and battery labels for the dashboard
  - CircuitPython: the node_config.py that node_setup.py puts on CIRCUITPY
  - Arduino:       -D build flags that override board_config_*.h's defaults

Written by Fletcher Meyers
October 2026
'''

import json
import os
import re
import tempfile
from pathlib import Path

NODES_FILE   = Path(__file__).parent / "nodes.json"
EXAMPLE_FILE = Path(__file__).parent / "nodes.example.json"
LEGACY_FILE  = Path(__file__).parent.parent / "nodes.json"   # where it was until Oct 2026

FRAMEWORKS = ("circuitpython", "arduino")
RADIO_FORMATS = ("json", "msgpack")
STORAGE    = ("sd", "flash", "none")

# How a node talks to the Pi: the RFM69 radio (radio_nodes.py), or Wi-Fi to the
# garden-wifi service (wifi_nodes.py). Wi-Fi nodes need no radio pins and
# keep no log by default (nothing to sync once the Pi is reachable again).
# Any node can have a nightly sleep_window: the sleep command goes over the
# link in use, and Wi-Fi sleeps along with the radio. Wi-Fi needs a board
# with a Wi-Fi chip whose framework can use it there (WIFI_SUPPORT).
#
# A node with a sleep_window and a LiPo on a MAX17048 gauge ("batt"
# readings: its battery is "batt", or not given) also sleeps through the day
# when its battery is low: below "low_battery_sleep" percent (default
# LOW_BATTERY_SLEEP), the Pi sends it back to sleep until a check-in just
# before its next window (communication_indoor.SleepScheduler). false turns
# that off.
#
# A radio node can send over the radio in MessagePack instead of JSON:
# "radio_format": "msgpack" (RADIO_FORMATS; JSON if not given). The same
# readings, about a quarter smaller, so more fit in a 60-byte radio packet.
# The Pi tells the node with every poll (link_plan's "m"), so it changes
# with no re-flash, and back again just as easily; USB and Wi-Fi stay JSON.
#
# A node with both links can have "battery_saver": true, which keeps only
# the link in use powered (arduino/src/link_power.h): with the radio first,
# Wi-Fi stays off, unless "wifi_check_hours": N turns it on every N hours to
# check it; with Wi-Fi first, the radio only comes on once Wi-Fi goes quiet.
#
# "link" is one of LINKS, or a list of both in order of preference
# (["wifi", "radio"]): the node answers on both, and the Pi uses the first
# that's up (links.py). node_setup.py prefer N <link> reorders it.
LINKS = ("radio", "wifi")
# Boards with a Wi-Fi chip, and the frameworks whose node code can use it
# there (CircuitPython only on the Pico W boards: ESP32 boards are Arduino
# only here, and the M0's Wi-Fi module needs Arduino's WiFi101).
WIFI_SUPPORT = {
    "picow":            ("arduino", "circuitpython"),
    "pico2w":           ("arduino", "circuitpython"),
    "feather_esp32s2":  ("arduino",),
    "feather_esp32_v2": ("arduino",),
    "feather_m0_wifi":  ("arduino",),
}
WIFI_BOARDS = tuple(WIFI_SUPPORT)

# Boards where Wi-Fi takes a real share of a small flash, so node_setup.py
# says what it costs and asks before putting it on: board -> the cost.
WIFI_COST = {
    "feather_m0_wifi": "its WiFi101 library takes about 18 KB of the M0's 256 KB flash "
                       "(the firmware goes from about 95 KB to 113 KB)",
}

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
    "feather_esp32s2":  {"chip": "esp32s2", "circuitpython": None,                       "arduino": "feather_esp32s2"},
    "feather_esp32_v2": {"chip": "esp32",   "circuitpython": None,                       "arduino": "feather_esp32_v2"},
    "feather_m0":       {"chip": "samd21",  "circuitpython": None,                       "arduino": "feather_m0"},
    # The Feather M0 WiFi (Adafruit 3044): a Feather M0 with an ATWINC1500
    # Wi-Fi module. Over USB it looks just like a plain one.
    "feather_m0_wifi":  {"chip": "samd21",  "circuitpython": None,                       "arduino": "feather_m0_wifi"},
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
ESP32_CHIPS = ("esp32", "esp32s2")
RTCS = ("pcf8523",)
_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")

# Soil sensor calibration, per sensor ("s0", "s1", ...): the raw reading in
# dry soil and in just-watered soil, which the dashboard shows as 0% and
# 100%. Set with node_setup.py calibrate; either may be missing until it's
# measured (the dashboard's usual scale fills in). The seesaw sensor reads
# 0-1023ish, higher when wetter, but either way round works.
#
# While the Pi calibrates a sensor by itself (soil_calibration.py: put it in
# dry soil, then water), its entry says so instead:
# {"auto": {"since": <ISO time>, "watered": <ISO time it saw the watering, or null>}}.
SOIL_SENSOR = re.compile(r"^s\d$")
SOIL_POINTS = ("dry", "wet")
_ISO = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$")

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class NodeConfigError(ValueError):
    pass


def _is_pi_list(path):
    return Path(path).resolve() == Path(NODES_FILE).resolve()


def start_pi_list():
    '''Create the Pi's nodes.json if it isn't there yet: a copy of the old
    repo-root nodes.json if one is left, otherwise of nodes.example.json.'''
    if Path(NODES_FILE).exists():
        return
    source = Path(LEGACY_FILE) if Path(LEGACY_FILE).exists() else Path(EXAMPLE_FILE)
    write_atomic(NODES_FILE, source.read_text(encoding="utf-8"))
    print(f"[NODES] Started the node list at {NODES_FILE} from {source}.")


def write_atomic(path, text):
    '''Write a file so that a reader (radio_nodes.py, the API) never sees it half-written.'''
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_node_file(path=None):
    '''The whole file as a dict, unchecked, for editing it. A file other than
    the Pi's own list that doesn't exist yet reads as empty.'''
    path = Path(path or NODES_FILE)
    if _is_pi_list(path):
        start_pi_list()
    elif not path.exists():
        return {"nodes": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise NodeConfigError(f"Could not read {path}: {e}")
    if not isinstance(data, dict) or not isinstance(data.setdefault("nodes", {}), dict):
        raise NodeConfigError(f'{path} has no "nodes" section')
    return data


def save_node_file(data, path=None):
    '''Write the whole file back, nodes in ID order.'''
    data = dict(data)
    data["nodes"] = {str(k): v for k, v in sorted(data["nodes"].items(), key=lambda kv: int(kv[0]))}
    write_atomic(path or NODES_FILE, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def load_nodes(path=None, skip_invalid=False):
    '''
    Return {node_id: settings} from nodes.json (the Pi's own list unless
    path says otherwise), checked; raises NodeConfigError. With
    skip_invalid, a node with a mistake in it is left out (with a warning)
    instead — so radio_nodes.py keeps polling the others.
    '''
    path = path or NODES_FILE
    if _is_pi_list(path):
        start_pi_list()
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
            try:
                node_id = int(key)
            except ValueError:
                raise NodeConfigError(f'Node ID "{key}" is not a number')
            if not 1 <= node_id <= 254:
                raise NodeConfigError(f"Node ID {node_id} must be 1-254")
            check_node(node_id, node)
        except NodeConfigError as e:
            if not skip_invalid:
                raise
            print(f"[NODES] Skipping a node in {Path(path).name}: {e}")
            continue
        nodes[node_id] = node
    if not nodes:
        raise NodeConfigError(f"{path} has no usable nodes")
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

    raw = node.get("link", "radio")
    links = links_of(node)
    if (isinstance(raw, list) and (not raw or len(set(raw)) != len(raw)))             or not all(isinstance(l, str) and l in LINKS for l in links):
        fail(f'link must be one of {", ".join(LINKS)}, or a list of them in order of '
             f'preference, not {json.dumps(raw)}')
    if "wifi" in links:
        if not wifi_able(node["board"], framework):
            able = [f"{b} ({'/'.join(f)})" for b, f in WIFI_SUPPORT.items()]
            fail(f"{framework} on {node['board']} can't use Wi-Fi (it can on: {', '.join(able)})")

    saver = node.get("battery_saver", False)
    if not isinstance(saver, bool):
        fail("battery_saver must be true or false")
    if saver and len(links) < 2:
        fail("battery_saver is for nodes with both Wi-Fi and the radio (it keeps only one on). "
             f"Turn it off with: python3 node_setup.py saver {node_id} off")
    hours = node.get("wifi_check_hours")
    if hours is not None:
        if not isinstance(hours, int) or isinstance(hours, bool) or hours <= 0:
            fail("wifi_check_hours must be a whole number of hours above 0")
        if not saver:
            fail("wifi_check_hours goes with battery_saver (without it Wi-Fi is always on). "
                 f"Turn both on with: python3 node_setup.py saver {node_id} on --wifi-check-hours {hours}")

    storage = storage_of(node)
    if storage not in STORAGE:
        fail(f'storage must be one of {", ".join(STORAGE)}, not "{storage}"')
    if storage == "flash" and (framework != "arduino" or board["chip"] != "samd21"):
        fail('storage "flash" is only for Arduino SAMD boards (feather_m0)')
    if "rtc" in node and node["rtc"] not in RTCS:
        fail(f'rtc must be one of {", ".join(RTCS)}, or left out for no RTC chip')

    pins = node.get("pins", None if "radio" in links else {})
    if not isinstance(pins, dict):
        fail('needs a "pins" section')
    required = REQUIRED_PINS[framework] if "radio" in links else ()
    for pin in required:
        if pin not in pins:
            fail(f'needs pin "{pin}"')
    if storage == "sd" and "sd_cs" not in pins:
        fail('storage "sd" needs pin "sd_cs"')
    if (framework == "circuitpython" and storage == "sd" and "sd_sck" not in pins
            and not all(p in pins for p in ("spi_sck", "spi_mosi", "spi_miso"))):
        fail('the SD card shares the radio\'s SPI bus here, so it needs "spi_sck", "spi_mosi" '
             'and "spi_miso" (or its own bus: "sd_sck", "sd_mosi", "sd_miso")')
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
        if radio_bus and board["chip"] not in RP2_CHIPS + ESP32_CHIPS:
            fail("custom radio SPI pins are only supported on RP2040/RP2350 and ESP32 boards")
        if sd_bus and board["chip"] not in RP2_CHIPS:
            fail("a separate SD SPI bus is only supported on RP2040/RP2350 boards")

    soil = node.get("soil")
    if soil is not None:
        if not isinstance(soil, dict):
            fail('soil must be {"s0": {"dry": <raw>, "wet": <raw>}, ...}')
        for sensor, points in soil.items():
            if not SOIL_SENSOR.match(sensor):
                fail(f'soil sensors are named "s0", "s1", ..., not "{sensor}"')
            if isinstance(points, dict) and set(points) == {"auto"}:
                auto = points["auto"]
                if not (isinstance(auto, dict) and set(auto) <= {"since", "watered"}
                        and isinstance(auto.get("since"), str) and _ISO.match(auto["since"])
                        and (auto.get("watered") is None or
                             (isinstance(auto["watered"], str) and _ISO.match(auto["watered"])))):
                    fail(f'soil "{sensor}" auto must be {{"since": "YYYY-MM-DDTHH:MM:SS", "watered": ... or null}}')
                continue
            if not isinstance(points, dict) or not set(points) <= set(SOIL_POINTS) or not all(
                    isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 65535
                    for v in points.values()):
                fail(f'soil "{sensor}" must be {{"dry": <raw reading>, "wet": <raw reading>}}')
            if points.get("dry") is not None and points.get("dry") == points.get("wet"):
                fail(f'soil "{sensor}": dry and wet must be different readings')

    window = node.get("sleep_window")
    if window is not None:
        if (not isinstance(window, list) or len(window) != 2
                or not all(isinstance(t, str) and _HHMM.match(t) for t in window)):
            fail('sleep_window must be ["HH:MM", "HH:MM"]')

    fmt = node.get("radio_format", "json")
    if fmt not in RADIO_FORMATS:
        fail(f'radio_format must be one of {", ".join(RADIO_FORMATS)}, not {json.dumps(fmt)}')
    if fmt != "json" and "radio" not in links:
        fail("radio_format is for nodes with the radio (Wi-Fi and USB always use JSON). "
             f"Set it back first with: python3 node_setup.py format {node_id} json")

    low = node.get("low_battery_sleep")
    if low is not None and low is not False:
        if not isinstance(low, int) or isinstance(low, bool) or not 1 <= low <= 99:
            fail("low_battery_sleep must be a battery percentage from 1 to 99, or false")
        if window is None:
            fail("low_battery_sleep needs a sleep_window (the node checks in just before it)")
        if (node.get("battery") or {}).get("type", "batt") != "batt":
            fail('low_battery_sleep needs a MAX17048 battery gauge (battery type "batt")')


def wifi_able(board, framework):
    '''Whether a node on this board and framework can be a Wi-Fi node.'''
    return framework in WIFI_SUPPORT.get(board, ())


def links_of(node):
    '''A node's links in order of preference: ["radio"], ["wifi"], or both.'''
    link = node.get("link", "radio")
    return list(link) if isinstance(link, list) else [link]


def link_of(node):
    '''Its preferred link (see links.active_link for the one in use now).'''
    return links_of(node)[0]


def storage_of(node):
    '''Where a node logs readings: an SD card unless it says otherwise, or
    nothing at all for a Wi-Fi-only node unless it says otherwise.'''
    return node.get("storage", "sd" if "radio" in links_of(node) else "none")


# ── What radio_nodes.py and wifi_nodes.py need ──────────────────────────────────────

def radio_node_ids(nodes):
    return [n for n, node in nodes.items() if "radio" in links_of(node)]


def wifi_node_ids(nodes):
    return [n for n, node in nodes.items() if "wifi" in links_of(node)]


def sleep_windows(nodes):
    return {n: tuple(node["sleep_window"]) for n, node in nodes.items() if node.get("sleep_window")}


LOW_BATTERY_SLEEP = 25   # percent: below this, a node with a LiPo gauge sleeps through the day


def low_battery_sleep(nodes):
    '''{node: percent} for the nodes that sleep through the day when their
    battery is below that: a sleep_window and a MAX17048 ("batt") gauge,
    unless "low_battery_sleep" is false.'''
    levels = {}
    for n, node in nodes.items():
        level = node.get("low_battery_sleep", LOW_BATTERY_SLEEP)
        if (node.get("sleep_window") and level is not False
                and (node.get("battery") or {}).get("type", "batt") == "batt"):
            levels[n] = level
    return levels


def sync_node_ids(nodes):
    '''Radio nodes that keep a reading log for the Pi to pull.'''
    return [n for n in radio_node_ids(nodes) if storage_of(nodes[n]) != "none"]


def link_plan(node):
    '''What polls tell a node about its links ("lp"), packed to fit a radio
    packet (see arduino/src/link_power.h). For a node with both: r or w for
    the link that comes first, a capital letter for battery saver, then the
    Wi-Fi check hours if any. Then "m" if it should send MessagePack over
    the radio (radio_format). None if there's nothing to say.'''
    links = links_of(node)
    code = ""
    if len(links) > 1:
        code = "w" if links[0] == "wifi" else "r"
        if node.get("battery_saver"):
            code = code.upper() + (str(node["wifi_check_hours"]) if node.get("wifi_check_hours") else "")
    if node.get("radio_format") == "msgpack" and "radio" in links:
        code += "m"
    return code or None


def next_free_id(taken):
    '''The lowest node ID (1-254) not in taken, or None if they're all used.'''
    taken = {int(n) for n in taken}
    return next((n for n in range(1, 255) if n not in taken), None)


# ── What the dashboard needs (flask_api.py's /api/nodes) ─────────────────────

PUBLIC_FIELDS = ("name", "short", "color", "battery", "board", "framework", "soil", "radio_format")


def public_nodes(nodes):
    '''How to show each node: no pins or other wiring.'''
    return {n: {**{k: node[k] for k in PUBLIC_FIELDS if k in node},
                "link": link_of(node), "links": links_of(node)}
            for n, node in nodes.items()}


# ── What the nodes need ──────────────────────────────────────────────────────

def circuitpython_config(node_id, node):
    '''The node_config.py that hardware_setup_garden.py imports on a CircuitPython node.'''
    pins = "".join(f"    {k!r}: {v!r},\n" for k, v in node.get("pins", {}).items())   # none: Wi-Fi only
    return (
        f"# node_config.py: written by node_setup.py on the Pi from nodes.json\n"
        f"# ({node['name']}). Change nodes.json and run node_setup.py again rather\n"
        f"# than editing this file, or the next update will overwrite it.\n"
        f"NODE_ID = {node_id}\n"
        f"SENSE_INTERVAL = {node['sense_interval_s']}\n"
        f"LOG_INTERVAL = {node['log_interval_s']}\n"
        f"USE_SD = {storage_of(node) == 'sd'}\n"
        f"PINS = {{\n{pins}}}\n"
        f"LINKS = {links_of(node)!r}\n"
        f"BATTERY_SAVER = {bool(node.get('battery_saver'))}\n"
        f"WIFI_CHECK_HOURS = {node.get('wifi_check_hours') or 0}\n"
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
    pins = node.get("pins", {})
    storage = storage_of(node)
    flags = [f"-D NODE_ID={node_id}"]
    links = links_of(node)
    if "wifi" in links:
        flags.append("-D NODE_LINK_WIFI")       # see wifi_link.h
    if "radio" not in links:
        flags.append("-D NODE_NO_RADIO")
    else:
        flags += [f"-D RFM69_CS={pins['radio_cs']}", f"-D RFM69_INT={pins['radio_irq']}",
                  f"-D RFM69_RST={pins['radio_rst']}"]
    if len(links) > 1 and links[0] == "wifi":
        flags.append("-D LINK_FIRST_WIFI")      # see link_power.h
    if node.get("battery_saver"):
        flags.append("-D BATTERY_SAVER")
    if node.get("wifi_check_hours"):
        flags.append(f"-D WIFI_CHECK_HOURS={node['wifi_check_hours']}")
    flags += [
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
