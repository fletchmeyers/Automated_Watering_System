'''
Python 3 running on Raspberry Pi 3B

Set up or update a node plugged into the Pi by USB, with the code in this
repo and its settings from the Pi's node list (raspberrypi/nodes.json):

    python3 node_setup.py              — update whichever node is plugged in; a
                                         board that isn't a node yet is offered
                                         the questions to add it
    python3 node_setup.py add          — add a node to the list (questions with
                                         defaults), then set up the board
    python3 node_setup.py remove 3     — take node 3 off the list
    python3 node_setup.py list         — show the nodes in the list
    python3 node_setup.py --dry-run    — show what would change, change nothing
    python3 node_setup.py --node 3     — set the board up as node 3 (one already
                                         in the list), e.g. to change its ID
    python3 node_setup.py --install    — reinstall CircuitPython itself too
    python3 node_setup.py --wifi-only  — just re-send a Wi-Fi node its Wi-Fi settings

nodes.json says what the node should be; this works out what the board is
now (a bootloader drive, CircuitPython, or Arduino on a serial port) and
gets it there, switching frameworks if needed:
  - CircuitPython: copies over whichever files differ — the code in
    circuitpython/, the libraries it uses, and a node_config.py written from
    nodes.json — and restarts the board if boot.py changed. A board without
    CircuitPython gets it first (the UF2 for its board from circuitpython.org).
  - Arduino on an RP2040/RP2350 board: builds a UF2 with this node's settings
    and copies it to the board's bootloader drive.
  - Arduino on the Feather M0 or an ESP32: builds and uploads over its serial
    port. A Wi-Fi node ("link": "wifi") is then sent the Pi's Wi-Fi network,
    password and address over USB, and checked for connecting to garden-wifi.
To reach the bootloader, a CircuitPython board is restarted from its console
and an Arduino one with a 1200-baud touch; a blank Pico is already there.
Arduino builds use PlatformIO (see README for the one-time install).

Written by Fletcher Meyers
October 2026
'''

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
try:
    import readline   # arrow keys and backspace work when typing answers to node_wizard's questions
except ImportError:   # not on Windows
    pass
from contextlib import contextmanager
from pathlib import Path

import node_wizard
from db import DB_FILE
from nodes import (
    NodeConfigError, NODES_FILE, load_nodes, storage_of, link_of, circuitpython_config, circuitpython_uf2_url,
    arduino_env, arduino_build_flags, BOARDS, BOOTLOADER_DRIVES, CIRCUITPYTHON_VERSION, RP2_CHIPS,
)

REPO             = Path(__file__).resolve().parent.parent
CIRCUITPY_SRC    = REPO / "circuitpython"
ARDUINO_DIR      = REPO / "arduino"
CIRCUITPY_LABEL  = "CIRCUITPY"
MOUNT_POINT      = Path("/mnt/circuitpy")
BOOT_MOUNT_POINT = Path("/mnt/uf2boot")
UF2_CACHE        = Path.home() / ".cache" / "garden-nodes"

# The node's own code, copied in this order: code.py last, so the board's
# auto-reload doesn't start the new code before the rest is in place.
CIRCUITPY_FILES = ("hardware_setup_garden.py", "communication_garden.py",
                   "sync_garden.py", "boot.py", "code.py")

_NODE_ID_LINE = re.compile(rb"^NODE_ID\s*=\s*(\d+)", re.MULTILINE)


class SetupError(Exception):
    pass


def device(label):
    return f"/dev/disk/by-label/{label}"


def wait_until(check, timeout, interval=0.5):
    '''Poll check() until it returns something truthy; return that, or None on timeout.'''
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(interval)
    return None


# ── Mounting drives ──────────────────────────────────────────────────────────

def mounted_at(dev):
    '''Where dev is already mounted, or None.'''
    real = os.path.realpath(dev)
    try:
        for line in Path("/proc/mounts").read_text().splitlines():
            source, where = line.split()[:2]
            if os.path.realpath(source) == real:
                return Path(where.replace("\\040", " "))
    except OSError:
        pass
    return None


