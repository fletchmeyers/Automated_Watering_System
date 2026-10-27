# tests/software_tests/test_pi_power.py
"""The Pi's own power from its Witty Pi 4 L3V7 (raspberrypi/pi_power.py)."""
import subprocess
import types
from datetime import datetime

import pytest

import pi_power
from pi_power import PowerWatch, charge_state, temperature_c


class Witty:
    '''Answers i2cget and pinctrl like the Witty Pi does: registers as UUGear's scripts read them.'''
    def __init__(self, on_battery=False, vbat=(3, 92), chrg=0, stdby=1):
        self.registers = {1: vbat[0], 2: vbat[1], 3: 5, 4: 26, 5: 0, 6: 17,
                          7: 1 if on_battery else 0, 19: 31}
        self.word = 0x2024          # LM75B: 36.125 °C, bytes swapped as i2cget returns them
        self.pins = {5: chrg, 6: stdby}

    def __call__(self, args, **kw):
        if args[0] == "i2cget":
            value = self.word if args[-1] == "w" else self.registers[int(args[4])]
            return types.SimpleNamespace(stdout=hex(value) + "\n")
        if args[0] == "pinctrl":
            level = "hi" if self.pins[int(args[2])] else "lo"
            return types.SimpleNamespace(stdout=f"{args[2]}: ip pu | {level} // GPIO{args[2]} = input\n")
        raise FileNotFoundError(args[0])


def test_reading_it_on_the_usb_supply():
    reading = pi_power.read(Witty())
    assert reading == {"vout": 5.26, "iout": 0.17, "on_battery": False, "low_voltage": 3.1,
                       "tmp": 36.125, "charge": "charging"}            # what wittyPi.sh's menu showed


def test_on_battery_the_input_is_the_battery():
    reading = pi_power.read(Witty(on_battery=True, chrg=1, stdby=1))
    assert reading["on_battery"] and reading["vbat"] == 3.92 and reading["charge"] == "discharging"


def test_the_temperature_sensor():
    assert temperature_c(0x2024) == 36.125
    assert temperature_c(0xE0FF) == pytest.approx(-0.125)    # below freezing: negative


def test_the_charger_pins():
    assert [charge_state(*p) for p in ((0, 1), (1, 1), (1, 0), (0, 0), (None, 1))] == \
        ["charging", "discharging", "charged", None, None]


def test_gpio_falls_back_to_wiringpi_gpio():
    def run(args, **kw):
        if args[0] == "pinctrl":
            raise FileNotFoundError("pinctrl")
        return types.SimpleNamespace(stdout="1\n")
    assert pi_power._gpio(5, run) == 1


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def watch():
    clock, stored, statuses, notes = Clock(), [], [], []
    w = PowerWatch(store=stored.append, status=lambda n, **kw: statuses.append((n, kw["power"])),
                   note=notes.append, clock=clock, now=lambda: datetime(2026, 10, 27, 14, 0))
    return w, clock, stored, statuses, notes


def mains(**changes):
    return dict({"vout": 5.26, "iout": 0.17, "on_battery": False, "low_voltage": 3.1, "tmp": 36.1,
                 "charge": "charging"}, **changes)


def battery(vbat, **changes):
    return dict(mains(on_battery=True, vbat=vbat, vout=5.1, iout=0.4, charge="discharging"), **changes)


def test_readings_are_stored_every_five_minutes_and_status_every_time(watch):
    w, clock, stored, statuses, notes = watch
    for _ in range(11):                          # 5 minutes of 30-second reads
        w.update(mains())
        clock.t += 30
    assert len(stored) == 2 and len(statuses) == 11
    assert stored[0] == {"t": "pipwr", "n": 0, "ts": "2026-10-27T14:00:00", "vout": 5.26, "iout": 0.17,
                         "on_battery": 0, "tmp": 36.1, "charging": 1}
    assert statuses[-1][0] == 0 and statuses[-1][1]["at"] == "2026-10-27T14:00:00"
    assert notes == []                           # nothing happened


def test_a_power_cut_is_noted_stored_at_once_and_estimated(watch):
    w, clock, stored, statuses, notes = watch
    w.update(mains())
    clock.t += 30
    w.update(battery(4.10))
    assert notes[-1].startswith("power cut: the Pi is running on its Witty Pi battery (4.1 V")
    assert stored[-1]["on_battery"] == 1 and stored[-1]["vbat"] == 4.10   # stored straight away
    assert statuses[-1][1]["minutes_left"] is None                        # too soon to guess
    clock.t += 20 * 60
    w.update(battery(3.90))                     # 0.2 V in 20 min: 0.8 V to go -> ~80 min
    assert statuses[-1][1]["minutes_left"] == 80
    assert statuses[-1][1]["since"] == "2026-10-27T14:00:00"
    clock.t += 10 * 60
    w.update(mains())
    assert notes[-1] == "power back: the Pi is on its USB supply again, after 30 min on battery"
    assert "minutes_left" not in statuses[-1][1]


def test_a_failed_read_is_said_once(capsys):
    w = PowerWatch()
    w.failed(subprocess.CalledProcessError(2, "i2cget"))
    w.failed(subprocess.CalledProcessError(2, "i2cget"))
    assert capsys.readouterr().out.count("Can't read the Witty Pi") == 1
