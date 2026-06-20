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
        year, month, day    = (int(x) for x in date_part.split("-"))
        hour, minute, second = (int(x) for x in time_part.split(":"))
        return time.struct_time((year, month, day, hour, minute, second, 0, -1, -1))
    except Exception as e:
        raise ValueError(f"Bad timestamp '{ts_str}': {e}")


def check_for_command(radio, timeout=0.5):
    '''
    Non-blocking radio listen. Returns a parsed dict or None.
    timeout controls how long to block waiting for a packet.
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
    If the poll includes a "ts" field, silently update the RTC first —
    this replaces the old separate sync/sync_ack ceremony for routine time updates.
    Then transmit the most recent in-memory sensor snapshot.
    '''
    # Silently update RTC if the Pi included a timestamp (routine clock sync)
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


def handle_sync(command, rtc, sender, get_timestamp_fn):
    '''
    Explicit RTC sync command (Pi sends current time, Pico confirms).
    Still supported for manual/cron-triggered syncs, but routine time updates
    now piggyback on poll packets via handle_poll().

    # TODO: once handle_poll's silent RTC update proves reliable in the field,
    # consider removing handle_sync and the sync_ack packet type entirely.
    '''
    ts_str = command.get("ts")
    if not ts_str:
        print("[SYNC] Missing 'ts' field, ignoring.")
        return
    try:
        rtc.datetime = _parse_iso_timestamp(ts_str)
        print("[SYNC] RTC updated to", ts_str)
    except ValueError as e:
        print("[SYNC] Failed to set RTC:", e)
        return
    time.sleep(1)
    sender.send({"t": "sync_ack", "ts": get_timestamp_fn()})


def handle_set_interval(command, sender):
    '''
    Update the sense interval. v=0 is not meaningful in the new architecture
    (the Pico is always listening) so it is treated as a no-op with a warning.

    # TODO: v=0 used to mean "indefinite sleep". Now that the Pico is always
    # in listen mode, decide whether to repurpose 0 or remove it from the protocol.
    '''
    v = command.get("v")
    if not isinstance(v, int) or v < 0:
        print("[INTERVAL] Invalid interval value:", v)
        return None
    if v == 0:
        print("[INTERVAL] Warning: interval=0 is not meaningful in listen-always mode. Ignoring.")
        return None
    print(f"[INTERVAL] Sense interval updated to {v}s")
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

    elif t == "sync":
        handle_sync(command, rtc, sender, get_timestamp_fn)

    elif t == "set_interval":
        return handle_set_interval(command, sender)

    else:
        print(f"[CMD] Unknown packet type: {t!r}")

    return None

