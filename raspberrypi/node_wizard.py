'''
Python 3 running on Raspberry Pi 3B

The questions node_setup.py asks to add a node to the Pi's nodes.json
(`python3 node_setup.py add`, or when the board plugged in isn't a node
yet). Every question has a default: just press Enter for the usual choice.
Type b to go back a question, or q to stop without saving.

    board -> framework -> radio or Wi-Fi -> node ID -> name -> short name
    -> where it logs -> pins (the board's usual wiring, or your own)
    -> how often it reads its sensors -> how often it logs -> clock chip
    -> sleep window -> review

At the review, every answer is listed with a number: type a number to
change that answer, or press Enter to save.

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
FRAMEWORK_NAMES = {"circuitpython": "CircuitPython", "arduino": "Arduino"}
LINK_NAMES = {"wifi": "Wi-Fi", "radio": "RFM69 radio"}
STORAGE_NAMES = {"sd": "SD card", "flash": "the board's flash chip", "none": "nothing"}

# Plot colors, in the order new nodes get them. Kept clear of the dashboard's
# green/amber/red status colors, so a node never reads as a warning.
COLORS = ["#79c0ff", "#f778ba", "#d2a8ff", "#ffa657", "#a5d6ff", "#56d4dd",
          "#ffb3d9", "#b4a7ff", "#e3b341", "#ff9bce"]

# How often a node with nothing to log keeps a reading for its log: unused,
# but every node needs a value.
UNLOGGED_INTERVAL_S = 60


class Cancelled(Exception):
    pass


class Back(Exception):
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
    passes its own), says things through say(). Typing q stops (Cancelled),
    b goes back a question (Back).'''

    def __init__(self, ask=input, say=print):
        self._ask, self.say = ask, say
        self.asked = 0   # questions actually put to the user

    def text(self, prompt, default=None):
        self.asked += 1
        line = self._ask(f"  {prompt}" + (f" [{default}]" if default not in (None, "") else "") + ": ").strip()
        if line.lower() in ("q", "quit"):
            raise Cancelled()
        if line.lower() in ("b", "back"):
            raise Back()
        return line or (default if default is not None else "")

    def choose(self, prompt, options, default):
        '''options: [(value, label)]. Accepts the number or the value itself.'''
        if len(options) == 1:
            self.say(f"  {prompt}: {options[0][1]}")
            return options[0][0]
        self.say(f"  {prompt}:")
        values = [v for v, _ in options]
        if default not in values:
            default = values[0]
        for i, (value, label) in enumerate(options, 1):
            self.say(f"    {i}. {label}" + ("   (default)" if value == default else ""))
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


class Step:
    '''One question: ask(answers) returns the answer, shown in the review as
    show(answer). Asked only when applies(answers). After changing an
    answer from the review, the later questions are asked again if
    redo_after (their choices depend on it), with the old answers as the
    defaults.'''
    def __init__(self, key, label, ask, show=str, applies=lambda a: True, redo_after=False):
        self.key, self.label, self.ask, self.show = key, label, ask, show
        self.applies, self.redo_after = applies, redo_after