@contextmanager
def mounted(dev, where, read_only=False):
    '''Mount dev at where for the duration (or use it where it's already mounted).'''
    existing = mounted_at(dev)
    if existing:
        yield existing
        return
    options = f"uid={os.getuid()},gid={os.getgid()}" + (",ro" if read_only else "")
    subprocess.run(["sudo", "mkdir", "-p", str(where)], check=True)
    subprocess.run(["sudo", "mount", "-o", options, dev, str(where)], check=True)
    try:
        yield where
    finally:
        # A board that just took a UF2 has already gone, so this may fail.
        subprocess.run(["sudo", "umount", str(where)], check=False, stderr=subprocess.DEVNULL)


def sync_disks():
    if hasattr(os, "sync"):   # flush to the drive before it can be unplugged
        os.sync()


# ── Updating a CircuitPython drive ───────────────────────────────────────────

def node_id_on_drive(drive):
    '''The NODE_ID set on a CIRCUITPY drive, or None for a board that isn't a node yet.'''
    for name in ("node_config.py", "hardware_setup_garden.py"):
        try:
            match = _NODE_ID_LINE.search((Path(drive) / name).read_bytes())
        except OSError:
            continue
        if match:
            return int(match.group(1))
    return None


def _lib_files(entry):
    '''Every file under one lib/ entry (a library folder or a single .mpy).'''
    src = CIRCUITPY_SRC / "lib" / entry
    if src.is_dir():
        return [p for p in sorted(src.rglob("*")) if p.is_file() and "__pycache__" not in p.parts]
    return [src] if src.is_file() else []


def required_libs():
    '''
    The lib/ entries the node code needs: the libraries its files mention,
    then the ones those mention, and so on. A .mpy keeps the names of the
    modules it imports as plain text, so a name turning up in a file's bytes
    is how a dependency shows (e.g. adafruit_ina23x needs adafruit_ina228).
    A false match only copies a spare library.
    '''
    entries = sorted(p.name for p in (CIRCUITPY_SRC / "lib").iterdir())
    modules = {name[:-4] if name.endswith(".mpy") else name: name for name in entries}
    needed = set()
    to_scan = [CIRCUITPY_SRC / name for name in CIRCUITPY_FILES]
    while to_scan:
        data = to_scan.pop().read_bytes()
        for module, entry in modules.items():
            if entry not in needed and module.encode() in data:
                needed.add(entry)
                to_scan.extend(_lib_files(entry))
    return needed


def plan_circuitpython(drive, node_id, node):
    '''The files to write to the drive, as [(path on drive, contents)], only where they differ.
    Libraries the node code needs are added; others already on the drive are kept up to date.'''
    drive = Path(drive)
    wanted = []

    on_drive = {p.name for p in (drive / "lib").iterdir()} if (drive / "lib").is_dir() else set()
    in_repo = {p.name for p in (CIRCUITPY_SRC / "lib").iterdir()}
    for entry in sorted(required_libs() | (on_drive & in_repo)):
        for src in _lib_files(entry):
            wanted.append((Path("lib") / src.relative_to(CIRCUITPY_SRC / "lib"), src.read_bytes()))

    wanted.append((Path("node_config.py"), circuitpython_config(node_id, node).encode()))
    for name in CIRCUITPY_FILES:
        wanted.append((Path(name), (CIRCUITPY_SRC / name).read_bytes()))

    plan = []
    for rel, data in wanted:
        try:
            if (drive / rel).read_bytes() == data:
                continue
        except OSError:
            pass
        plan.append((rel, data))
    return plan


def apply_plan(drive, plan):
    for rel, data in plan:
        target = Path(drive) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    sync_disks()


def update_circuitpython(node_id, node, dry_run):
    '''Copy what differs onto CIRCUITPY. Returns True if the node should now be
    checked (always, unless it's a dry run).'''
    with mounted(device(CIRCUITPY_LABEL), MOUNT_POINT, read_only=dry_run) as drive:
        plan = plan_circuitpython(drive, node_id, node)
        if not plan:
            print(f"[SETUP] Node {node_id} already matches the repo and nodes.json — nothing to copy.")
            return not dry_run
        print(f"[SETUP] {'Would copy' if dry_run else 'Copying'} {len(plan)} file(s) to {drive}:")
        for rel, data in plan:
            print(f"          {rel}  ({len(data):,} bytes)")
        if dry_run:
            return False
        apply_plan(drive, plan)
    if any(rel.name == "boot.py" for rel, _ in plan):
        # boot.py only runs at a hard reset; do one from the console.
        print("[SETUP] boot.py changed — restarting the board so it takes effect.")
        if run_on_console(["import microcontroller", "microcontroller.reset()"]) is None:
            print("[SETUP] Couldn't reach its console: press the board's reset button instead.")
    else:
        print("[SETUP] Done. CircuitPython restarts the code by itself.")
    return True


