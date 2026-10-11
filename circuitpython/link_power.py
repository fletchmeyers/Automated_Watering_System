'''
CircuitPython

Which links a node with both the radio and Wi-Fi keeps powered: the
CircuitPython twin of arduino/src/link_power.cpp.

Normally both are on, and the Pi uses whichever it prefers. With battery
saver (nodes.json "battery_saver": true) only the preferred link stays on:
  - Radio first: Wi-Fi stays off. With "wifi_check_hours" it comes on for
    CHECK_MINUTES every that many hours, and stays on while the Pi uses it
    (which means the radio is down).
  - Wi-Fi first: the radio comes on once nothing has come over Wi-Fi for
    SILENCE_MINUTES (listening is cheap), and goes off once the Pi is back
    on Wi-Fi.
Only real commands count as hearing the Pi; its "still there?" pings don't.
Polls carry the Pi's preferred link ("pf"), so a switch on the Pi needs no
code update.

Written by Fletcher Meyers
October 2026
'''

import time

SILENCE_MINUTES = 5   # primary quiet this long: the (cheap) radio backup comes on
KEEP_MINUTES    = 5   # a backup the Pi is using stays on this long after
CHECK_MINUTES   = 5   # how long a Wi-Fi check lasts


class LinkPower:
    def __init__(self, links, battery_saver=False, wifi_check_hours=0, clock=time.monotonic):
        self.preferred = links[0]
        self.saver = battery_saver
        self.check_hours = wifi_check_hours or 0
        self._clock = clock
        self._started = clock()
        self.reset()

    def heard(self, link, command):
        '''A command came in over link ("radio" or "wifi").'''
        if command.get("pf") in ("radio", "wifi"):
            self.preferred = command["pf"]
        if command.get("t") == "ping":
            return   # pings don't keep a link on
        self._heard_at[link] = self._clock()

    def keep(self, link):
        '''Keep link on for a while, as if the Pi were using it.'''
        self._heard_at[link] = self._clock()

    def reset(self):
        '''At boot or after a sleep: count quiet time from now.'''
        now = self._clock()
        self._heard_at = {"radio": None, "wifi": None}
        self._quiet_since = now

    def plan(self):
        '''(radio on, wifi on) right now.'''
        if not self.saver:
            return True, True
        now = self._clock()
        backup = "wifi" if self.preferred == "radio" else "radio"
        heard = self._heard_at[backup]
        backup_on = heard is not None and now - heard < KEEP_MINUTES * 60
        if backup == "radio":
            last = self._heard_at["wifi"] or self._quiet_since
            backup_on = backup_on or now - last >= SILENCE_MINUTES * 60
        elif self.check_hours:
            # A few minutes of Wi-Fi every check_hours (the first right after boot).
            backup_on = backup_on or (now - self._started) % (self.check_hours * 3600) < CHECK_MINUTES * 60
        return (self.preferred == "radio" or backup_on), (self.preferred == "wifi" or backup_on)
