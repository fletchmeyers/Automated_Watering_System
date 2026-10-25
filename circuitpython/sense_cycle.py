'''
CircuitPython

One sense cycle (every sensor read once), a sensor at a time: code.py's
loop reads the next sensor on each pass and checks the radio (and USB and
Wi-Fi) in between, so a command arriving mid-cycle waits for one sensor read
at most, rather than the whole cycle. (Some reads take a while: the light
sensor waits for a fresh measurement, and a corroded connection drags.)
Until a cycle finishes, polls are answered with the last complete one.

Each read goes through sensor_guard.py: an offline sensor is skipped, and a
slow or failing one counts towards taking it offline.

Written by Fletcher Meyers
October 2026
'''

import time


class SenseCycle:
    def __init__(self, sensors, guard, note=print, feed=lambda: None, clock=time.monotonic):
        self.sensors = sensors   # [(name, read function)], in order (the SHT40 before the VOC sensor)
        self.guard = guard
        self._note = note        # a guard note (offline, back): code.py puts it on the SD card
        self._feed = feed        # the watchdog
        self._clock = clock
        self._next = None        # where the cycle under way has got to, or None
        self._packets = []
        self._timestamp = None

    @property
    def busy(self):
        return self._next is not None

    def start(self, timestamp):
        '''Begin a cycle; its readings are stamped with timestamp.'''
        self._next = 0
        self._packets = []
        self._timestamp = timestamp

    def step(self):
        '''Read the next sensor that's due. Returns (packets, timestamp) once
        the cycle is done, else None.'''
        while self._next < len(self.sensors):
            name, read = self.sensors[self._next]
            self._next += 1
            if not self.guard.due(name):
                continue   # offline: costs nothing, so on to the next
            started = self._clock()
            ok = True
            try:
                self._packets.append(read())
            except Exception as e:
                ok = False
                print(f"[ERROR] Sensor '{name}' failed: {e}")
            note = self.guard.result(name, ok, self._clock() - started)
            if note:
                self._note(note)
            self._feed()
            break   # one read per step: back to the radio
        if self._next < len(self.sensors):
            return None
        self._next = None
        return self._packets, self._timestamp

    def run_all(self, timestamp):
        '''A whole cycle now (asleep, with the radio off anyway).'''
        self.start(timestamp)
        while True:
            done = self.step()
            if done is not None:
                return done