# ── The CircuitPython console (REPL) ─────────────────────────────────────────

_ANSI = re.compile(r"\x1b\][^\x1b\x07]*(\x1b\\|\x07)|\x1b\[[0-9;]*[A-Za-z]")


def console_ports():
    '''Serial ports that may be a CircuitPython console, likeliest first: ports
    named as the console, then unnamed ones, then the data port (boot.py's).'''
    from serial.tools import list_ports
    ports = [p for p in list_ports.comports()
             if p.vid in (0x239A, 0x2E8A, 0x303A)]   # Adafruit, Raspberry Pi, Espressif
    def rank(p):
        name = (p.interface or "").lower()
        return (2 if "data" in name else 0 if "circuitpython" in name else 1, p.device)
    return [p.device for p in sorted(ports, key=rank)]


def _read_for(port, seconds):
    out = b""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        out += port.read(256)
    return out


def _clean(raw):
    '''Console bytes as readable text: no terminal title or colour codes.'''
    return _ANSI.sub("", raw.decode(errors="replace")).replace("\r", "")


def _to_prompt(s):
    '''Stop code.py and get to the REPL. Returns (reached ">>>"?, what was printed).'''
    s.reset_input_buffer()
    s.write(b"\x03\x03")     # Ctrl-C twice: stop code.py
    out = _read_for(s, 1)
    s.write(b"\r")           # "Press any key to enter the REPL"
    out += _read_for(s, 1.5)
    return b">>>" in out, out


def _with_console(action, timeout=15):
    '''
    Find the port that reaches the REPL prompt and return action(port, text so
    far). Keeps trying for timeout seconds: just after files are written the
    board is busy reloading and doesn't answer Ctrl-C straight away. Returns
    None if no port ever gave a prompt, or "" if the action reset the board
    (which takes the port away mid-command).
    '''
    import serial
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for path in console_ports():
            try:
                s = serial.Serial(path, 115200, timeout=0.2)
            except (OSError, serial.SerialException):
                continue
            at_prompt = False
            try:
                at_prompt, out = _to_prompt(s)
                if at_prompt:
                    return action(s, out)
            except (OSError, serial.SerialException):
                if at_prompt:
                    return ""
            finally:
                try:
                    s.close()
                except (OSError, serial.SerialException):
                    pass
        time.sleep(1)
    return None


def run_on_console(lines):
    '''Type lines into the board's REPL. Returns what it printed, or None if
    there was no console to type into.'''
    def type_lines(s, out):
        for line in lines:
            s.write(line.encode() + b"\r")
            out += _read_for(s, 0.5)
        return _clean(out)
    return _with_console(type_lines)


def console_output(seconds=10):
    '''Restart code.py from the console (Ctrl-D) and return what it prints, e.g. a traceback.'''
    def soft_reboot(s, out):
        s.write(b"\x04")
        return _clean(_read_for(s, seconds))
    return _with_console(soft_reboot)



# ── Getting a board into its UF2 bootloader ──────────────────────────────────

def bootloader_drive():
    '''The label of a UF2 bootloader drive that's plugged in, or None.'''
    for label in BOOTLOADER_DRIVES:
        if Path(device(label)).exists():
            return label
    return None


def touch_1200(port):
    '''Open the port at 1200 baud and close it: an Arduino-Pico board takes
    that as "restart into the bootloader" (it's how its uploader does it).'''
    import serial
    try:
        s = serial.Serial(port, 1200)
        s.dtr = False
        time.sleep(0.1)
        s.close()
    except (OSError, serial.SerialException):
        pass   # the board resetting takes the port away


