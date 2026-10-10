'''
Python 3 running on Raspberry Pi 3B

The questions node_setup.py asks to add a node to the Pi's nodes.json
(`python3 node_setup.py add`, or when the board plugged in isn't a node
yet). Every question has a default: just press Enter for the usual choice.

    board -> framework -> radio or Wi-Fi -> node ID -> name -> where it logs
    -> pins (the board's usual wiring, or your own) -> intervals -> clock chip
    -> sleep window -> save

The node ID offered is the lowest one that isn't in the list and has no
readings in sensors.db, so a new node never shares an old one's history.
Anything not asked here (a battery label, a different color) can be edited
in nodes.json afterwards; node_setup.py checks it either way.

Written by Fletcher Meyers
October 2026
'''

import sqlite3

from nodes import (NodeConfigError, BOARDS, ESP32_CHIPS, REQUIRED_PINS, check_node,
                   next_free_id, read_node_file, save_node_file)

# Each board's usual wiring, per framework: an RFM69 FeatherWing / breakout
# as in board_config_*.h (Arduino) or hardware_setup_garden.py
# (CircuitPython), and the SD card where the board has one built in.
DEFAULT_PINS = {
    "arduino": {
        "pico":   {"radio_cs": 17, "radio_irq": 21, "radio_rst": 20},
        "picow":  {"radio_cs": 17, "radio_irq": 21, "radio_rst": 20},
        "pico2":  {"radio_cs": 17, "radio_irq": 21, "radio_rst": 20},
        "pico2w": {"radio_cs": 17, "radio_irq": 21, "radio_rst": 20},
        "feather_rp2040_adalogger": {"radio_cs": 10, "radio_irq": 6, "radio_rst": 11,
                                     "sd_cs": 23, "sd_sck": 18, "sd_mosi": 19, "sd_miso": 20},
        "feather_esp32s2":  {"radio_cs": 10, "radio_irq": 6, "radio_rst": 11},
        "feather_esp32_v2": {"radio_cs": 33, "radio_irq": 27, "radio_rst": 15},
        "feather_m0":       {"radio_cs": 9, "radio_irq": 6, "radio_rst": 11, "sd_cs": 10},
    },
    "circuitpython": {
        **{board: {"spi_sck": "GP18", "spi_mosi": "GP19", "spi_miso": "GP16",
                   "radio_cs": "GP22", "radio_rst": "GP27", "sd_cs": "GP17"}
           for board in ("pico", "picow", "pico2", "pico2w")},
        "feather_rp2040_adalogger": {"spi_sck": "SCK", "spi_mosi": "MOSI", "spi_miso": "MISO",
                                     "radio_cs": "D10", "radio_rst": "D11", "sd_cs": "SD_CS",
                                     "sd_sck": "SD_SCK", "sd_mosi": "SD_MOSI", "sd_miso": "SD_MISO"},
    },
}
SD_PINS = ("sd_cs", "sd_sck", "sd_mosi", "sd_miso")

BOARD_NAMES = {
    "pico": "Pico", "picow": "Pico W", "pico2": "Pico 2", "pico2w": "Pico 2 W",
    "feather_rp2040_adalogger": "Feather RP2040 Adalogger",
    "feather_esp32s2": "ESP32-S2 Feather", "feather_esp32_v2": "ESP32 V2 Feather",
    "feather_m0": "Feather M0",
}

# Plot colors, in the order new nodes get them. Kept clear of the dashboard's
# green/amber/red status colors, so a node never reads as a warning.
COLORS = ["#79c0ff", "#f778ba", "#d2a8ff", "#ffa657", "#a5d6ff", "#56d4dd",
          "#ffb3d9", "#b4a7ff", "#e3b341", "#ff9bce"]


class Cancelled(Exception):
    pass


def ids_with_data(db_file):
    '''Node IDs that have readings in sensors.db (none if it can't be read).'''
    try:
        conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True, timeout=10)
        try:
            return {n for (n,) in conn.execute("SELECT DISTINCT node_id FROM readings") if n is not None}
        finally:
            conn.close()
    except sqlite3.Error:
        return set()


