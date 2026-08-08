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

# Fresh poll results, written by main.py the instant a poll's batch completes,
# so /api/poll can return real sensor values immediately instead of waiting
# on push_data.sh -> GitHub -> Pages CDN to publish the static file.
POLL_RESULT_FILE = "/tmp/pico_poll_result.json"

# Ping test request/result handoff — deliberately separate from COMMAND_FILE
# since the ping loop bypasses CommandManager entirely (see run_ping_test()
# in communication_indoor.py for why).
PING_REQUEST_FILE  = "/tmp/pico_ping_request.json"
PING_RESULT_FILE   = "/tmp/pico_ping_result.json"
# Updated after each individual ping (not just at the end) so the dashboard
# can poll this for a live "x/y pong" readout while a test is running.
PING_PROGRESS_FILE = "/tmp/pico_ping_progress.json"

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
    # Clear any stale result from a previous poll so wait_for_poll_result()
    # can't pick up an old file before this poll has actually completed.
    Path(POLL_RESULT_FILE).unlink(missing_ok=True)
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


def request_ping_test(node_id=1, count=10):
    '''
    Write a ping test request. main.py picks this up on its next loop
    iteration (only when nothing else is pending) and runs count back-to-back
    ping/pong round trips, writing the result to PING_RESULT_FILE when done.
    '''
    Path(PING_REQUEST_FILE).write_text(json.dumps({"n": node_id, "count": count}))
    # Clear any stale result/progress from a previous test so the dashboard
    # can't briefly show old data before the new test has actually started.
    Path(PING_RESULT_FILE).unlink(missing_ok=True)
    Path(PING_PROGRESS_FILE).unlink(missing_ok=True)
    print(f"[PING] Test requested: node={node_id}, count={count}")


def wait_for_poll_result(timeout=WAIT_TIMEOUT):
    '''
    Block until POLL_RESULT_FILE appears, then return its parsed packet list.
    Used by /api/poll to hand back real sensor values the moment main.py has
    them, instead of the old approach of watching DATA_FILE's mtime and then
    making the browser wait on push_data.sh -> GitHub -> Pages to publish it.
    '''
    print(f"[POLL] Waiting up to {timeout}s for poll result...")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.3)
        if Path(POLL_RESULT_FILE).exists():
            try:
                packets = json.loads(Path(POLL_RESULT_FILE).read_text())
                print(f"[POLL] Result received ({len(packets)} packets).")
                return packets
            except Exception:
                continue
    print("[POLL] Timed out waiting for poll result.")
    return None


def get_ping_progress():
    '''
    Read the current in-progress ping test state, if any. Returns None if no
    test is running / no progress file exists yet. Cheap, non-blocking read —
    meant to be polled frequently by the dashboard while a test is in flight.
    '''
    try:
        return json.loads(Path(PING_PROGRESS_FILE).read_text())
    except Exception:
        return None


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


def wait_for_ping_result(timeout=20):
    '''
    Block until PING_RESULT_FILE appears, then return its parsed contents.
    Poll frequently (0.2s) since the whole test is expected to finish in a
    few seconds — this shouldn't feel like the longer 1s-interval waits used
    for polls/syncs.
    '''
    print(f"[PING] Waiting up to {timeout}s for ping test result...")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.2)
        if Path(PING_RESULT_FILE).exists():
            try:
                result = json.loads(Path(PING_RESULT_FILE).read_text())
                print("[PING] Result received.")
                return result
            except Exception:
                continue
    print("[PING] Timed out waiting for ping test result.")
    return None


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

    elif args[0] == "ping":
        request_ping_test()
        result = wait_for_ping_result()
        if result:
            print(f"[PING] {result['hits']}/{result['count']} pongs, "
                  f"avg {result['avg_rtt_ms']}ms")

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
        print("Usage: python3 sync_indoor.py [poll|sync|health|ping|interval <seconds>]")
        sys.exit(1)