def enter_bootloader(board):
    '''Restart the plugged-in board into its UF2 bootloader and return the
    drive's label: from its console if it runs CircuitPython, with a
    1200-baud touch if it runs Arduino.'''
    label = bootloader_drive()
    if label:
        return label
    said = ""
    if board.framework == "circuitpython":
        print("[SETUP] Restarting it into its bootloader (from its console)...")
        said = run_on_console(["import microcontroller",
                               "microcontroller.on_next_reset(microcontroller.RunMode.UF2)",
                               "microcontroller.reset()"])
        if said is None:
            raise SetupError("Couldn't reach the board's console to restart it into its bootloader.")
    elif board.port:
        print("[SETUP] Restarting it into its bootloader (1200-baud touch)...")
        touch_1200(board.port)
    label = wait_until(bootloader_drive, timeout=30)
    if label is None:
        if said.strip():
            print("[SETUP] The board's console said:\n" + said.strip())
        raise SetupError("The bootloader drive didn't appear. Hold the board's BOOTSEL/BOOT "
                         "button while you plug it in, then run this again.")
    return label


def copy_uf2(label, uf2, node_id, node):
    '''Copy a UF2 onto the bootloader drive (after checking it's the right
    kind of chip); the board installs it and restarts by itself.'''
    chip = BOARDS[node["board"]]["chip"]
    if BOOTLOADER_DRIVES[label] != chip:
        raise SetupError(f"nodes.json says node {node_id} is a {node['board']} ({chip}), but the "
                         f"board plugged in is a {BOOTLOADER_DRIVES[label]} ({label} drive).")
    print(f"[SETUP] Copying {uf2.name} to the {label} drive (the board restarts by itself)...")
    with mounted(device(label), BOOT_MOUNT_POINT) as drive:
        shutil.copyfile(uf2, Path(drive) / uf2.name)
        sync_disks()


# ── CircuitPython ────────────────────────────────────────────────────────────

def download_uf2(node):
    '''The CircuitPython UF2 for this node's board, downloaded once and kept in UF2_CACHE.'''
    url = circuitpython_uf2_url(node)
    path = UF2_CACHE / url.rsplit("/", 1)[1]
    if path.exists():
        return path
    UF2_CACHE.mkdir(parents=True, exist_ok=True)
    print(f"[SETUP] Downloading {url}")
    part = path.with_suffix(".part")
    request = urllib.request.Request(url, headers={"User-Agent": "garden node_setup.py"})
    try:
        with urllib.request.urlopen(request, timeout=60) as r, open(part, "wb") as f:
            shutil.copyfileobj(r, f)
    except OSError as e:
        part.unlink(missing_ok=True)
        raise SetupError(f"Could not download CircuitPython for {node['board']}: {e}")
    part.rename(path)
    return path


def install_circuitpython(node_id, node, board, dry_run):
    '''Put CircuitPython on the board (whatever it runs now), then the node code.
    Returns True if the node should now be checked.'''
    if dry_run:
        print(f"[SETUP] Would install CircuitPython {CIRCUITPYTHON_VERSION} "
              f"({circuitpython_uf2_url(node)}), then copy the node code.")
        return False
    label = enter_bootloader(board)
    copy_uf2(label, download_uf2(node), node_id, node)
    if not wait_until(lambda: Path(device(CIRCUITPY_LABEL)).exists(), timeout=90):
        raise SetupError("CircuitPython didn't come up (no CIRCUITPY drive after 90s).")
    time.sleep(3)   # let it finish starting before its drive is written to
    print("[SETUP] CircuitPython is installed. Copying the node code...")
    return update_circuitpython(node_id, node, dry_run=False)


# ── Arduino ──────────────────────────────────────────────────────────────────

def find_pio():
    for candidate in (shutil.which("pio"), Path.home() / ".platformio/penv/bin/pio"):
        if candidate and Path(candidate).exists():
            return str(candidate)
    raise SetupError("PlatformIO isn't installed on the Pi yet — see README "
                     "'Adding or updating a node' for the one-time install.")


