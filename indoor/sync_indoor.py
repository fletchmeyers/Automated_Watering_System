'''
Python 3 running on Raspberry Pi 3B

Send a time-sync command to the Pico and wait for acknowledgement + sensor burst.
Also provides a sensor health report based on the last N packets in the data file.

Can be run directly (manually or via cron) or imported by main.py.

Crontab example — sync every hour:
    0 * * * * /usr/bin/python3 /home/pi/sync_indoor.py >> /home/pi/sync.log 2>&1

Written by Fletcher Meyers
March 2026
'''

import json
import time
from datetime import datetime
from pathlib import Path

# Data file written by main.py on the Pi
DATA_FILE = Path(__file__).parent / "data_from_pico.txt"

# How long to wait for the Pico's sync_ack after sending the sync command (seconds)
ACK_TIMEOUT = 10

# How long to wait for each follow-up sensor packet after the ack (seconds)
BURST_PACKET_TIMEOUT = 3


# ── Sync functions ────────────────────────────────────────────────────────────

COMMAND_FILE = "/tmp/pico_command.json"

def request_sync():
    command = {"t": "sync", "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S")}
    Path(COMMAND_FILE).write_text(json.dumps(command))
    print(f"[SYNC] Command written: {command}")



def sensor_health_report(filepath=DATA_FILE, n=100):
    '''
    Tail the last `n` lines of `filepath`, count distinct sensor packet types,
    and report how many were seen vs how many were expected.

    "Expected" is inferred from the same window: any type that appeared at least
    once is considered an expected type. This means the report will flag sensors
    that drop out mid-window but correctly ignores sensors that have never appeared
    (e.g. a sensor that wasn't connected when the file was first written).

    Returns a dict:
        {
            "window":   int,           — number of data packets examined (excl. control)
            "expected": int,           — distinct sensor types seen in window
            "seen":     int,           — same as expected (by definition of inference)
            "types":    dict[str,int], — {sensor_type: packet_count}
            "missing":  list[str],     — types seen earlier in window but absent recently
        }

    Prints a human-readable summary.
    '''
    _NON_SENSOR_TYPES = {"ts", "sync_ack", "sync"}
    try:
        with open(filepath, "r") as f:
            lines = f.readlines()
    except FileNotFoundError:
        print(f"[HEALTH] Data file not found: {filepath}")
        return {}

    # Take the last n lines, parse them, skip control packet types
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
            pass  # malformed line, skip silently

    if not sensor_packets:
        print("[HEALTH] No sensor packets found in the last", len(tail), "lines.")
        return {}

    # Count types across the full window
    type_counts = {}
    for pkt in sensor_packets:
        t = pkt.get("t", "unknown")
        type_counts[t] = type_counts.get(t, 0) + 1

    all_types = set(type_counts.keys())

    # Detect types that appeared early in the window but are absent in the
    # most recent quarter — a sign a sensor has gone offline recently.
    recent_cutoff = max(1, len(sensor_packets) * 3 // 4)
    recent_packets = sensor_packets[recent_cutoff:]
    recent_types = {p.get("t") for p in recent_packets}
    missing = sorted(all_types - recent_types)

    report = {
        "window":   len(sensor_packets),
        "expected": len(all_types),
        "seen":     len(all_types),
        "types":    type_counts,
        "missing":  missing,
    }

    # Human-readable summary
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

def check_and_forward_command(radio):
    cmd_path = Path(COMMAND_FILE)
    if not cmd_path.exists():
        return False
    try:
        command = json.loads(cmd_path.read_text())
        packet = json.dumps(command, separators=(",", ":"))
        radio.send(bytes(packet, "utf-8"))
        print(f"[SYNC] Forwarded command to Pico: {command}")
        return True
    except Exception as e:
        print(f"[SYNC] Failed to send command: {e}")
        return False
    finally:
        cmd_path.unlink(missing_ok=True)
        
# ── Entry point (cron / manual) ───────────────────────────────────────────────

if __name__ == "__main__":
    request_sync()
    print("\n[HEALTH] Running sensor health check...")
    sensor_health_report(DATA_FILE, n=100)