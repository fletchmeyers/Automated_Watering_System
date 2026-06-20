'''
Python 3 running on Raspberry Pi 3B

Outbound command functions: poll nodes, request bulk sync, set interval.
Also provides sensor_health_report() for diagnostics.

Written by Fletcher Meyers
March 2026
'''

import json
import time
from datetime import datetime
from pathlib import Path


DATA_FILE    = Path(__file__).parent / "data_from_pico.txt"
COMMAND_FILE = "/tmp/pico_command.json"


# ── Outbound commands ─────────────────────────────────────────────────────────

def request_poll(node_id=1):
    '''
    Write a poll command to the command file.
    Always includes the current time so the Pico can silently update its RTC —
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


def request_sync(node_id=1):
    '''
    Explicit RTC sync (writes a "sync" command rather than a "poll").
    Kept for manual/cron use; routine time updates now piggyback on poll packets.

    # TODO: if handle_poll's silent RTC update proves reliable, remove this
    # and the sync/sync_ack packet type from the protocol entirely.
    '''
    command = {
        "t":  "sync",
        "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "n":  node_id,
    }
    Path(COMMAND_FILE).write_text(json.dumps(command))
    print(f"[SYNC] Command written: {command}")


# ── Diagnostics ───────────────────────────────────────────────────────────────

def sensor_health_report(filepath=DATA_FILE, n=100):
    '''
    Tail the last `n` lines of `filepath`, count distinct sensor packet types,
    and report how many were seen vs. how many were expected.

    Returns a dict with keys: window, expected, seen, types, missing.
    '''
    _NON_SENSOR_TYPES = {"ts", "sync_ack", "sync", "sync_complete"}
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


# ── Entry point (manual / cron) ───────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "sync":
        request_bulk_sync()
    else:
        request_poll()
        print("\n[HEALTH] Running sensor health check...")
        sensor_health_report(DATA_FILE, n=100)