def _steps(w, nodes, boards, taken_ids):
    '''The questions, in order. Each one's default is the earlier answer
    when going back over it, otherwise the usual choice.'''
    boards = list(boards or BOARDS)
    taken = set(nodes) | set(taken_ids)
    suggested = next_free_id(taken)
    if suggested is None:
        raise NodeConfigError("All node IDs 1-254 are in use.")

    def board(a):
        return w.choose("Board", [(b, f"{BOARD_NAMES.get(b, b)} ({b})") for b in boards],
                        a.get("board") or ("pico2w" if "pico2w" in boards else boards[0]))

    def framework(a):
        able = [f for f in FRAMEWORK_NAMES if BOARDS[a["board"]][f]]
        return w.choose("Framework", [(f, FRAMEWORK_NAMES[f]) for f in able], a.get("framework", able[0]))

    def wifi_able(a):
        return a["framework"] == "arduino" and BOARDS[a["board"]]["chip"] in ESP32_CHIPS

    def link(a):
        if not wifi_able(a):
            return w.choose("How it reaches the Pi", [("radio", LINK_NAMES["radio"])], "radio")
        return w.choose("How it reaches the Pi",
                        [("wifi", "Wi-Fi (no radio needed; the Pi gives it the Wi-Fi settings)"),
                         ("radio", LINK_NAMES["radio"])], a.get("link", "wifi"))

    def node_id(a):
        def free(n):
            if n in nodes:
                return f"Node {n} is already {nodes[n].get('name', 'in the list')}."
            if n in taken_ids:
                return f"Node {n} has old readings in the database; pick another so they don't mix."
            return None
        return w.number("Node ID", a.get("node_id", suggested), 1, 254, allowed=free)

    def name(a):
        return w.text("Name (shown on the dashboard)",
                      a.get("name") or f"{BOARD_NAMES.get(a['board'], a['board'])} {a['node_id']}")

    def short(a):
        return w.text("Short name (plot labels)", a.get("short") or a["name"].split()[0][:10])

    def defaults(a):
        return DEFAULT_PINS.get(a["framework"], {}).get(a["board"], {})

    def storage(a):
        options = [("sd", "SD card: readings from while it can't reach the Pi are synced later"),
                   ("none", "Nothing: the Pi only gets readings when it asks")]
        if a["framework"] == "arduino" and BOARDS[a["board"]]["chip"] == "samd21":
            options.append(("flash", "The board's own flash chip"))
        usual = "sd" if a["link"] == "radio" and "sd_cs" in defaults(a) else "none"
        return w.choose("Where it logs readings", options, a.get("storage", usual))

    def wanted_pins(a):
        wanted = list(REQUIRED_PINS[a["framework"]]) if a["link"] == "radio" else []
        if a["storage"] == "sd":
            wanted += [p for p in SD_PINS if p in defaults(a)] or ["sd_cs"]
        return wanted

    def pins(a):
        wanted = wanted_pins(a)
        before = a.get("pins") or {}
        usual = {p: before.get(p, defaults(a).get(p)) for p in wanted}
        usual = {p: v for p, v in usual.items() if v is not None}
        if len(usual) == len(wanted):
            w.say("  Pins: " + ", ".join(f"{p}={v}" for p, v in usual.items()))
            if w.yes("Use these pins?"):
                return usual
        return {p: w.pin(p, a["framework"], usual.get(p)) for p in wanted}

    def sense(a):
        return w.number("Seconds between sensor readings (the Pi gets the latest when it asks)",
                        a.get("sense", 3))

    def log(a):
        return w.number(f"Seconds between readings saved to the {STORAGE_NAMES[a['storage']]}",
                        a.get("log", 300))

    def rtc(a):
        return w.yes("Does it have a PCF8523 clock chip (RTC)?", default=a.get("rtc", False))

    def sleep(a):
        while True:
            window = w.text("Nightly sleep window, e.g. 19:00-07:00 (Enter for none, - to clear)",
                            "-".join(a["sleep"]) if a.get("sleep") else "")
            if window in ("", "-"):
                return None
            times = [t.strip() for t in window.split("-")]
            try:
                check_node(0, {"name": "x", "framework": "arduino", "board": "feather_m0",
                               "sense_interval_s": 1, "log_interval_s": 1, "storage": "none",
                               "pins": {"radio_cs": 9, "radio_irq": 6, "radio_rst": 11},
                               "sleep_window": times})
                return times
            except NodeConfigError:
                w.say("    Write it as HH:MM-HH:MM.")

    return [
        Step("board", "Board", board, show=lambda v: BOARD_NAMES.get(v, v), redo_after=True),
        Step("framework", "Framework", framework, show=FRAMEWORK_NAMES.get, redo_after=True),
        Step("link", "Reaches the Pi by", link, show=LINK_NAMES.get, redo_after=True),
        Step("node_id", "Node ID", node_id),
        Step("name", "Name", name),
        Step("short", "Short name", short),
        Step("storage", "Logs readings to", storage, show=STORAGE_NAMES.get, redo_after=True),
        Step("pins", "Pins", pins, show=lambda v: ", ".join(f"{p}={x}" for p, x in v.items()),
             applies=lambda a: bool(wanted_pins(a))),
        Step("sense", "Reads its sensors every", sense, show=lambda v: f"{v} s"),
        Step("log", "Saves a reading every", log, show=lambda v: f"{v} s",
             applies=lambda a: a["storage"] != "none"),
        Step("rtc", "Clock chip (RTC)", rtc, show=lambda v: "PCF8523" if v else "none"),
        Step("sleep", "Sleep window", sleep, show=lambda v: "-".join(v) if v else "none",
             applies=lambda a: a["link"] == "radio"),
    ]


