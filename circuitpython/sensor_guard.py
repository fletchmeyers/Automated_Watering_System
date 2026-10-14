'''
CircuitPython

A safety net for sensors whose connections are going bad (corroded pins
on the I2C bus, say): a sensor that fails, or takes longer than SLOW_S to
read, STRIKES times in a row is taken offline. code.py stops reading it,
so it can't hold up the sensors that still work (or the radio), and
tries it again after RETRY_MINUTES (10, then 30, then every hour). One
good read brings it back. Restarting the node tries everything again too.

The offline sensors go to the Pi with every poll ({"t": "off", "s":
"sht,s1"}; see send_latest() in communication_garden.py), and the
dashboard marks their cards offline.

Written by Fletcher Meyers
October 2026
'''

import time

SLOW_S        = 1.0            # a read taking longer than this counts as a bad one
STRIKES       = 3              # bad reads in a row before a sensor is taken offline
RETRY_MINUTES = (10, 30, 60)   # then tried again after these, the last repeating


class SensorGuard:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._strikes = {}   # sensor -> bad reads in a row
        self._off = {}       # sensor -> (when to try it again, tries so far)

    def due(self, name):
        '''True if name should be read this time round.'''
        off = self._off.get(name)
        return off is None or self._clock() >= off[0]

    def result(self, name, ok, seconds):
        '''How a read of name went: ok (no error) and how long it took.
        Returns a line for the console when the sensor goes offline, stays
        offline after a retry, or comes back; else None.'''
        why = "failed" if not ok else f"took {seconds:.1f} s"
        bad = not ok or seconds > SLOW_S
        now = self._clock()
        if name in self._off:
            if not bad:
                del self._off[name]
                self._strikes[name] = 0
                return f"[SENSOR] {name} is answering again; reading it as normal."
            tries = self._off[name][1] + 1
            wait = RETRY_MINUTES[min(tries, len(RETRY_MINUTES) - 1)]
            self._off[name] = (now + wait * 60, tries)
            return f"[SENSOR] {name} still isn't answering properly ({why}); trying again in {wait} min."
        if not bad:
            self._strikes[name] = 0
            return None
        strikes = self._strikes.get(name, 0) + 1
        self._strikes[name] = strikes
        if strikes < STRIKES:
            return None
        self._off[name] = (now + RETRY_MINUTES[0] * 60, 0)
        return (f"[SENSOR] {name} offline: {STRIKES} bad reads in a row (the last {why}). Not reading "
                f"it for {RETRY_MINUTES[0]} min, so the other sensors keep going; check its connector.")

    def offline(self):
        '''The sensors offline right now, in order.'''
        return sorted(self._off)
