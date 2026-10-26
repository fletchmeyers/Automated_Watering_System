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

from nodes import (NodeConfigError, BOARDS, LINKS, REQUIRED_PINS, WIFI_COST, check_node, links_of,
                   wifi_able, next_free_id, read_node_file, save_node_file, storage_of)

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
        "feather_m0_wifi":  {"radio_cs": 9, "radio_irq": 6, "radio_rst": 11, "sd_cs": 10},
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
    "feather_m0": "Feather M0", "feather_m0_wifi": "Feather M0 WiFi",
}


def wifi_worth_it(wizard, board):
    '''On a board where Wi-Fi has a cost (WIFI_COST), say what it is and ask.'''
    if board not in WIFI_COST:
        return True
    wizard.say(f"  Wi-Fi on a {BOARD_NAMES.get(board, board)}: {WIFI_COST[board]}.")
    return wizard.yes("Put Wi-Fi on it?")
FRAMEWORK_NAMES = {"circuitpython": "CircuitPython", "arduino": "Arduino"}
# The link question's answers: one link, or both in order of preference
# (nodes.json "link": ["wifi", "radio"]).
LINK_NAMES = {"wifi": "Wi-Fi", "radio": "RFM69 radio",
              "wifi,radio": "Wi-Fi, with the radio as backup",
              "radio,wifi": "RFM69 radio, with Wi-Fi as backup"}


def _has(a, link):
    return link in a["link"].split(",")
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

    def link(a):
        if not wifi_able(a["board"], a["framework"]):
            return w.choose("How it reaches the Pi", [("radio", LINK_NAMES["radio"])], "radio")
        while True:
            answer = w.choose("How it reaches the Pi",
                              [("wifi", "Wi-Fi (no radio needed; the Pi gives it the Wi-Fi settings)"),
                               ("radio", LINK_NAMES["radio"]),
                               ("wifi,radio", "Both: Wi-Fi first, the radio if Wi-Fi is down"),
                               ("radio,wifi", "Both: the radio first, Wi-Fi if the radio is down")],
                              a.get("link", "radio" if a["board"] in WIFI_COST else "wifi"))
            if answer == "radio" or wifi_worth_it(w, a["board"]):
                return answer

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
        usual = "sd" if _has(a, "radio") and "sd_cs" in defaults(a) else "none"
        return w.choose("Where it logs readings", options, a.get("storage", usual))

    def wanted_pins(a):
        wanted = list(REQUIRED_PINS[a["framework"]]) if _has(a, "radio") else []
        if a["storage"] == "sd":
            sd = [p for p in SD_PINS if p in defaults(a)] or ["sd_cs"]
            if a["framework"] == "circuitpython" and "sd_sck" not in sd:
                # Without its own bus the card is on the radio's SPI pins.
                sd += [p for p in ("spi_sck", "spi_mosi", "spi_miso") if p not in wanted]
            wanted += sd
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

    def saver(a):
        return w.yes("Battery saver: keep only the link in use powered (Wi-Fi stays off while "
                     "the radio works)?", default=a.get("saver", False))

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
        Step("saver", "Battery saver", saver, show=lambda v: "on" if v else "off",
             applies=lambda a: "," in a["link"]),
        Step("sleep", "Sleep window", sleep, show=lambda v: "-".join(v) if v else "none"),
    ]


def _build(nodes, a):
    node = {"name": a["name"], "short": a["short"],
            "color": next((c for c in COLORS if c not in {n.get("color") for n in nodes.values()}),
                          COLORS[a["node_id"] % len(COLORS)]),
            "framework": a["framework"], "board": a["board"]}
    if a["link"] != "radio":
        links = a["link"].split(",")
        node["link"] = links if len(links) > 1 else links[0]
    node.update(sense_interval_s=a["sense"],
                log_interval_s=a["log"] if a["storage"] != "none" else UNLOGGED_INTERVAL_S,
                storage=a["storage"])
    if a.get("pins"):
        node["pins"] = a["pins"]
    if a["rtc"]:
        node["rtc"] = "pcf8523"
    if a.get("saver") and "," in a["link"]:
        node["battery_saver"] = True
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


