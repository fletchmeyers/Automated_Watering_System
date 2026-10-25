'''
CircuitPython 10.0.3 running on Pico 2W RP2350

Command handlers for packets received from the Pi.
The Pico is always in listen mode between sense cycles; incoming packets
are dispatched here based on their "t" (type) field.

Written by Fletcher Meyers
March 2026
'''

import json
import time

# Longest sleep a node will accept, as a guard against a bad wake time
# (the Arduino nodes' SLEEP_MAX_S).
SLEEP_MAX_S = 16 * 3600

# Set by handle_sleep() when the Pi's "sleep" command was accepted: the wake
# time as "YYYY-MM-DDTHH:MM:SS". code.py sleeps until then and clears it.
requested_wake = None


def _parse_iso_timestamp(ts_str):
    '''
    Parse an ISO-8601 string "YYYY-MM-DDTHH:MM:SS" into a time.struct_time.
    CircuitPython's time module does not have strptime, so we parse manually.
    '''
    try:
        date_part, time_part = ts_str.split("T")
        year, month, day     = (int(x) for x in date_part.split("-"))
        hour, minute, second = (int(x) for x in time_part.split(":"))
        return time.struct_time((year, month, day, hour, minute, second, 0, -1, -1))
    except Exception as e:
        raise ValueError(f"Bad timestamp '{ts_str}': {e}")


def _seconds(ts_str):
    '''Seconds since 2000-01-01 for an ISO timestamp, by hand: the same on
    every board, whatever epoch its time.mktime() uses.'''
    t = _parse_iso_timestamp(ts_str)
    year, month = t.tm_year, t.tm_mon
    if month <= 2:   # count March as the first month, so leap days come last
        year -= 1
        month += 12
    days = 365 * year + year // 4 - year // 100 + year // 400 + (153 * (month - 3) + 2) // 5 + t.tm_mday
    return (days - 730426) * 86400 + t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec


def check_for_command(radio, timeout=0.5):
    '''
    Non-blocking radio listen. Returns a parsed dict or None.
    Keep timeout short (≤0.5s) so the sense loop stays on schedule.
    '''
    packet = radio.receive(timeout=timeout, with_header=True)
    if packet is None:
        return None
    try:
        import node_events
        node_events.radio.heard()   # anything at all shows the radio is hearing (node_events.RadioWatch)
    except ImportError:
        pass
    payload = packet[4:]
    if payload[:1] == b"~":
        return None   # a fragment of another node's oversized packet — never a command
    try:
        from communication_garden import decode_packet   # JSON or MessagePack
        command = decode_packet(payload)
        return command if isinstance(command, dict) else None
    except Exception as e:
        print("[CMD] Could not parse packet:", e)
        return None


def handle_poll(command, sender, get_timestamp_fn, send_latest_fn):
    '''
    Respond to a "poll" request from the Pi.
    Every poll includes a "ts" field used to silently keep the RTC in sync —
    no separate sync command or ack needed.
    '''
    ts_str = command.get("ts")
    if ts_str:
        try:
            from hardware_setup_garden import rtc
            rtc.datetime = _parse_iso_timestamp(ts_str)
        except Exception as e:
            print("[POLL] RTC update failed:", e)

    timestamp = get_timestamp_fn()
    send_latest_fn(sender, timestamp)
    print(f"[POLL] Latest reading sent (ts={timestamp}).")


def handle_sync(command, sender, send_sync_chunk_fn):
    '''
    Respond to a "sync" request from the Pi with one chunk of SD data.
    Delegates to send_sync_chunk() in communication_garden.py, which handles
    the file rotation, cursor, and chunk transfer.
    '''
    send_sync_chunk_fn(sender, command)


def handle_ping(command, sender):
    '''
    Respond to a "ping" with an immediate "pong" echoing the same q value
    (as "pq", since sender.send() overwrites "q" with its own sequence).
    No sensor reads or SD writes — this exists purely to measure radio
    round-trip time and drop rate as fast as possible.
    '''
    sender.send({"t": "pong", "pq": command.get("q")})


def handle_sleep(command, sender, get_timestamp_fn):
    '''
    The Pi's {"t": "sleep", "w": "YYYY-MM-DDTHH:MM:SS"}: answer with
    sleep_ack (ok 1, or ok 0 and why), and if accepted set requested_wake
    for code.py, which switches the links off until then (see
    arduino/src/node_sleep.h, which this matches). The wake time is
    absolute, so a retried command can't push it later.
    '''
    global requested_wake
    w = command.get("w")
    now = get_timestamp_fn()
    refused = None
    if now < "2024":
        refused = "clock not set"
    else:
        try:
            left = _seconds(w) - _seconds(now)
            if left <= 0:
                refused = "wake time already passed"
            elif left > SLEEP_MAX_S:
                refused = "longer than SLEEP_MAX_S"
        except (ValueError, AttributeError):
            refused = "bad wake time"

    ack = {"t": "sleep_ack", "ok": 0 if refused else 1, "w": w}
    if refused:
        ack["why"] = refused
    sender.send(ack)
    if refused:
        print(f"[SLEEP] Refused: {refused}")
    else:
        requested_wake = w


def handle_set_interval(command, sender):
    '''Update the sense interval. v must be a positive integer (seconds).'''
    v = command.get("v")
    if not isinstance(v, int) or v <= 0:
        print(f"[INTERVAL] Invalid interval value: {v!r} — must be a positive integer.")
        return None
    print(f"[INTERVAL] Sense interval updated to {v}s.")
    time.sleep(1)
    sender.send({"t": "set_interval_ack", "v": v})
    return v


def dispatch_command(command, sender, radio, rtc, get_timestamp_fn, send_latest_fn, send_sync_chunk_fn, node_id,
                     send_info_fn=None):
    '''
    Central dispatcher — call this from code.py whenever check_for_command()
    returns a packet. Routes to the appropriate handler based on packet type.
    Returns the new SENSE_INTERVAL if set_interval was received, else None.

    node_id is this node's own NODE_ID. Every node on this frequency/key
    hears every packet — the Pi's commands to other nodes, other nodes'
    replies, and their sync lines (which carry no "n" at all) — so anything
    not addressed to this node is ignored.
    '''
    if command is None or command.get("n") != node_id:
        return None
    print("[CMD] Received:", command)

    t = command.get("t")

    if t == "poll":
        handle_poll(command, sender, get_timestamp_fn, send_latest_fn)

    elif t == "ping":
        handle_ping(command, sender)

    elif t == "sync":
        handle_sync(command, sender, send_sync_chunk_fn)

    elif t == "info" and send_info_fn is not None:
        send_info_fn(sender)

    elif t == "set_interval":
        return handle_set_interval(command, sender)

    elif t == "sleep":
        handle_sleep(command, sender, get_timestamp_fn)

    elif t == "data_ack":
        print(f"[ACK] Pi confirmed batch (q={command.get('q')}).")

    else:
        print(f"[CMD] Unknown packet type: {t!r}")

    return None