def run_pio(env_name, flags, extra=()):
    command = [find_pio(), "run", "-d", str(ARDUINO_DIR), "-e", env_name, *extra]
    print("[SETUP] Building (the first build for a board downloads its compiler — several minutes)...")
    result = subprocess.run(command, env={**os.environ, "PLATFORMIO_BUILD_FLAGS": " ".join(flags)})
    if result.returncode != 0:
        raise SetupError("PlatformIO build/upload failed — see its output above.")


def update_arduino(node_id, node, port, dry_run):
    '''A SAMD board (the M0): build and upload over its serial port.'''
    flags = arduino_build_flags(node_id, node)
    env_name = arduino_env(node)
    print(f"[SETUP] Build {env_name} for node {node_id} with: {' '.join(flags)}")
    if dry_run:
        print(f"[SETUP] Would upload to {port}.")
        return False
    run_pio(env_name, flags, ["-t", "upload", "--upload-port", port])
    return True


def flash_arduino_uf2(node_id, node, board, dry_run):
    '''An RP2040/RP2350 board, whatever it runs now: build a UF2, restart the
    board into its bootloader and copy the UF2 over.'''
    flags = arduino_build_flags(node_id, node)
    env_name = arduino_env(node)
    print(f"[SETUP] Build {env_name} for node {node_id} with: {' '.join(flags)}")
    if dry_run:
        print("[SETUP] Would restart it into its bootloader and copy the firmware over.")
        return False
    run_pio(env_name, flags)
    build_dir = Path(os.environ.get("PLATFORMIO_BUILD_DIR", ARDUINO_DIR / ".pio" / "build"))
    uf2 = build_dir / env_name / "firmware.uf2"
    if not uf2.exists():
        raise SetupError(f"The build finished but {uf2} isn't there.")
    copy_uf2(enter_bootloader(board), uf2, node_id, node)
    return True


# ── Finding what's plugged in ────────────────────────────────────────────────

class Board:
    '''What's plugged in: framework is "circuitpython", "arduino" (anything on
    a serial port) or "bootloader"; node_id is the node it says it is;
    boards, which BOARDS it could be (when that can be told).'''
    def __init__(self, framework, node_id=None, port=None, label=None, boards=None):
        self.framework, self.node_id, self.port, self.label = framework, node_id, port, label
        self.boards = boards

    def __str__(self):
        if self.framework == "bootloader":
            return f"a board waiting in its bootloader ({self.label} drive)"
        what = ("a CircuitPython board" if self.framework == "circuitpython"
                else f"an Arduino board on {self.port}")
        return what + (f", node {self.node_id}" if self.node_id else ", not set up as a node yet")


