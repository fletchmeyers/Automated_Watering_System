'''
CircuitPython 10.0.3 running on Pico 2W RP2350

A "sync" packet from the Pi sets the RTC and triggers an immediate sensor read.
Pico confirms with a sync_ack packet and resumes normal loop.

Written by Fletcher Meyers
March 2026
'''

import json
import time



def _parse_iso_timestamp(ts_str):
    '''
    Parse an ISO-8601 string "YYYY-MM-DDTHH:MM:SS" into a time.struct_time.
    CircuitPython's time module does not have strptime, so we gotta do it manually.
    Returns a time.struct_time or raises ValueError on bad input.
    '''
    try:
        date_part, time_part = ts_str.split("T")
        year, month, day = (int(x) for x in date_part.split("-"))
        hour, minute, second = (int(x) for x in time_part.split(":"))
        # struct_time fields: (tm_year, tm_mon, tm_mday, tm_hour, tm_min,
        #                      tm_sec, tm_wday, tm_yday, tm_isdst)
        # wday/yday/isdst are not used by PCF8523 but must be present.
        return time.struct_time((year, month, day, hour, minute, second, 0, -1, -1))
    except Exception as e:
        raise ValueError(f"Bad timestamp '{ts_str}': {e}")



def check_for_command(radio, timeout=0.5):
    '''
    Listen on the radio for up to `timeout` seconds.
    Returns a parsed dict if a valid JSON command packet arrives, else None.
    Call this at the end of each main loop iteration.
    '''
    packet = radio.receive(timeout=timeout, with_header=True)
    if packet is None:
        return None
    try:
        payload = packet[4:].decode("utf-8")
        return json.loads(payload)
    except Exception as e:
        print("[SYNC] Could not parse incoming packet:", e)
        return None

def handle_sync(command, rtc, sender, get_timestamp_fn):
    ts_str = command.get("ts")
    if not ts_str:
        print("[SYNC] Sync command missing 'ts' field, ignoring.")
        return
    try:
        rtc.datetime = _parse_iso_timestamp(ts_str)
        print("[SYNC] RTC updated to", ts_str)
    except ValueError as e:
        print("[SYNC] Failed to set RTC:", e)
        return
    sender.send({"t": "sync_ack", "ts": get_timestamp_fn()})

def interruptible_sleep(radio, duration, chunk=0.5):
    '''
    Sleep for `duration` seconds, but check for incoming radio commands
    every `chunk` seconds. Returns any command packet received, or None.
    '''
    elapsed = 0
    while elapsed < duration:
        command = check_for_command(radio, timeout=chunk)
        if command is not None:
            return command
        elapsed += chunk
    return None