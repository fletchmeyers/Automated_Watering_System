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


def check_for_command(radio, timeout=0.5):
    '''
    Non-blocking radio listen. Returns a parsed dict or None.
    Keep timeout short (≤0.5s) so the sense loop stays on schedule.
    '''
    packet = radio.receive(timeout=timeout, with_header=True)
    if packet is None:
        return None
    try:
        payload = packet[4:].decode("utf-8")
        parsed  = json.loads(payload)
        print("[CMD] Received:", parsed)
        return parsed
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


def handle_bulk_sync(sender, radio, send_bulk_sync_fn):
    '''
    Respond to a "sync_request" from the Pi.
    Delegates to send_bulk_sync() in communication_garden.py which handles
    the file rename, chunked transfer, per-chunk acks, and cleanup.
    '''
    print("[SYNC] Bulk sync requested.")
    send_bulk_sync_fn(sender, radio)


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


def dispatch_command(command, sender, radio, rtc, get_timestamp_fn, send_latest_fn, send_bulk_sync_fn):
    '''
    Central dispatcher — call this from code.py whenever check_for_command()
    returns a packet. Routes to the appropriate handler based on packet type.
    Returns the new SENSE_INTERVAL if set_interval was received, else None.
    '''
    if command is None:
        return None

    t = command.get("t")

    if t == "poll":
        handle_poll(command, sender, get_timestamp_fn, send_latest_fn)

    elif t == "sync_request":
        handle_bulk_sync(sender, radio, send_bulk_sync_fn)

    elif t == "set_interval":
        return handle_set_interval(command, sender)

    elif t == "data_ack":
        print(f"[ACK] Pi confirmed batch (q={command.get('q')}).")

    else:
        print(f"[CMD] Unknown packet type: {t!r}")

    return None