def set_links(path, node_id, links, wizard):
    '''Change which links a node has ("wifi", "radio" or both, in order of
    preference), asking for the radio pins if it's gaining the radio. The
    board then needs re-flashing. Returns the node's new settings.'''
    data = read_node_file(path)
    node = data["nodes"].get(str(node_id))
    if node is None:
        raise NodeConfigError(f"Node {node_id} isn't in {path}.")
    links = list(links)
    if not links or len(set(links)) != len(links) or any(l not in LINKS for l in links):
        raise NodeConfigError(f"Say its links in order of preference: {' and/or '.join(LINKS)}.")
    changed = dict(node)
    if "wifi" in links and "wifi" not in links_of(node):
        # A Feather M0 WiFi looks like a plain M0 over USB, so it may be
        # listed as one.
        if node["board"] == "feather_m0" and wizard.yes(
                f"Is node {node_id} a Feather M0 WiFi (Adafruit 3044, with the ATWINC1500 module)?",
                default=False):
            changed["board"] = "feather_m0_wifi"
        if not wifi_able(changed["board"], node["framework"]):
            check_node(node_id, {**changed, "link": links})   # says why not
        if not wifi_worth_it(wizard, changed["board"]):
            raise Cancelled()
    if len(links) < 2:
        # Battery saver chooses between two links; with one it has nothing to do.
        changed.pop("battery_saver", None)
        changed.pop("wifi_check_hours", None)
    # Where it logs stays as it was: a node with the radio would otherwise
    # default to an SD card it may not have.
    changed["storage"] = storage_of(node)
    changed["link"] = links[0] if len(links) == 1 else links
    if "radio" in links:
        framework, board = node["framework"], node["board"]
        pins = dict(node.get("pins") or {})
        wanted = [p for p in REQUIRED_PINS[framework] if p not in pins]
        if wanted:
            usual = DEFAULT_PINS.get(framework, {}).get(board, {})
            offer = {p: usual[p] for p in wanted if p in usual}
            if len(offer) == len(wanted):
                wizard.say("  Radio pins: " + ", ".join(f"{p}={v}" for p, v in offer.items()))
            if len(offer) == len(wanted) and wizard.yes("Use these pins?"):
                pins.update(offer)
            else:
                pins.update({p: wizard.pin(p, framework, offer.get(p)) for p in wanted})
        changed["pins"] = pins
    check_node(node_id, changed)
    data["nodes"][str(node_id)] = changed
    save_node_file(data, path)
    return changed


def set_saver(path, node_id, on, wifi_check_hours=None):
    '''Turn battery saver on or off for a node with both links (the board
    then needs re-flashing). wifi_check_hours: with the radio first, how
    often to turn Wi-Fi on to check it (None: never). Returns the node.'''
    data = read_node_file(path)
    node = data["nodes"].get(str(node_id))
    if node is None:
        raise NodeConfigError(f"Node {node_id} isn't in {path}.")
    changed = {k: v for k, v in node.items() if k not in ("battery_saver", "wifi_check_hours")}
    if on:
        changed["battery_saver"] = True
        if wifi_check_hours:
            changed["wifi_check_hours"] = wifi_check_hours
    elif wifi_check_hours:
        raise NodeConfigError("--wifi-check-hours goes with battery saver on.")
    check_node(node_id, changed)
    data["nodes"][str(node_id)] = changed
    save_node_file(data, path)
    return changed


def set_pins(path, node_id, assignments):
    '''Change some of a node's pins: assignments is {"radio_cs": "6", ...}
    as typed; numbers for an Arduino node, board pin names ("GP22", "D10")
    for a CircuitPython one. Checked like the rest of nodes.json before it's
    saved; the board then needs re-flashing. Returns the node.'''
    data = read_node_file(path)
    node = data["nodes"].get(str(node_id))
    if node is None:
        raise NodeConfigError(f"Node {node_id} isn't in {path}.")
    pins = dict(node.get("pins", {}))
    for name, value in assignments.items():
        if node["framework"] == "arduino":
            try:
                pins[name] = int(value)
            except ValueError:
                raise NodeConfigError(f'Arduino pin "{name}" must be a number, not {value!r}')
        else:
            pins[name] = value
    changed = dict(node, pins=pins)
    check_node(node_id, changed)
    data["nodes"][str(node_id)] = changed
    save_node_file(data, path)
    return changed


def set_radio_format(path, node_id, fmt):
    '''How a node sends over the radio: "json" or "msgpack". The Pi tells it
    with its next poll, so no re-flash. Returns the node.'''
    data = read_node_file(path)
    node = data["nodes"].get(str(node_id))
    if node is None:
        raise NodeConfigError(f"Node {node_id} isn't in {path}.")
    changed = {k: v for k, v in node.items() if k != "radio_format"}
    if fmt != "json":
        changed["radio_format"] = fmt
    check_node(node_id, changed)
    data["nodes"][str(node_id)] = changed
    save_node_file(data, path)
    return changed


def prefer_link(path, node_id, link):
    '''Make link the first of a node's links (it must have both); returns
    the new order. The Pi switches within a minute, with no restart.'''
    data = read_node_file(path)
    node = data["nodes"].get(str(node_id))
    if node is None:
        raise NodeConfigError(f"Node {node_id} isn't in {path}.")
    links = links_of(node)
    if link not in links:
        # Adding a link means new firmware: say exactly what to run.
        steps = [f"python3 node_setup.py link {node_id} {' '.join([link] + links)}",
                 "python3 node_setup.py"]
        if link == "radio":
            steps.append("sudo systemctl restart garden-radio")
        raise NodeConfigError(
            f"Node {node_id} has no {LINK_NAMES.get(link, link)} link (only "
            f"{', '.join(LINK_NAMES[l] for l in links)}). To add it, with the board plugged into "
            "the Pi, run:\n    " + "\n    ".join(steps))
    if len(links) == 1:
        raise NodeConfigError(f"Node {node_id} only has {LINK_NAMES[link]}, so there's nothing to switch.")
    node["link"] = [link] + [l for l in links if l != link]
    save_node_file(data, path)
    return node["link"]


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
