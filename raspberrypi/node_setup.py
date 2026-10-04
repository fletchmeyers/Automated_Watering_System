'''
Python 3 running on Raspberry Pi 3B

Set up or update a node plugged into the Pi by USB, with the code in this
repo and its settings from nodes.json:

    python3 node_setup.py              — update whichever node is plugged in
    python3 node_setup.py --dry-run    — show what would change, change nothing
    python3 node_setup.py --install --node 3
                                       — install CircuitPython on a board and set it
                                         up as node 3 (add node 3 to nodes.json first)
    python3 node_setup.py --node 3     — update a board as node 3 (to change its ID)
    python3 node_setup.py list         — show the nodes in nodes.json

CircuitPython: mounts the CIRCUITPY drive and copies over whichever files
differ: the code in circuitpython/, the libraries it uses, and a
node_config.py written from nodes.json, then restarts the board if boot.py
changed. --install first puts CircuitPython itself on the board: a blank
board shows its bootloader drive by itself, one already running CircuitPython
is restarted into it, and the UF2 for its board is downloaded and copied over.
Arduino: builds with this node's settings and uploads, using PlatformIO
(see README for the one-time install).

Written by Fletcher Meyers
October 2026
'''

import argparse
import glob
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path

from nodes import (
    NodeConfigError, load_nodes, circuitpython_config, circuitpython_uf2_url,
    arduino_env, arduino_build_flags, BOARDS, BOOTLOADER_DRIVES, CIRCUITPYTHON_VERSION,
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

# CircuitPython 10 on a 4 MB ESP32-S2 needs at least this TinyUF2 bootloader.
MIN_TINYUF2 = (0, 33, 0)

_NODE_ID_LINE = re.compile(rb"^NODE_ID\s*=\s*(\d+)", re.MULTILINE)
_TINYUF2      = re.compile(r"TinyUF2 Bootloader\s+v?(\d+)\.(\d+)\.(\d+)")


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
    '''Copy what differs onto CIRCUITPY. Returns True if the node should now be checked.'''
    with mounted(device(CIRCUITPY_LABEL), MOUNT_POINT, read_only=dry_run) as drive:
        plan = plan_circuitpython(drive, node_id, node)
        if not plan:
            print(f"[SETUP] Node {node_id} already matches the repo and nodes.json — nothing to copy.")
            return False
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


def run_on_console(lines):
    '''
    Stop the running code, get to the REPL prompt (">>>") and type lines into
    it. Returns what the board printed, or None if no port gave a prompt.
    '''
    import serial
    for path in console_ports():
        try:
            with serial.Serial(path, 115200, timeout=0.2) as s:
                s.reset_input_buffer()
                s.write(b"\x03\x03")     # Ctrl-C twice: stop code.py
                out = _read_for(s, 1)
                s.write(b"\r")           # "Press any key to enter the REPL"
                out += _read_for(s, 1.5)
                if b">>>" not in out:
                    continue             # not the console (or not CircuitPython)
                for line in lines:
                    s.write(line.encode() + b"\r")
                    out += _read_for(s, 0.5)
                return _clean(out)
        except (OSError, serial.SerialException):
            return ""                    # a reset took the port away mid-command
    return None


def console_output(seconds=10):
    '''Restart code.py from the console (Ctrl-D) and return what it prints, e.g. a traceback.'''
    import serial
    for path in console_ports():
        try:
            with serial.Serial(path, 115200, timeout=0.2) as s:
                s.write(b"\x03\x03")     # stop code.py, if it's running
                out = _read_for(s, 1)
                s.write(b"\r")           # into the REPL
                out += _read_for(s, 1.5)
                if b">>>" not in out:
                    continue
                s.write(b"\x04")         # Ctrl-D: soft reboot, which runs code.py
                return _clean(_read_for(s, seconds))
        except (OSError, serial.SerialException):
            continue
    return None


# ── Installing CircuitPython ─────────────────────────────────────────────────

def bootloader_drive():
    '''The label of a UF2 bootloader drive that's plugged in, or None.'''
    for label in BOOTLOADER_DRIVES:
        if Path(device(label)).exists():
            return label
    return None


def check_tinyuf2(drive):
    '''Refuse an ESP32-S2 bootloader too old for CircuitPython 10.'''
    try:
        info = (Path(drive) / "INFO_UF2.TXT").read_text(errors="replace")
    except OSError:
        return
    match = _TINYUF2.search(info)
    if match and tuple(int(x) for x in match.groups()) < MIN_TINYUF2:
        have = ".".join(match.groups())
        need = ".".join(map(str, MIN_TINYUF2))
        raise SetupError(f"This ESP32-S2's TinyUF2 bootloader is {have}; CircuitPython 10 needs "
                         f"{need} or newer. Update it first (Adafruit's ESP32-S2 Feather guide, "
                         "'Install UF2 Bootloader'), then run this again.")


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


def install_circuitpython(args, nodes, label):
    '''Put CircuitPython on the plugged-in board, then set it up as its node.'''
    detected = None
    if label is None:
        if not Path(device(CIRCUITPY_LABEL)).exists():
            raise SetupError(
                "No board waiting to install. A blank Pico shows its bootloader drive by itself; "
                "otherwise hold BOOTSEL (Pico) or double-tap reset (ESP32-S2) as you plug it in. "
                "Switching an Arduino board to CircuitPython isn't automated yet.")
        with mounted(device(CIRCUITPY_LABEL), MOUNT_POINT, read_only=True) as drive:
            detected = node_id_on_drive(drive)

    node_id = args.node or detected
    if node_id is None:
        raise SetupError("Which node should this board be? Say with --node N "
                         "(add it to nodes.json first).")
    if node_id not in nodes:
        raise SetupError(f"Node {node_id} isn't in nodes.json yet — add it there first.")
    node = nodes[node_id]
    if node["framework"] != "circuitpython":
        raise SetupError(f"nodes.json says node {node_id} runs {node['framework']}; "
                         "--install only installs CircuitPython so far.")
    chip = BOARDS[node["board"]]["chip"]
    print(f"[SETUP] Node {node_id}: {node['name']} ({node['board']}, CircuitPython {CIRCUITPYTHON_VERSION})")

    if label is None:
        if args.dry_run:
            print("[SETUP] Would restart it into its bootloader, then install CircuitPython "
                  f"{CIRCUITPYTHON_VERSION} from {circuitpython_uf2_url(node)}")
            return
        print("[SETUP] Restarting it into its bootloader...")
        typed = run_on_console(["import microcontroller",
                                "microcontroller.on_next_reset(microcontroller.RunMode.UF2)",
                                "microcontroller.reset()"])
        if typed is None:
            raise SetupError("Couldn't reach the board's console to restart it into its bootloader.")
        label = wait_until(bootloader_drive, timeout=30)
        if label is None:
            if typed.strip():
                print("[SETUP] The board's console said:\n" + typed.strip())
            raise SetupError("The bootloader drive didn't appear. Hold BOOTSEL (Pico) or "
                             "double-tap reset (ESP32-S2) as you plug it in, then run this again.")

    if BOOTLOADER_DRIVES[label] != chip:
        raise SetupError(f"nodes.json says node {node_id} is a {node['board']} ({chip}), but the "
                         f"board plugged in is a {BOOTLOADER_DRIVES[label]} ({label} drive).")
    if args.dry_run:
        print(f"[SETUP] Would install CircuitPython {CIRCUITPYTHON_VERSION} from "
              f"{circuitpython_uf2_url(node)}, then copy the node code.")
        return

    uf2 = download_uf2(node)
    print(f"[SETUP] Installing {uf2.name} (the board restarts by itself)...")
    with mounted(device(label), BOOT_MOUNT_POINT) as drive:
        if chip == "esp32s2":
            check_tinyuf2(drive)
        shutil.copyfile(uf2, Path(drive) / uf2.name)
        sync_disks()
    if not wait_until(lambda: Path(device(CIRCUITPY_LABEL)).exists(), timeout=90):
        raise SetupError("CircuitPython didn't come up (no CIRCUITPY drive after 90s).")
    time.sleep(3)   # let it finish starting before its drive is written to
    print("[SETUP] CircuitPython is installed. Copying the node code...")
    if update_circuitpython(node_id, node, dry_run=False):
        confirm_running(node_id, wait=12)
    print("[SETUP] If this is a new node, restart the Pi's radio loop so it starts "
          "polling it: sudo systemctl restart garden-sensor")


# ── Arduino ──────────────────────────────────────────────────────────────────

def find_pio():
    for candidate in (shutil.which("pio"), Path.home() / ".platformio/penv/bin/pio"):
        if candidate and Path(candidate).exists():
            return str(candidate)
    raise SetupError("PlatformIO isn't installed on the Pi yet — see README "
                     "'Adding or updating a node' for the one-time install.")


def update_arduino(node_id, node, port, dry_run):
    flags = arduino_build_flags(node_id, node)
    env_name = arduino_env(node)
    print(f"[SETUP] Build {env_name} for node {node_id} with: {' '.join(flags)}")
    if dry_run:
        print(f"[SETUP] Would upload to {port}.")
        return False
    command = [find_pio(), "run", "-d", str(ARDUINO_DIR), "-e", env_name,
               "-t", "upload", "--upload-port", port]
    print("[SETUP] Building and uploading (the first build on the Pi takes several minutes)...")
    result = subprocess.run(command, env={**os.environ, "PLATFORMIO_BUILD_FLAGS": " ".join(flags)})
    if result.returncode != 0:
        raise SetupError("PlatformIO build/upload failed — see its output above.")
    return True


# ── Finding what's plugged in ────────────────────────────────────────────────

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
    print(f"[SETUP] It didn't answer as node {node_id}" + (f" (it says it's node {answered})" if answered else "") + ".")
    if framework == "circuitpython":
        output = console_output()
        if output:
            lines = [l for l in output.strip().splitlines() if l.strip()]
            print("[SETUP] Its console, restarting code.py:\n          " + "\n          ".join(lines[-20:]))
        else:
            print("[SETUP] Couldn't read its console either; look with:\n"
                  "          python3 -m serial.tools.miniterm /dev/ttyACM0 115200")
    return False


def run(args):
    nodes = load_nodes()

    label = bootloader_drive()
    if args.install or label:
        if label and not args.install:
            print(f"[SETUP] A board is waiting in its bootloader ({label} drive) — installing CircuitPython.")
        install_circuitpython(args, nodes, label)
        return

    if Path(device(CIRCUITPY_LABEL)).exists():
        framework = "circuitpython"
        with mounted(device(CIRCUITPY_LABEL), MOUNT_POINT, read_only=True) as drive:
            detected = node_id_on_drive(drive)
        port = None
        print(f"[SETUP] Found a CircuitPython board (CIRCUITPY drive), "
              f"{'node ' + str(detected) if detected else 'not set up as a node yet'}.")
    else:
        ports = [args.port] if args.port else serial_ports()
        if not ports:
            raise SetupError("Nothing plugged in: no CIRCUITPY drive and no USB serial port.")
        framework = "arduino"
        detected, port = ask_node(ports)
        if port is None:
            if not args.port and len(ports) > 1:
                raise SetupError(f"No node answered on {', '.join(ports)}; name the board's port with --port.")
            port = ports[0]
        print(f"[SETUP] Found an Arduino board on {port}, "
              f"{'node ' + str(detected) if detected else 'not running node firmware'}.")

    node_id = args.node or detected
    if node_id is None:
        raise SetupError("Can't tell which node this board is; say with --node N.")
    if args.node and detected and args.node != detected:
        print(f"[SETUP] It's currently node {detected}; setting it up as node {args.node} instead.")
    if node_id not in nodes:
        raise SetupError(f"Node {node_id} isn't in nodes.json yet — add it there first.")
    node = nodes[node_id]
    if node["framework"] != framework:
        raise SetupError(f"nodes.json says node {node_id} runs {node['framework']}, but this board "
                         f"is running {framework}. Switching frameworks isn't automated yet.")
    print(f"[SETUP] Node {node_id}: {node['name']} ({node['framework']}, {node['board']})")

    if framework == "circuitpython":
        updated = update_circuitpython(node_id, node, args.dry_run)
    else:
        updated = update_arduino(node_id, node, port, args.dry_run)
    if updated:
        confirm_running(node_id, framework)


def list_nodes():
    for node_id, node in load_nodes().items():
        sleep = node.get("sleep_window")
        print(f"  {node_id}: {node['name']:<24} {node['framework']:<13} {node['board']:<11} "
              f"log every {node['log_interval_s']}s to {node.get('storage', 'sd')}"
              + (f", asleep {sleep[0]}-{sleep[1]}" if sleep else ""))


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", nargs="?", choices=["update", "list"], default="update")
    parser.add_argument("--node", type=int, help="node ID to set the board up as")
    parser.add_argument("--install", action="store_true",
                        help=f"install CircuitPython {CIRCUITPYTHON_VERSION} on the board first")
    parser.add_argument("--port", help="the board's serial port, if more than one is plugged in (Arduino)")
    parser.add_argument("--dry-run", action="store_true", help="show what would change, change nothing")
    args = parser.parse_args()
    try:
        if args.action == "list":
            list_nodes()
        else:
            run(args)
    except (NodeConfigError, SetupError) as e:
        sys.exit(f"[SETUP] {e}")
    except subprocess.CalledProcessError as e:
        sys.exit(f"[SETUP] {' '.join(e.cmd)} failed (exit {e.returncode}).")


if __name__ == "__main__":
    main()
