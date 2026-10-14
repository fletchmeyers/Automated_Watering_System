'''
Python 3 running on Raspberry Pi 3B

Pull a node's whole sync backlog over a USB cable instead of the radio:
much faster, for when a node has days of readings waiting. The garden-usb
service does it by itself whenever a node is plugged into the Pi:

    python3 usb_sync.py --watch      — the service: watch for nodes being
                                       plugged in and pull each one's backlog
    python3 usb_sync.py              — once, now: find the node on any USB
                                       serial port
    python3 usb_sync.py /dev/ttyACM0 — or name the port

The watcher asks each board that appears for its "info" (a few seconds
after it appears, so it has started), and pulls its log if anything is
waiting; then leaves that board alone until it's unplugged and plugged in
again. It stays off the ports while node_setup.py runs (NODE_SETUP_FILE),
and node_setup.py waits for a pull under way to finish.

It sends the same "sync" requests as the radio side (see send_sync_chunk()
in communication_garden.py), just much bigger chunks with nothing lost on
the way, and stores the lines the same way (sync archive + sensors.db).
main.py keeps running: while this holds USB_SYNC_FILE, it leaves this
node's radio sync alone, and afterwards picks up from wherever the node's
cursor has got to. Needs pyserial (sudo apt install python3-serial).

The Pico needs boot.py (it adds the USB data port this talks to) and a
reset after copying it over. The M0 shares its one USB port with its
debug output, so anything that isn't a JSON packet is skipped.

Written by Fletcher Meyers
October 2026
'''

import argparse
import glob
import json
import os
import sys
import time
from pathlib import Path

import db
from communication_indoor import store_sync_lines
from sync_indoor import NODE_SETUP_FILE, USB_SYNC_FILE, held_by, save_node_info

CHUNK_LINES   = 200   # lines per request — nothing is lost over USB, so go big
REPLY_TIMEOUT = 10    # seconds to wait for the end of a chunk
MAX_RETRIES   = 3

WATCH_EVERY   = 3     # seconds between looks at the USB ports
SETTLE        = 5     # seconds a new board gets to start up before it's asked
GIVE_UP       = 60    # seconds a new board keeps being asked before it's left alone


class UsbNode:
    '''A node on a serial port, spoken to with one JSON object per line.'''

    def __init__(self, port):
        self.port = port
        self._partial = b""   # a line cut off by the port's read timeout, waiting for the rest
        self.chatter = []     # the board's last few non-packet lines (its debug output)

    def send(self, command):
        self.port.write(json.dumps(command, separators=(",", ":")).encode() + b"\n")

    def packets(self, timeout):
        '''Yield each JSON packet the node sends until timeout passes with nothing new.'''
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            raw = self.port.readline()
            if not raw:
                continue
            if not raw.endswith(b"\n"):
                # readline() gave up mid-line (the node was still writing it):
                # keep what came and finish the line on the next read.
                self._partial += raw
                continue
            raw, self._partial = (self._partial + raw).strip(), b""
            if raw[:1] != b"{":
                # An Arduino board's debug output: kept (the last few lines)
                # so a failure can show what the board said about it.
                if raw:
                    self.chatter = (self.chatter + [raw.decode(errors="replace")])[-20:]
                continue
            try:
                packet = json.loads(raw)
            except ValueError:
                continue
            if isinstance(packet, dict):
                deadline = time.monotonic() + timeout
                yield packet

    def ask(self, command, reply_type, timeout=REPLY_TIMEOUT):
        '''Send command; return (reply, lines): the first reply_type packet,
        and any sync lines (packets with no "q") that came before it.'''
        self.send(command)
        lines = []
        for packet in self.packets(timeout):
            if packet.get("t") == reply_type and "q" in packet:
                return packet, lines
            if "q" not in packet:
                lines.append(packet)
        return None, lines


# USB-serial chips (WCH CH9102 on the ESP32 Feather V2, Silicon Labs CP210x,
# FTDI) wire DTR/RTS to the board's reset circuit, so opening the port the
# usual way restarts the board mid-conversation.
_AUTO_RESET_VIDS = (0x1A86, 0x10C4, 0x0403)