def _build(nodes, a):
    node = {"name": a["name"], "short": a["short"],
            "color": next((c for c in COLORS if c not in {n.get("color") for n in nodes.values()}),
                          COLORS[a["node_id"] % len(COLORS)]),
            "framework": a["framework"], "board": a["board"]}
    if a["link"] == "wifi":
        node["link"] = "wifi"
    node.update(sense_interval_s=a["sense"],
                log_interval_s=a["log"] if a["storage"] != "none" else UNLOGGED_INTERVAL_S,
                storage=a["storage"])
    if a.get("pins"):
        node["pins"] = a["pins"]
    if a["rtc"]:
        node["rtc"] = "pcf8523"
    if a.get("sleep"):
        node["sleep_window"] = a["sleep"]
    return node


def new_node(nodes, wizard, boards=None, taken_ids=()):
    '''Ask about a new node, then review it; return (node_id, settings),
    checked. boards: what the plugged-in board could be, if that's known
    (one skips the question). Raises Cancelled if the user stops.'''
    w = wizard
    steps = _steps(w, nodes, boards, taken_ids)
    w.say("Adding a node. Press Enter to take the default in [brackets], b to go back a "
          "question, or q to stop.")
    a = {}

    really_asked = set()   # steps that put a question (not one with only one choice)

    def ask_from(i, only_one=False, history=None):
        '''Ask steps[i:] in order (just steps[i] if only_one), with b going
        back to the last question that was really asked (from history, the
        ones before steps[i]), or to the review when changing one answer
        from there.'''
        editing = only_one
        history = list(history or [])
        while i < len(steps):
            step = steps[i]
            if not step.applies(a):
                a.pop(step.key, None)
                i += 1
                continue
            asked_before = w.asked
            try:
                a[step.key] = step.ask(a)
            except Back:
                if history:
                    i = history.pop()
                elif editing:
                    return
                else:
                    w.say("    That's the first question.")
                continue
            if w.asked > asked_before:
                history.append(i)
                really_asked.add(i)
            if only_one and not step.redo_after:
                return
            only_one = False
            i += 1

    ask_from(0)
    while True:
        shown = [s for s in steps if s.applies(a)]
        w.say(f"\n  Node {a['node_id']}, to be saved:")
        for n, s in enumerate(shown, 1):
            w.say(f"    {n:>2}. {s.label + ':':<26}{s.show(a[s.key])}")
        try:
            line = w.text("Press Enter to save it, type a number to change that answer, or q to stop")
        except Back:
            # Back through the questions from the last one, as if the review
            # hadn't been reached yet.
            asked = [i for i in sorted(really_asked) if steps[i].applies(a)]
            ask_from(asked[-1], history=asked[:-1])
            continue
        if not line:
            node = _build(nodes, a)
            try:
                check_node(a["node_id"], node)
            except NodeConfigError as e:
                w.say(f"    {e}")
                continue
            return a["node_id"], node
        if line.isdigit() and 1 <= int(line) <= len(shown):
            ask_from(steps.index(shown[int(line) - 1]), only_one=True)
        else:
            w.say(f"    Type a number from 1 to {len(shown)}, or press Enter to save.")


def add_node(path, wizard, boards=None, db_file=None):
    '''Run the questions and save the new node to path. Returns its ID, or
    None if cancelled.'''
    data = read_node_file(path)
    nodes = {int(k): v for k, v in data["nodes"].items()}
    taken = ids_with_data(db_file) if db_file else set()
    try:
        node_id, node = new_node(nodes, wizard, boards, taken)
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
    except (Cancelled, Back, EOFError, KeyboardInterrupt):
        return False
    del data["nodes"][str(node_id)]
    save_node_file(data, path)
    wizard.say(f"[SETUP] Removed node {node_id}.")
    return True
