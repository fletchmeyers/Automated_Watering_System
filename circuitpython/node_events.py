'''
CircuitPython

A note on the SD card for each thing worth knowing about later: every start
(why it restarted, the CircuitPython version, whether USB was connected), a
crash (the end of its error message), being stopped at the console, a sensor
going offline or coming back, the first failed radio send since starting,
a switch of radio format. So when a node goes quiet in the garden, the next
sync (or USB pull) says what happened.

Each note is a line in the reading log like any other, {"t": "ev", "m":
"<what>", "ts": ...}, so it reaches the Pi by the same sync, over the radio
or USB; the Pi keeps it out of the readings and logs it instead (the
node_events.txt file next to sensors.db, and its service's log).

Written by Fletcher Meyers
October 2026
'''

MAX_CHARS = 200   # a note's text; a crash keeps the end of its error, where the cause is

_timestamp = None


def setup(timestamp_fn):
    '''code.py's get_timestamp, for stamping notes.'''
    global _timestamp
    _timestamp = timestamp_fn


def record(message):
    '''Print message and add it to the SD log as an event note.'''
    message = str(message)
    if len(message) > MAX_CHARS:
        message = "..." + message[-(MAX_CHARS - 3):]
    print(f"[EVENT] {message}")
    try:
        import communication_garden
        ts = _timestamp() if _timestamp else "unknown"
        communication_garden.append_to_sd([{"t": "ev", "m": message}], ts)
    except Exception as e:
        print(f"[EVENT] Couldn't write it to the SD card: {e}")


def _name(value):
    '''"microcontroller.ResetReason.POWER_ON" -> "POWER_ON".'''
    return str(value).split(".")[-1]


def boot():
    '''The note at every start.'''
    parts = ["started"]
    try:
        import microcontroller
        parts.append(f"reset reason {_name(microcontroller.cpu.reset_reason)}")
    except Exception:
        pass
    try:
        import os
        parts.append(f"CircuitPython {os.uname().release}")
    except Exception:
        pass
    try:
        import supervisor
        parts.append("USB connected" if supervisor.runtime.usb_connected else "no USB")
    except Exception:
        pass
    record(", ".join(parts))


def crashed(error):
    '''The note for a crash: its error, as much as fits (the end, where the cause is).'''
    try:
        import traceback
        text = "".join(traceback.format_exception(error))
    except Exception:
        text = f"{type(error).__name__}: {error}"
    text = " | ".join(line.strip() for line in text.strip().splitlines())
    room = MAX_CHARS - len("crashed: ")
    if len(text) > room:
        text = "..." + text[-(room - 3):]
    record("crashed: " + text)