def open_port(path):
    import serial   # pyserial; imported here so the tests don't need it
    from serial.tools import list_ports
    vid = next((p.vid for p in list_ports.comports() if p.device == path), None)
    port = serial.Serial()
    port.port, port.baudrate, port.timeout = path, 115200, 0.2
    if vid in _AUTO_RESET_VIDS:
        # Held off before opening, so the board keeps running. (Native-USB
        # boards are left alone: an Arduino Pico only sends while DTR is on.)
        port.dtr = False
        port.rts = False
    port.open()
    return port


def resets_on_open(path):
    '''True for a USB-serial chip (e.g. the ESP32 V2's CH9102): Linux raises
    DTR/RTS for a moment whenever the port opens, before they can be held
    off, which resets the board — so it spends a few seconds booting before
    it can answer.'''
    try:
        from serial.tools import list_ports
    except ImportError:
        return False
    return any(p.device == path and p.vid in _AUTO_RESET_VIDS for p in list_ports.comports())


def find_node(paths, quiet=False):
    '''Return (UsbNode, info reply) for the first port that answers "info".'''
    say = (lambda msg: None) if quiet else print
    for path in paths:
        try:
            port = open_port(path)
        except Exception as e:
            say(f"[USB] {path}: could not open ({e})")
            continue
        port.reset_input_buffer()
        node = UsbNode(port)
        # A board that reset as its port opened needs longer: keep asking.
        deadline = time.monotonic() + (12 if resets_on_open(path) else 0)
        while True:
            info, _ = node.ask({"t": "info"}, "info", timeout=3)
            if info is not None or time.monotonic() >= deadline:
                break
        if info is not None and isinstance(info.get("n"), int):
            say(f"[USB] {path}: node {info['n']}")
            return node, info
        say(f"[USB] {path}: no answer")
        port.close()
    return None, None


def sync_node(node, node_id, db_conn, chunk_lines=CHUNK_LINES, report_every=2000):
    '''Pull every line the node has logged. Returns the number stored.'''
    cursor  = {}     # the node's g/o after the last stored chunk
    stored  = 0
    started = time.monotonic()
    next_report = report_every

    while True:
        command = {"t": "sync", "k": chunk_lines, **cursor}
        for attempt in range(MAX_RETRIES + 1):
            se, lines = node.ask(command, "se")
            if se is not None and len(lines) >= se.get("c", 0):
                break
            got = len(lines)
            want = se.get("c") if se else "?"
            print(f"[USB] Chunk at {cursor or 'start'} came back short ({got}/{want}) — asking again.")
        else:
            raise RuntimeError("node stopped answering over USB")

        for line in lines:
            line.pop("j", None)
        store_sync_lines(node_id, lines, db_conn)
        stored += len(lines)
        cursor = {"g": se.get("g"), "o": se.get("o")}

        if stored >= next_report:
            rate = stored / max(time.monotonic() - started, 0.001)
            print(f"[USB] {stored:,} lines so far ({rate:,.0f}/s), up to {lines[-1].get('ts') if lines else '?'}")
            next_report += report_every
        if not se.get("m") or se.get("c", 0) == 0:
            break

    # The node only moves its cursor when a request confirms it, so confirm
    # the last chunk with an empty one — otherwise its next sync would start
    # by resending it.
    node.ask({"t": "sync", "k": 0, **cursor}, "se")
    return stored


def hold(node_id):
    '''Tell main.py (and node_setup.py) this process is using a node's USB.'''
    Path(USB_SYNC_FILE).write_text(json.dumps({"n": node_id, "pid": os.getpid()}))


def release():
    Path(USB_SYNC_FILE).unlink(missing_ok=True)