class Wizard:
    '''Asks through ask(prompt) -> the typed line (input() unless a test
    passes its own), says things through say().'''

    def __init__(self, ask=input, say=print):
        self._ask, self.say = ask, say

    def text(self, prompt, default=None):
        line = self._ask(f"  {prompt}" + (f" [{default}]" if default not in (None, "") else "") + ": ").strip()
        if line.lower() in ("q", "quit"):
            raise Cancelled()
        return line or (default if default is not None else "")

    def choose(self, prompt, options, default):
        '''options: [(value, label)]. Accepts the number or the value itself.'''
        if len(options) == 1:
            self.say(f"  {prompt}: {options[0][1]}")
            return options[0][0]
        self.say(f"  {prompt}:")
        for i, (value, label) in enumerate(options, 1):
            self.say(f"    {i}. {label}" + ("   (default)" if value == default else ""))
        values = [v for v, _ in options]
        while True:
            line = self.text("Choose", str(values.index(default) + 1))
            if line.isdigit() and 1 <= int(line) <= len(options):
                return values[int(line) - 1]
            if line in values:
                return line
            self.say(f"    Type a number from 1 to {len(options)}.")

    def yes(self, prompt, default=True):
        while True:
            line = self.text(prompt + (" (Y/n)" if default else " (y/N)")).lower()
            if not line:
                return default
            if line in ("y", "yes", "n", "no"):
                return line.startswith("y")
            self.say("    Type y or n.")

    def number(self, prompt, default, low=1, high=None, allowed=lambda n: None):
        '''allowed(n) returns a reason n can't be used, or None.'''
        while True:
            line = self.text(prompt, str(default))
            if line.isdigit() and int(line) >= low and (high is None or int(line) <= high):
                why = allowed(int(line))
                if why is None:
                    return int(line)
                self.say(f"    {why}")
            else:
                self.say(f"    Type a whole number from {low}" + (f" to {high}." if high else " up."))

    def pin(self, name, framework, default=None):
        while True:
            line = self.text(f"{name} pin" + (" (a number)" if framework == "arduino"
                                               else ' (a board pin name, e.g. "GP22")'), default)
            if framework == "circuitpython" and line:
                return line
            if framework == "arduino" and str(line).isdigit():
                return int(line)
            self.say("    That pin isn't in the right form.")


