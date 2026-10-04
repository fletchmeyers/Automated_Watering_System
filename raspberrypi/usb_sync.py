'''
Python 3 running on Raspberry Pi 3B

Pull a node's whole sync backlog over a USB cable instead of the radio:
much faster, for when a node has days of readings waiting. Plug the node
into the Pi and run:

    python3 usb_sync.py              — find the node on any USB serial port
    python3 usb_sync.py /dev/ttyACM0 — or name the port

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
from sync_indoor import USB_SYNC_FILE, save_node_info

CHUNK_LINES   = 200   # lines per request — nothing is lost over USB, so go big
REPLY_TIMEOUT = 10    # seconds to wait for the end of a chunk
MAX_RETRIES   = 3


class UsbNode:
    '''A node on a serial port, spoken to with one JSON object per line.'''

    def __init__(self, port):
        self.port = port

    def send(self, command):
        self.port.write(json.dumps(command, separators=(",", ":")).encode() + b"\n")

    def packets(self, timeout):
        '''Yield each JSON packet the node sends until timeout passes with nothing new.'''
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            raw = self.port.readline()
            if not raw:
                continue
            raw = raw.strip()
            if raw[:1] != b"{":
                continue                      # the M0's debug output
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


def open_port(path):
    import serial   # pyserial; imported here so the tests don't need it
    return serial.Serial(path, 115200, timeout=0.2)


def find_node(paths):
    '''Return (UsbNode, info reply) for the first port that answers "info".'''
    for path in paths:
        try:
            port = open_port(path)
        except Exception as e:
            print(f"[USB] {path}: could not open ({e})")
            continue
        port.reset_input_buffer()
        node = UsbNode(port)
        info, _ = node.ask({"t": "info"}, "info", timeout=3)
        if info is not None and isinstance(info.get("n"), int):
            print(f"[USB] {path}: node {info['n']}")
            return node, info
        print(f"[USB] {path}: no answer")
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


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("port", nargs="?", help="serial port (default: try each /dev/ttyACM* and /dev/ttyUSB*)")
    args = parser.parse_args()

    paths = [args.port] if args.port else sorted(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*"))
    if not paths:
        sys.exit("[USB] No USB serial ports found — is the node plugged in?")

    node, info = find_node(paths)
    if node is None:
        sys.exit("[USB] No node answered. A Pico needs boot.py copied over and a reset first.")
    node_id = info["n"]
    save_node_info(info)

    flag = Path(USB_SYNC_FILE)
    flag.write_text(json.dumps({"n": node_id, "pid": os.getpid()}))
    try:
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
    finally:
        flag.unlink(missing_ok=True)
        node.port.close()


if __name__ == "__main__":
    main()
