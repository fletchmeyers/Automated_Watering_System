'''
Python 3 running on Raspberry Pi 3B

The Pi's own power, from its Witty Pi 4 L3V7 UPS HAT: whether it's running
on the USB supply or on the Witty Pi's battery, the voltages and current,
whether the battery's charging, and the Witty Pi's temperature.

garden-api reads it every READ_EVERY seconds (keep_watching, a thread in
flask_api.py) and:
  - keeps the latest in node_info.json under node 0 ("power"), for the
    dashboard's Pi chip;
  - stores a reading every STORE_EVERY seconds, and straight away when the
    power source changes, as node 0's "pipwr" readings (the analysis panel
    plots them);
  - notes a power cut, and the power coming back, in its log and in
    node_events.txt, with a rough estimate of the battery time left once
    it's been on battery a while.

How the Witty Pi says it, as UUGear's own scripts (~/wittypi/utilities.sh,
wittyPi.sh) read it, at I2C address 0x08 on bus 1:
  registers 3/4  output voltage (whole volts, then hundredths)
  registers 5/6  output current (whole amps, then hundredths)
  register 7     power mode: 0 on the USB supply, otherwise on the battery
  registers 1/2  input voltage, which is the battery's while on battery
  register 19    low-voltage shutdown, in tenths of a volt (31 = 3.1 V)
  register 50    LM75B temperature (a 16-bit word)
  GPIO 5 / 6     the charger's CHRG / STDBY: low/high charging,
                 high/high discharging, high/low charged
Uses i2cget (i2c-tools) and pinctrl (Raspberry Pi OS), so no Python
packages beyond the standard library.

Written by Fletcher Meyers
October 2026
'''

import subprocess
import time
from datetime import datetime

PI_NODE      = 0      # the Pi's readings, beside the nodes' in sensors.db
I2C_BUS      = 1
I2C_ADDRESS  = 0x08
READ_EVERY   = 30     # seconds
STORE_EVERY  = 300    # seconds between stored readings while nothing changes
ESTIMATE_AFTER = 600  # seconds on battery before guessing the time left
CHRG_PIN, STDBY_PIN = 5, 6

ISO_FORMAT = "%Y-%m-%dT%H:%M:%S"


def _i2cget(register, word=False, run=subprocess.run):
    args = ["i2cget", "-y", str(I2C_BUS), hex(I2C_ADDRESS), str(register)] + (["w"] if word else [])
    out = run(args, capture_output=True, text=True, timeout=5, check=True).stdout.strip()
    return int(out, 16)


def _volts(whole, hundredths):
    return round(whole + hundredths / 100, 2)


def temperature_c(word):
    '''The LM75B's 16-bit reading (as i2cget returns it, bytes swapped) in °C.'''
    raw = (((word & 0xFF) << 8) | ((word & 0xFF00) >> 8)) >> 5
    if raw >= 0x400:
        raw -= 0x800
    return raw * 0.125


def _gpio(pin, run=subprocess.run):
    '''1 or 0 for a GPIO input, or None if it can't be read.'''
    try:
        out = run(["pinctrl", "get", str(pin)], capture_output=True, text=True, timeout=5).stdout
        if "| hi" in out:
            return 1
        if "| lo" in out:
            return 0
    except (OSError, subprocess.SubprocessError):
        pass
    try:   # wiringPi's gpio, which UUGear's installer may have added
        out = run(["gpio", "-g", "read", str(pin)], capture_output=True, text=True, timeout=5).stdout.strip()
        return int(out) if out in ("0", "1") else None
    except (OSError, subprocess.SubprocessError):
        return None


def charge_state(chrg, stdby):
    if chrg == 0 and stdby == 1:
        return "charging"
    if chrg == 1 and stdby == 1:
        return "discharging"
    if chrg == 1 and stdby == 0:
        return "charged"
    return None


