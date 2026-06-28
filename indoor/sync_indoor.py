'''
Python 3 running on Raspberry Pi 3B

Outbound command functions: poll nodes, request bulk sync, set interval.
Also provides sensor_health_report() for diagnostics.

Usage (while main.py is running):
    python3 sync_indoor.py            — request a poll, wait for new data, run health report
    python3 sync_indoor.py sync       — request a bulk SD sync, wait for completion
    python3 sync_indoor.py health     — run health report only
    python3 sync_indoor.py interval N — set sense interval to N seconds

Written by Fletcher Meyers
March 2026
'''

import json
import time
from datetime import datetime
from pathlib import Path


DATA_FILE    = Path(__file__).parent / "data_from_pico.txt"
COMMAND_FILE = "/tmp/pico_command.json"

WAIT_TIMEOUT = 90   # seconds before giving up waiting for a response


# ── Outbound commands ─────────────────────────────────────────────────────────

def request_poll(node_id=1):
    '''
    Write a poll command to the command file.
    Always includes the current time so the Pico silently updates its RTC —
    no separate sync/sync_ack round trip needed for routine clock maintenance.
    '''
    command = {
        "t":  "poll",
        "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "n":  node_id,
    }
    Path(COMMAND_FILE).write_text(json.dumps(command))
    print(f"[POLL] Command written: {command}")


def request_bulk_sync(node_id=1):
    '''
    Write a sync_request command to the command file.
    The Pico will rename its data.txt → sending.txt and stream the contents
    back in chunks, waiting for a per-chunk ack before advancing.
    '''
    command = {"t": "sync_request", "n": node_id}
    Path(COMMAND_FILE).write_text(json.dumps(command))
    print(f"[SYNC] Command written: {command}")


def request_set_interval(seconds, node_id=1):
    '''Write a set_interval command. seconds must be a positive integer.'''
    if not isinstance(seconds, int) or seconds <= 0:
        print("[INTERVAL] seconds must be a positive integer.")
        return
    command = {"t": "set_interval", "v": seconds, "n": node_id}
    Path(COMMAND_FILE).write_text(json.dumps(command))
    print(f"[INTERVAL] Command written: {command}")


# ── Wait helpers (for use alongside a running main.py) ───────────────────────

def wait_for_new_data(timeout=WAIT_TIMEOUT):
    '''
    Block until a new line appears in the data file, then return.
    Used after request_poll() to confirm main.py forwarded the command
    and the Pico responded.
    '''
    filepath = DATA_FILE
    try:
        before = filepath.stat().st_mtime if filepath.exists() else 0
    except OSError:
        before = 0

    print(f"[POLL] Waiting up to {timeout}s for new data...")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(1)
        try:
            after = filepath.stat().st_mtime
            if after > before:
                print("[POLL] New data received.")
                return True
        except OSError:
            pass
    print("[POLL] Timed out waiting for poll response.")
    return False


def wait_for_sync_complete(timeout=WAIT_TIMEOUT):
    '''
    Block until the command file disappears, which main.py does when it
    receives sync_complete from the Pico. Returns True on success.
    '''
    print(f"[SYNC] Waiting up to {timeout}s for bulk sync to complete...")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(1)
        if not Path(COMMAND_FILE).exists():
            print("[SYNC] Bulk sync confirmed complete.")
            return True
    print("[SYNC] Timed out waiting for sync_complete.")
    return False


def wait_for_interval_ack(timeout=WAIT_TIMEOUT):
    '''
    Block until the command file disappears, indicating main.py received
    a set_interval_ack from the Pico.
    '''
    print(f"[INTERVAL] Waiting up to {timeout}s for interval ack...")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(1)
        if not Path(COMMAND_FILE).exists():
            print("[INTERVAL] Interval change confirmed.")
            return True
    print("[INTERVAL] Timed out waiting for set_interval_ack.")
    return False


# ── Diagnostics ───────────────────────────────────────────────────────────────

def sensor_health_report(filepath=DATA_FILE, n=100):
    '''
    Tail the last `n` lines of `filepath`, count distinct sensor packet types,
    and report how many were seen vs. how many were expected.

    Returns a dict with keys: window, expected, seen, types, missing.
    '''
    _NON_SENSOR_TYPES = {"ts", "sync_complete"}
    try:
        with open(filepath, "r") as f:
            lines = f.readlines()
    except FileNotFoundError:
        print(f"[HEALTH] Data file not found: {filepath}")
        return {}

    tail = lines[-n:] if len(lines) >= n else lines
    sensor_packets = []
    for line in tail:
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            if data.get("t") not in _NON_SENSOR_TYPES:
                sensor_packets.append(data)
        except Exception:
            pass

    if not sensor_packets:
        print("[HEALTH] No sensor packets found in the last", len(tail), "lines.")
        return {}

    type_counts = {}
    for pkt in sensor_packets:
        t = pkt.get("t", "unknown")
        type_counts[t] = type_counts.get(t, 0) + 1

    all_types = set(type_counts.keys())

    recent_cutoff = max(1, len(sensor_packets) * 3 // 4)
    recent_types  = {p.get("t") for p in sensor_packets[recent_cutoff:]}
    missing       = sorted(all_types - recent_types)

    report = {
        "window":   len(sensor_packets),
        "expected": len(all_types),
        "seen":     len(all_types),
        "types":    type_counts,
        "missing":  missing,
    }

    print(f"\n[HEALTH] Sensor health report (last {len(tail)} lines, "
          f"{len(sensor_packets)} sensor packets)")
    print(f"  Distinct sensor types : {len(all_types)}")
    for t, count in sorted(type_counts.items()):
        status = " ← possibly offline" if t in missing else ""
        print(f"    {t:12s}  {count:4d} packets{status}")
    if missing:
        print(f"  WARNING: {len(missing)} type(s) absent from recent packets: {missing}")
    else:
        print("  All sensor types present in recent packets.")
    print()

    return report


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    args = sys.argv[1:]

    if not args or args[0] == "poll":
        request_poll()
        wait_for_new_data()
        sensor_health_report()

    elif args[0] == "sync":
        request_bulk_sync()
        wait_for_sync_complete()

    elif args[0] == "health":
        sensor_health_report()

    elif args[0] == "interval":
        if len(args) < 2:
            print("Usage: python3 sync_indoor.py interval <seconds>")
            sys.exit(1)
        try:
            seconds = int(args[1])
        except ValueError:
            print("Interval must be a positive integer.")
            sys.exit(1)
        request_set_interval(seconds)
        wait_for_interval_ack()

    else:
        print("Usage: python3 sync_indoor.py [poll|sync|health|interval <seconds>]")
        sys.exit(1)