def serial_ports():
    return sorted(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*"))


def ask_node(paths, quiet=False):
    '''(node ID, port) of a running node on one of paths, or (None, None).'''
    from usb_sync import find_node   # needs pyserial
    node, info = find_node(paths, quiet=quiet)
    if node is None:
        return None, None
    node.port.close()
    return info["n"], node.port.port


def boards_with(chip=None, circuitpython=None):
    return [name for name, b in BOARDS.items()
            if (chip is None or b["chip"] == chip)
            and (circuitpython is None or b["circuitpython"] == circuitpython)] or None


def circuitpython_board(drive):
    '''The board ID CircuitPython reports in boot_out.txt, e.g. "raspberry_pi_pico2".'''
    try:
        match = re.search(r"Board ID:\s*(\S+)", (Path(drive) / "boot_out.txt").read_text(errors="replace"))
    except OSError:
        return None
    return match.group(1) if match else None


# USB vendor IDs that give away an Arduino board: the ESP32-S2's own USB, and
# the ESP32 V2's CH9102 USB-serial chip.
_BOARDS_BY_VID = {0x303A: ["feather_esp32s2"], 0x1A86: ["feather_esp32_v2"]}


def boards_on_port(path):
    try:
        from serial.tools import list_ports
    except ImportError:
        return None
    vid = next((p.vid for p in list_ports.comports() if p.device == path), None)
    return _BOARDS_BY_VID.get(vid)


def detect(args):
    '''Work out what's plugged in.'''
    label = bootloader_drive()
    if label:
        return Board("bootloader", label=label, boards=boards_with(chip=BOOTLOADER_DRIVES[label]))
    if Path(device(CIRCUITPY_LABEL)).exists():
        with mounted(device(CIRCUITPY_LABEL), MOUNT_POINT, read_only=True) as drive:
            cp_board = circuitpython_board(drive)
            return Board("circuitpython", node_id_on_drive(drive),
                         boards=boards_with(circuitpython=cp_board) if cp_board else None)
    ports = [args.port] if args.port else serial_ports()
    if not ports:
        raise SetupError("Nothing plugged in: no bootloader drive, no CIRCUITPY drive and "
                         "no USB serial port.")
    detected, port = ask_node(ports)
    if port is None:
        if not args.port and len(ports) > 1:
            raise SetupError(f"No node answered on {', '.join(ports)}; name the board's port with --port.")
        port = ports[0]
    return Board("arduino", detected, port, boards=boards_on_port(port))


def confirm_running(node_id, framework="circuitpython", wait=8, timeout=40):
    '''After an update, check the node answers over USB with the right ID, and if
    it doesn't, show what a CircuitPython board prints as its code starts.'''
    print(f"[SETUP] Checking the node answers (up to {timeout}s while it restarts)...")
    time.sleep(wait)
    deadline = time.monotonic() + timeout - wait
    answered = None
    while answered != node_id and time.monotonic() < deadline:
        answered, _ = ask_node(serial_ports(), quiet=True)
        if answered != node_id:
            time.sleep(2)
    if answered == node_id:
        print(f"[SETUP] Node {node_id} is running the new code.")
        return True
    print(f"[SETUP] It didn't answer as node {node_id}"
          + (f" (it says it's node {answered})" if answered else "") + ".")
    if framework == "circuitpython":
        output = console_output()
        if output:
            lines = [l for l in output.strip().splitlines() if l.strip()]
            print("[SETUP] Its console, restarting code.py:\n          " + "\n          ".join(lines[-20:]))
            return False
    print("[SETUP] Look at its serial output with:\n"
          "          python3 -m serial.tools.miniterm /dev/ttyACM0 115200")
    return False


# ── Wi-Fi nodes ──────────────────────────────────────────────────────────────

def _nmcli(args, sudo=False):
    '''One value from nmcli -g, with its escaping (\\: and \\\\) undone.'''
    result = subprocess.run((["sudo"] if sudo else []) + ["nmcli"] + args,
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise SetupError(f"nmcli {' '.join(args)} failed: {result.stderr.strip()}")
    value = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    return value.replace("\\:", ":").replace("\\\\", "\\")


def pi_address():
    '''The Pi's own address on the local network (no packets are sent).'''
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("192.0.2.1", 9))   # a documentation-only address: picks the outgoing interface
        return s.getsockname()[0]


def wifi_settings():
    '''What a Wi-Fi node needs: the network the Pi itself is on (and its
    password, from NetworkManager — needs sudo), and where to find the Pi.'''
    import socket
    from wifi_nodes import WIFI_PORT
    connection = _nmcli(["-g", "GENERAL.CONNECTION", "device", "show", "wlan0"])
    if not connection:
        raise SetupError("The Pi isn't on Wi-Fi itself (wlan0 has no connection), "
                         "so there's no network to give the node.")
    return {
        "s": _nmcli(["-s", "-g", "802-11-wireless.ssid", "connection", "show", "id", connection]),
        "p": _nmcli(["-s", "-g", "802-11-wireless-security.psk", "connection", "show", "id", connection],
                    sudo=True),
        "h": pi_address(),
        "hn": socket.gethostname(),
        "port": WIFI_PORT,
    }


def send_wifi_config(node_id, settings, answer_within=15):
    '''
    Find the node on a USB port and send it the Wi-Fi settings, all in one
    port session. On a board with a USB-serial chip (the ESP32 V2), opening
    or closing the port can reset it, so it may spend a few seconds booting
    before it answers: keep asking on the same open port rather than closing
    and reopening (which could reset it again). True once it confirms;
    otherwise says which step failed and what the board printed meanwhile.
    '''
    from usb_sync import UsbNode, open_port
    for path in serial_ports():
        try:
            node = UsbNode(open_port(path))
        except Exception:
            continue
        try:
            deadline = time.monotonic() + answer_within
            info = None
            while time.monotonic() < deadline:
                info, _ = node.ask({"t": "info"}, "info", timeout=3)
                if info is not None:
                    break
            if info is None or info.get("n") != node_id:
                continue
            ack, _ = node.ask({"t": "wifi_config", **settings}, "wifi_config_ack", timeout=10)
            if ack and ack.get("ok"):
                return True
            print(f"[SETUP] Node {node_id} answered on {path} but didn't confirm the Wi-Fi settings.")
            if node.chatter:
                print("[SETUP] What it printed:\n          " + "\n          ".join(node.chatter))
            return False
        finally:
            node.port.close()
    print(f"[SETUP] Node {node_id} didn't answer on {', '.join(serial_ports()) or 'any USB port'}.")
    return False


def setup_wifi(node_id, timeout=60):
    '''Send a freshly flashed Wi-Fi node the network settings over USB (they
    never touch git or the terminal), then wait for it to reach garden-wifi.'''
    from wifi_nodes import STATUS_FILE
    settings = wifi_settings()
    print(f"[SETUP] Sending node {node_id} the Wi-Fi settings for \"{settings['s']}\" "
          f"(Pi at {settings['h']}:{settings['port']})...")
    if not send_wifi_config(node_id, settings):
        raise SetupError("Couldn't give the node its Wi-Fi settings over USB: it didn't answer, "
                         "or didn't confirm them. Run node_setup.py again to retry.")

    print(f"[SETUP] Waiting for node {node_id} to connect over Wi-Fi (up to {timeout}s)...")
    def connected():
        try:
            return json.loads(Path(STATUS_FILE).read_text()).get(str(node_id))
        except (OSError, ValueError):
            return None
    link = wait_until(connected, timeout=timeout, interval=2)
    if link:
        print(f"[SETUP] Node {node_id} is connected over Wi-Fi from {link['ip']}; "
              "the Pi polls it every minute.")
    else:
        print(f"[SETUP] Node {node_id} hasn't connected yet. Check the Pi's side with\n"
              "          sudo journalctl -u garden-wifi -n 30\n"
              "        and the node's own output with\n"
              "          python3 -m serial.tools.miniterm /dev/ttyACM0 115200")


def interactive():
    return sys.stdin.isatty()


def run(args, board=None):
    # Another node's mistake shouldn't stop this one being set up: skip it.
    nodes = load_nodes(args.nodes, skip_invalid=True)
    board = board or detect(args)
    print(f"[SETUP] Found {board}.")

    node_id = args.node or board.node_id
    if node_id is None:
        if not interactive():
            raise SetupError("Which node should this board be? Say with --node N, or run "
                             "node_setup.py add to add a new node.")
        if not node_wizard.Wizard().yes("It isn't a node yet. Add it to the node list as a new node?"):
            raise SetupError("Nothing done. To set it up as a node already in the list: --node N")
        node_id = node_wizard.add_node(args.nodes, node_wizard.Wizard(), board.boards, DB_FILE)
        if node_id is None:
            return
        nodes = load_nodes(args.nodes, skip_invalid=True)
    if args.node and board.node_id and args.node != board.node_id:
        print(f"[SETUP] It's currently node {board.node_id}; setting it up as node {args.node} instead.")
    if node_id not in nodes:
        raise SetupError(f"Node {node_id} isn't in {args.nodes} (or was skipped above). "
                         "Add it with: python3 node_setup.py add")
    node = nodes[node_id]
    target = node["framework"]
    chip = BOARDS[node["board"]]["chip"]
    print(f"[SETUP] Node {node_id}: {node['name']} ({target} on {node['board']})")

    if args.wifi_only:
        # Already running its firmware: just (re)send the Wi-Fi settings, e.g.
        # after the Wi-Fi password or the Pi's address changed.
        if link_of(node) != "wifi":
            raise SetupError(f"Node {node_id} isn't a Wi-Fi node, so it has no Wi-Fi settings.")
        if not args.dry_run:
            setup_wifi(node_id)
        return

    if target == "circuitpython":
        if board.framework == "circuitpython" and not args.install:
            checked = update_circuitpython(node_id, node, args.dry_run)
        else:
            if board.framework == "arduino":
                print("[SETUP] Switching it from Arduino to CircuitPython.")
            checked = install_circuitpython(node_id, node, board, args.dry_run)
    elif chip in RP2_CHIPS:
        if board.framework == "circuitpython":
            print("[SETUP] Switching it from CircuitPython to Arduino.")
        checked = flash_arduino_uf2(node_id, node, board, args.dry_run)
    else:
        if board.framework != "arduino":
            raise SetupError(f"Node {node_id} is an Arduino {node['board']}, but what's plugged in is "
                             f"{board}. A {node['board']} is only set up from its serial port.")
        checked = update_arduino(node_id, node, board.port, args.dry_run)

    running = checked and confirm_running(node_id, target, wait=12 if board.framework != target else 8)
    if link_of(node) == "wifi":
        if args.dry_run:
            print("[SETUP] Would then send it the Pi's Wi-Fi network, password and address over USB.")
        elif running:
            setup_wifi(node_id)
    elif not args.dry_run and board.node_id != node_id:
        print(f"[SETUP] If node {node_id} is new to the network, restart the Pi's radio loop so it "
              "starts polling it: sudo systemctl restart garden-sensor")


def add(args):
    '''Add a node to the list; then, if a board is plugged in, set it up as it.'''
    try:
        board = detect(args)
    except SetupError:
        board = None
    if board is not None and board.node_id is not None:
        print(f"[SETUP] Plugged in: {board}. Adding a new node to the list anyway.")
    node_id = node_wizard.add_node(args.nodes, node_wizard.Wizard(),
                                   board.boards if board else None, DB_FILE)
    if node_id is None:
        return
    if board is None:
        print(f"[SETUP] Plug the board in and run: python3 node_setup.py --node {node_id}")
    elif node_wizard.Wizard().yes(f"Set up the plugged-in board as node {node_id} now?"):
        args.node = node_id
        run(args, board)
    else:
        print(f"[SETUP] Later, with the board plugged in: python3 node_setup.py --node {node_id}")


def list_nodes(path):
    print(f"Nodes in {path}:")
    for node_id, node in load_nodes(path, skip_invalid=True).items():
        sleep = node.get("sleep_window")
        print(f"  {node_id}: {node['name']:<24} {node['framework']:<13} {node['board']:<24} "
              f"log every {node['log_interval_s']}s to {storage_of(node)}, over {link_of(node)}"
              + (f", asleep {sleep[0]}-{sleep[1]}" if sleep else ""))


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", nargs="?", choices=["update", "add", "remove", "list"], default="update")
    parser.add_argument("node_id", nargs="?", type=int, help="for remove: the node to take off the list")
    parser.add_argument("--node", type=int, help="node ID to set the board up as")
    parser.add_argument("--install", action="store_true",
                        help=f"(re)install CircuitPython {CIRCUITPYTHON_VERSION} even if the board has it")
    parser.add_argument("--wifi-only", action="store_true",
                        help="don't flash anything: just (re)send a Wi-Fi node its Wi-Fi settings")
    parser.add_argument("--port", help="the board's serial port, if more than one is plugged in")
    parser.add_argument("--dry-run", action="store_true", help="show what would change, change nothing")
    parser.add_argument("--nodes", default=str(NODES_FILE), metavar="FILE",
                        help="a nodes file other than the Pi's own list, e.g. a bench test "
                             "file in tests/hardware_tests/")
    args = parser.parse_args()
    try:
        if args.action == "list":
            list_nodes(args.nodes)
        elif args.action == "add":
            add(args)
        elif args.action == "remove":
            if args.node_id is None:
                parser.error("say which node: node_setup.py remove N")
            if node_wizard.remove_node(args.nodes, args.node_id, node_wizard.Wizard()):
                print("[SETUP] Restart the Pi's radio loop so it stops polling it: "
                      "sudo systemctl restart garden-sensor")
        else:
            run(args)
    except (NodeConfigError, SetupError) as e:
        sys.exit(f"[SETUP] {e}")
    except subprocess.CalledProcessError as e:
        sys.exit(f"[SETUP] {' '.join(e.cmd)} failed (exit {e.returncode}).")


if __name__ == "__main__":
    main()