def new_node(nodes, wizard, boards=None, taken_ids=()):
    '''Ask about a new node; return (node_id, settings), checked. boards: what
    the plugged-in board could be, if that's known (one skips the question).'''
    w = wizard
    w.say("Adding a node. Press Enter to take the default in [brackets], or q to stop.")

    boards = list(boards or BOARDS)
    board = w.choose("Board", [(b, f"{BOARD_NAMES.get(b, b)} ({b})") for b in boards],
                     "pico2w" if "pico2w" in boards else boards[0])
    frameworks = [f for f in ("circuitpython", "arduino") if BOARDS[board][f]]
    framework = w.choose("Framework", [(f, {"circuitpython": "CircuitPython",
                                            "arduino": "Arduino"}[f]) for f in frameworks],
                         frameworks[0])
    wifi_able = framework == "arduino" and BOARDS[board]["chip"] in ESP32_CHIPS
    link = w.choose("How it reaches the Pi",
                    [("wifi", "Wi-Fi (no radio needed; the Pi gives it the Wi-Fi settings)"),
                     ("radio", "RFM69 radio")] if wifi_able else [("radio", "RFM69 radio")],
                    "wifi" if wifi_able else "radio")

    taken = set(nodes) | set(taken_ids)
    suggested = next_free_id(taken)
    if suggested is None:
        raise NodeConfigError("All node IDs 1-254 are in use.")
    def id_free(n):
        if n in nodes:
            return f"Node {n} is already {nodes[n].get('name', 'in the list')}."
        if n in taken_ids:
            return f"Node {n} has old readings in the database; pick another so they don't mix."
        return None
    node_id = w.number("Node ID", suggested, 1, 254, allowed=id_free)

    name = w.text("Name (shown on the dashboard)", f"{BOARD_NAMES.get(board, board)} {node_id}")
    short = w.text("Short name (plot labels)", name.split()[0][:10])

    defaults = DEFAULT_PINS.get(framework, {}).get(board, {})
    storage_options = [("none", "Nothing: readings only reach the Pi when it polls")]
    storage_options.insert(0, ("sd", "SD card (a node that sometimes can't reach the Pi syncs it later)"))
    if framework == "arduino" and BOARDS[board]["chip"] == "samd21":
        storage_options.append(("flash", "The board's own flash chip"))
    storage = w.choose("Where it logs readings", storage_options,
                       "sd" if link == "radio" and "sd_cs" in defaults else "none")

    wanted = list(REQUIRED_PINS[framework]) if link == "radio" else []
    if storage == "sd":
        wanted += [p for p in SD_PINS if p in defaults] or ["sd_cs"]
    pins = {p: defaults[p] for p in wanted if p in defaults}
    if wanted:
        missing = [p for p in wanted if p not in pins]
        if pins:
            w.say("  The board's usual pins: " + ", ".join(f"{p}={v}" for p, v in pins.items()))
        if missing or not w.yes("Use these pins?"):
            for p in wanted:
                pins[p] = w.pin(p, framework, pins.get(p))

    sense = w.number("Seconds between sensor readings", 3)
    log = w.number("Seconds between logged readings" if storage != "none"
                   else "Seconds between readings it keeps for the Pi", 60 if link == "wifi" else 300)

    node = {"name": name, "short": short,
            "color": next((c for c in COLORS if c not in {n.get("color") for n in nodes.values()}),
                          COLORS[node_id % len(COLORS)]),
            "framework": framework, "board": board}
    if link == "wifi":
        node["link"] = "wifi"
    node.update(sense_interval_s=sense, log_interval_s=log, storage=storage)
    if pins:
        node["pins"] = pins
    if w.yes("Does it have a PCF8523 clock chip (RTC)?", default=False):
        node["rtc"] = "pcf8523"
    if link == "radio":
        while True:
            window = w.text("Nightly sleep window, e.g. 19:00-07:00 (Enter for none)", "")
            if not window:
                break
            node["sleep_window"] = [t.strip() for t in window.split("-")]
            try:
                check_node(node_id, node)
                break
            except NodeConfigError:
                del node["sleep_window"]
                w.say("    Write it as HH:MM-HH:MM.")

    check_node(node_id, node)
    return node_id, node


def add_node(path, wizard, boards=None, db_file=None):
    '''Run the questions and save the new node to path. Returns its ID, or
    None if cancelled.'''
    data = read_node_file(path)
    nodes = {int(k): v for k, v in data["nodes"].items()}
    taken = ids_with_data(db_file) if db_file else set()
    try:
        node_id, node = new_node(nodes, wizard, boards, taken)
        wizard.say(f"\n  Node {node_id}: {node['name']}, {node['framework']} on {node['board']}, "
                   f"over {node.get('link', 'radio')}, logging to {node['storage']}")
        if not wizard.yes("Save it?"):
            raise Cancelled()
    except (Cancelled, EOFError, KeyboardInterrupt):
        wizard.say("\n[SETUP] Stopped; nothing was saved.")
        return None
    data["nodes"][str(node_id)] = node
    save_node_file(data, path)
    wizard.say(f"[SETUP] Saved node {node_id} to {path}.")
    return node_id


def remove_node(path, node_id, wizard):
    data = read_node_file(path)
    node = data["nodes"].get(str(node_id))
    if node is None:
        raise NodeConfigError(f"Node {node_id} isn't in {path}.")
    if len(data["nodes"]) == 1:
        raise NodeConfigError("That's the only node in the list; add its replacement first.")
    try:
        if not wizard.yes(f"Remove node {node_id} ({node.get('name')}) from the list? "
                          "Its readings stay in the database", default=False):
            return False
    except (Cancelled, EOFError, KeyboardInterrupt):
        return False
    del data["nodes"][str(node_id)]
    save_node_file(data, path)
    wizard.say(f"[SETUP] Removed node {node_id}.")
    return True