def read(run=subprocess.run):
    '''The Witty Pi's state now, as a dict.'''
    get = lambda register, word=False: _i2cget(register, word, run)
    on_battery = get(7) != 0
    reading = {
        "vout": _volts(get(3), get(4)),
        "iout": _volts(get(5), get(6)),
        "on_battery": on_battery,
        "low_voltage": get(19) / 10,
        "tmp": temperature_c(get(50, word=True)),
        "charge": charge_state(_gpio(CHRG_PIN, run), _gpio(STDBY_PIN, run)),
    }
    if on_battery:
        reading["vbat"] = _volts(get(1), get(2))
    return reading


class PowerWatch:
    '''What garden-api does with each reading (see the top of the file).'''
    def __init__(self, store=None, status=None, note=print, clock=time.monotonic, now=datetime.now):
        self._store = store       # a packet into sensors.db (db.insert_packet)
        self._status = status     # update_node_info
        self._note = note         # an event line (log and node_events.txt)
        self._clock = clock
        self._now = now
        self._stored_at = None
        self._on_battery = None
        self._battery_since = None    # (monotonic, wall-clock ISO, volts) when the power went
        self._failed = False

    def failed(self, error):
        '''A read didn't work: said once, until reads work again.'''
        if not self._failed:
            self._failed = True
            print(f"[POWER] Can't read the Witty Pi: {error}")

    def update(self, reading):
        self._failed = False
        now, stamp = self._clock(), self._now().strftime(ISO_FORMAT)
        changed = self._on_battery is not None and reading["on_battery"] != self._on_battery
        if reading["on_battery"] and (self._battery_since is None or changed):
            self._battery_since = (now, stamp, reading.get("vbat"))
        if changed:
            if reading["on_battery"]:
                self._note(f"power cut: the Pi is running on its Witty Pi battery "
                           f"({reading.get('vbat', '?')} V; it shuts down cleanly at {reading['low_voltage']} V)")
            else:
                minutes = round((now - self._battery_since[0]) / 60) if self._battery_since else "?"
                self._note(f"power back: the Pi is on its USB supply again, after {minutes} min on battery")
        if not reading["on_battery"]:
            self._battery_since = None
        self._on_battery = reading["on_battery"]

        status = {k: reading[k] for k in ("vout", "iout", "on_battery", "low_voltage", "tmp", "charge")}
        status["at"] = stamp
        if reading["on_battery"]:
            status["vbat"] = reading.get("vbat")
            status["since"] = self._battery_since[1]
            status["minutes_left"] = self.minutes_left(reading, now)
        if self._status is not None:
            try:
                self._status(PI_NODE, power=status)
            except OSError as e:
                print(f"[POWER] Couldn't record the Pi's power: {e}")

        if self._store is not None and (changed or self._stored_at is None or now - self._stored_at >= STORE_EVERY):
            self._stored_at = now
            packet = {"t": "pipwr", "n": PI_NODE, "ts": stamp, "vout": reading["vout"], "iout": reading["iout"],
                      "on_battery": int(reading["on_battery"]), "tmp": round(reading["tmp"], 2),
                      "charging": int(reading["charge"] == "charging")}
            if reading.get("vbat") is not None:
                packet["vbat"] = reading["vbat"]
            try:
                self._store(packet)
            except Exception as e:
                print(f"[POWER] Couldn't store the Pi's power reading: {e}")
        return status

    def minutes_left(self, reading, now):
        '''A rough guess at the battery time left: how fast the voltage has
        fallen since the power went, carried on down to the shutdown
        voltage. None until it's been on battery ESTIMATE_AFTER seconds and
        the voltage has visibly fallen. (A LiPo's voltage falls faster near
        empty, so this errs long.)'''
        if not self._battery_since:
            return None
        started, _, v0 = self._battery_since
        v = reading.get("vbat")
        elapsed = now - started
        if v is None or v0 is None or elapsed < ESTIMATE_AFTER or v0 - v < 0.02:
            return None
        rate = (v0 - v) / elapsed            # volts per second
        return max(0, round((v - reading["low_voltage"]) / rate / 60))


def keep_watching(store=None, status=None, note=print):
    '''garden-api's thread: read the Witty Pi every READ_EVERY seconds.'''
    watch = PowerWatch(store=store, status=status, note=note)
    while True:
        try:
            watch.update(read())
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            watch.failed(e)
        time.sleep(READ_EVERY)