def pull_backlog(node, info):
    '''Everything node (found by find_node, with its info reply) has waiting,
    into the database. Returns the number of lines stored.'''
    node_id = info["n"]
    save_node_info(info)
    hold(node_id)
    # Give main.py a moment to see the flag and drop any radio chunk
    # request it already had in flight to this node.
    time.sleep(3)
    db_conn = db.get_connection()
    db_conn.execute("PRAGMA busy_timeout = 30000")   # main.py writes too
    started = time.monotonic()
    try:
        stored = sync_node(node, node_id, db_conn)
    finally:
        db_conn.close()
    minutes = (time.monotonic() - started) / 60
    print(f"[USB] Done: {stored:,} lines from node {node_id} in {minutes:.1f} min.")
    info, _ = node.ask({"t": "info"}, "info", timeout=3)
    if info is not None:
        save_node_info(info)
    return stored


def usb_devices():
    '''The USB serial ports plugged in now, by board: {board: [ports]}. A
    CircuitPython board has two (its console, then the data port boot.py
    adds), so its ports are listed last first: the data port is asked, and
    nothing gets typed into its console.'''
    from serial.tools import list_ports
    boards = {}
    for p in list_ports.comports():
        if not p.device.startswith(("/dev/ttyACM", "/dev/ttyUSB")):
            continue
        key = p.serial_number or (p.location or p.device).split(":")[0]
        boards.setdefault(key, []).append(p.device)
    return {key: sorted(ports, reverse=True) for key, ports in boards.items()}


class Watcher:
    '''The garden-usb service: pulls each node's backlog as it's plugged in.'''

    def __init__(self, devices=usb_devices, find=find_node, pull=pull_backlog, clock=time.monotonic):
        self._devices = devices
        self._find = find
        self._pull = pull
        self._clock = clock
        self._boards = {}   # board -> {"since": first seen, "done": left alone until unplugged}

    def step(self):
        '''One look at the ports: ask (and pull) any board that's due.'''
        now = self._clock()
        boards = self._devices()
        for key in list(self._boards):
            if key not in boards:
                del self._boards[key]   # unplugged: next time it's plugged in, ask again
        for key, ports in boards.items():
            state = self._boards.setdefault(key, {"since": now, "done": False})
            if state["done"] or now - state["since"] < SETTLE:
                continue
            if held_by(NODE_SETUP_FILE) or held_by(USB_SYNC_FILE):
                return   # node_setup.py (or a usb_sync.py by hand) has the ports: wait
            if self._visit(ports) or now - state["since"] >= GIVE_UP:
                state["done"] = True

    def _visit(self, ports):
        '''Ask the board on ports for its info, and pull its log if anything
        is waiting. True once it has answered (or isn't worth asking again).'''
        hold(None)   # node_setup.py waits while this talks to the board
        node = None
        try:
            node, info = self._find(ports, quiet=True)
            if node is None:
                return False   # still starting up, or not a garden node
            waiting = info.get("ub") or 0
            if not waiting:
                print(f"[USB] Node {info['n']} plugged in on {node.port.port}; nothing waiting to sync.")
                return True
            print(f"[USB] Node {info['n']} plugged in on {node.port.port} with {waiting:,} bytes "
                  "waiting; pulling them over USB.")
            self._pull(node, info)
            return True
        except Exception as e:
            print(f"[USB] Pulling from {ports[0]} failed: {e}")
            return True   # don't keep at it; unplug and plug in to try again
        finally:
            release()
            if node is not None:
                node.port.close()

    def run(self):
        print("[USB] Watching for nodes plugged in over USB.")
        while True:
            self.step()
            time.sleep(WATCH_EVERY)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("port", nargs="?", help="serial port (default: try each /dev/ttyACM* and /dev/ttyUSB*)")
    parser.add_argument("--watch", action="store_true",
                        help="keep running, pulling each node's backlog as it's plugged in (garden-usb)")
    args = parser.parse_args()

    if args.watch:
        Watcher().run()
        return

    paths = [args.port] if args.port else sorted(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*"))
    if not paths:
        sys.exit("[USB] No USB serial ports found — is the node plugged in?")

    node, info = find_node(paths)
    if node is None:
        sys.exit("[USB] No node answered. A Pico needs boot.py copied over and a reset first.")
    try:
        pull_backlog(node, info)
    finally:
        release()
        node.port.close()


if __name__ == "__main__":
    main()
