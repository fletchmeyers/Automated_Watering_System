'''
CircuitPython

A note on the SD card for each thing worth knowing about later: every start
(why it restarted, the CircuitPython version, whether USB was connected), a
crash (the end of its error message), being stopped at the console, a sensor
going offline or coming back, the first failed radio send since starting,
a switch of radio format. So when a node goes quiet in the garden, the next
sync (or USB pull) says what happened.

The radio gets notes of its own (RadioWatch, as node_events.radio): when a
send fails (the radio library says so by returning False, or an error) and
when sends work again, with how many failed; and when nothing at all has
been heard over the radio for QUIET_MINUTES (the Pi polls every minute), and
when it's heard again. Between them they say which way a radio link that
went quiet was failing.

Each note is a line in the reading log like any other, {"t": "ev", "m":
"<what>", "ts": ...}, so it reaches the Pi by the same sync, over the radio
or USB; the Pi keeps it out of the readings and logs it instead (the
node_events.txt file next to sensors.db, and its service's log).

Written by Fletcher Meyers
October 2026
'''

import time

MAX_CHARS     = 200   # a note's text; a crash keeps the end of its error, where the cause is
QUIET_MINUTES = 10    # nothing heard over the radio for this long: noted

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


class RadioWatch:
    '''Notes when the radio stops (and starts) sending or hearing.'''
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._failures = 0        # sends failed in a row
        self.reset()

    def reset(self):
        '''Count quiet time from now (at start, and after a sleep), without a note.'''
        self._last_heard = self._clock()
        self._quiet = False

    def sent(self, ok, size, error=None):
        '''How a radio send of size bytes went.'''
        if not ok:
            self._failures += 1
            if self._failures == 1:
                record(f"radio send failed ({size} bytes): {error or 'the radio said it did not complete'}")
        elif self._failures:
            record(f"radio sends working again, after {self._failures} failed")
            self._failures = 0

    def heard(self):
        '''Something arrived over the radio (from the Pi or another node).'''
        now = self._clock()
        if self._quiet:
            minutes = round((now - self._last_heard) / 60)
            record(f"hearing the radio again, after {minutes} min of silence")
            self._quiet = False
        self._last_heard = now

    def check(self, listening):
        '''Each pass of the loop: listening is whether the radio is on.'''
        if not listening:
            self._last_heard = self._clock()   # switched off (battery saver): not silence
            return
        if not self._quiet and self._clock() - self._last_heard >= QUIET_MINUTES * 60:
            self._quiet = True
            record(f"nothing heard over the radio for {QUIET_MINUTES} min (the Pi polls every minute)")


radio = RadioWatch()
