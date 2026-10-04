'''
Python 3 running on Raspberry Pi 3B

Update a node plugged into the Pi by USB to the code in this repo, with its
settings from nodes.json:

    python3 node_setup.py              — update whichever node is plugged in
    python3 node_setup.py --dry-run    — show what would change, change nothing
    python3 node_setup.py --node 3     — set the board up as node 3 (one that isn't
                                         a node yet, or to change its ID)
    python3 node_setup.py list         — show the nodes in nodes.json

CircuitPython: mounts the CIRCUITPY drive and copies over whichever files
differ: the code in circuitpython/, the libraries it uses, and a
node_config.py written from nodes.json. The board restarts on its own.
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
from pathlib import Path

from nodes import (
    NodeConfigError, load_nodes, circuitpython_config, arduino_env, arduino_build_flags,
)

REPO             = Path(__file__).resolve().parent.parent
CIRCUITPY_SRC    = REPO / "circuitpython"
ARDUINO_DIR      = REPO / "arduino"
CIRCUITPY_DEVICE = "/dev/disk/by-label/CIRCUITPY"
MOUNT_POINT      = Path("/mnt/circuitpy")

# The node's own code, copied in this order: code.py last, so the board's
# auto-reload doesn't start the new code before the rest is in place.
CIRCUITPY_FILES = ("hardware_setup_garden.py", "communication_garden.py",
                   "sync_garden.py", "boot.py", "code.py")
# Libraries the node code imports (and theirs). Other libraries already on
# the drive are kept up to date too, but unused ones aren't added.
CIRCUITPY_LIBS = ("adafruit_bus_device", "adafruit_register", "adafruit_ticks.mpy",
                  "adafruit_rfm69.mpy", "adafruit_pcf8523", "adafruit_max1704x.mpy",
                  "adafruit_ltr390.mpy", "adafruit_seesaw", "adafruit_sht4x.mpy",
                  "adafruit_sgp40", "adafruit_ina23x.mpy")

_NODE_ID_LINE = re.compile(rb"^NODE_ID\s*=\s*(\d+)", re.MULTILINE)


class SetupError(Exception):
    pass


# ── CircuitPython ────────────────────────────────────────────────────────────

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


def plan_circuitpython(drive, node_id, node):
    '''The files to write to the drive, as [(path on drive, contents)], only where they differ.'''
    drive = Path(drive)
    wanted = []

    on_drive = {p.name for p in (drive / "lib").iterdir()} if (drive / "lib").is_dir() else set()
    in_repo = {p.name for p in (CIRCUITPY_SRC / "lib").iterdir()}
    for entry in sorted(set(CIRCUITPY_LIBS) | (on_drive & in_repo)):
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
    if hasattr(os, "sync"):   # flush to the drive before it can be unplugged
        os.sync()


def mounted_at(device):
    '''Where device is already mounted, or None.'''
    real = os.path.realpath(device)
    try:
        for line in Path("/proc/mounts").read_text().splitlines():
            dev, where = line.split()[:2]
            if os.path.realpath(dev) == real:
                return Path(where.replace("\\040", " "))
    except OSError:
        pass
    return None


def update_circuitpython(node_id, node, dry_run):
    drive = mounted_at(CIRCUITPY_DEVICE)
    we_mounted = drive is None
    if we_mounted:
        drive = MOUNT_POINT
        options = f"uid={os.getuid()},gid={os.getgid()}" + (",ro" if dry_run else "")
        subprocess.run(["sudo", "mkdir", "-p", str(drive)], check=True)
        subprocess.run(["sudo", "mount", "-o", options, CIRCUITPY_DEVICE, str(drive)], check=True)
    try:
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
        print("[SETUP] Done. CircuitPython restarts the code by itself.")
        if any(rel.name == "boot.py" for rel, _ in plan):
            print("[SETUP] boot.py changed: press the board's reset button (or unplug it)\n"
                  "        so it takes effect — until then the USB data port may be missing.")
            return False
        return True
    finally:
        if we_mounted:
            subprocess.run(["sudo", "umount", str(drive)], check=False)


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


def ask_node(paths):
    '''(node ID, port) of a running node on one of paths, or (None, None).'''
    from usb_sync import find_node   # needs pyserial
    node, info = find_node(paths)
    if node is None:
        return None, None
    node.port.close()
    return info["n"], node.port.port


def confirm_running(node_id, wait=8):
    '''After an update, check the node answers over USB with the right ID.'''
    print(f"[SETUP] Checking the node answers ({wait}s for it to restart)...")
    time.sleep(wait)
    answered, _ = ask_node(serial_ports())
    if answered == node_id:
        print(f"[SETUP] Node {node_id} is running the new code.")
    else:
        print(f"[SETUP] It didn't answer as node {node_id} (got {answered}). Check its "
              "console output; a CircuitPython error shows there.")


def run(args):
    nodes = load_nodes()

    if Path(CIRCUITPY_DEVICE).exists():
        framework = "circuitpython"
        drive = mounted_at(CIRCUITPY_DEVICE)
        detected = node_id_on_drive(drive) if drive else None
        if detected is None and drive is None:
            # Read it from the drive itself; mount read-only just for this.
            subprocess.run(["sudo", "mkdir", "-p", str(MOUNT_POINT)], check=True)
            subprocess.run(["sudo", "mount", "-o", f"ro,uid={os.getuid()}", CIRCUITPY_DEVICE,
                            str(MOUNT_POINT)], check=True)
            try:
                detected = node_id_on_drive(MOUNT_POINT)
            finally:
                subprocess.run(["sudo", "umount", str(MOUNT_POINT)], check=False)
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
        confirm_running(node_id)


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
