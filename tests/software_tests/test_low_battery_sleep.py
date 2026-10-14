# tests/software_tests/test_low_battery_sleep.py
"""A node with a low LiPo sleeps through the day, checking in before its night."""
import sqlite3
from datetime import datetime, timedelta

import pytest

import db
from communication_indoor import SleepScheduler
from nodes import NodeConfigError, check_node, low_battery_sleep
from test_sleep import Clock, at


class Battery:
    '''Readings as (time, percent); answers like db.latest_value.'''
    def __init__(self, *readings):
        self.readings = list(readings)
        self.asked = 0

    def __call__(self, node_id, since):
        self.asked += 1
        fresh = [(t, v) for t, v in self.readings if t.strftime("%Y-%m-%dT%H:%M:%S") >= since]
        return max(fresh)[1] if fresh else None


def scheduler(clock, battery, level=25, light=None, daylight=None):
    return SleepScheduler({2: ("19:00", "07:00")}, now_fn=clock, low_battery={2: level}, battery=battery,
                          light=light, daylight=daylight)


def test_a_low_day_is_slept_in_three_hour_stretches_until_the_evening_check_in(capsys):
    clock = Clock(at("07:03", day=28))
    battery = Battery((at("07:02", day=28), 18.4))
    s = scheduler(clock, battery)
    wakes = []
    while True:
        command = s.next_command()
        if command is None:
            break
        wakes.append(command["w"][11:16])
        s.handle_ack({"n": 2, "ok": 1, "w": command["w"]})
        assert s.sleep_reason(2) == "battery 18%, under 25%"
        # It wakes, the Pi polls it (a fresh reading), and the day is decided again.
        clock.t = datetime.strptime(command["w"], "%Y-%m-%dT%H:%M:%S") + timedelta(minutes=2)
        battery.readings.append((clock.t, 18.0))
    assert wakes == ["10:03", "13:05", "16:07", "18:45"]
    assert "it checks in then" in capsys.readouterr().out

    # At the evening check-in it stays awake; the window then puts it to
    # sleep for the night.
    assert clock.t == at("18:47", day=28) and not s.asleep(2)
    clock.t = at("19:00", day=28)
    assert s.next_command()["w"] == "2026-09-29T07:00:00"


def test_enough_battery_stays_awake():
    clock = Clock(at("07:03", day=28))
    s = scheduler(clock, Battery((at("07:02", day=28), 25.0)))
    assert s.next_command() is None


def test_no_fresh_reading_means_no_day_sleep():
    '''A reading from before 06:53 might be from yesterday's charge: wait for a new one.'''
    clock = Clock(at("07:03", day=28))
    battery = Battery((at("18:40", day=27), 10.0))
    s = scheduler(clock, battery)
    assert s.next_command() is None
    battery.readings.append((at("07:04", day=28), 12.0))
    clock.t = at("07:05", day=28)
    assert s.next_command()["w"] == "2026-09-28T10:05:00"


def test_low_later_in_the_day_sleeps_too_but_not_right_before_the_check_in():
    clock = Clock(at("16:00", day=28))
    s = scheduler(clock, Battery((at("15:58", day=28), 20.0)))
    assert s.next_command()["w"] == "2026-09-28T18:45:00"   # under 3 h to go: straight to the check-in
    s2 = scheduler(Clock(at("18:20", day=28)), Battery((at("18:20", day=28), 19.0)))
    assert s2.next_command() is None          # 25 minutes to the check-in: not worth it


# ── The weather and the node's own light sensor ──────────────────────────────

class Outlook:
    def __init__(self, dark, why="3 forecasts agree it's dark (a 90%, b 85%, c 95% cloud)"):
        self.dark, self.why, self.asked = dark, why, []

    def __call__(self, start, end):
        self.asked.append((start, end))
        return self.dark, self.why


def test_it_only_sleeps_when_the_forecasts_agree_its_a_dark_day(capsys):
    clock = Clock(at("07:03", day=28))
    outlook = Outlook(False, "not all forecasts say it's dark (a 90%, b 40% cloud)")
    s = scheduler(clock, Battery((at("07:02", day=28), 10.0)), daylight=outlook)
    assert s.next_command() is None
    assert outlook.asked == [(at("07:03", day=28), at("10:03", day=28))]   # the hours it would sleep
    out = capsys.readouterr().out
    assert "stays awake to charge: not all forecasts say it's dark" in out

    clock.t += timedelta(minutes=1)
    outlook.why = "not all forecasts say it's dark (a 91%, b 42% cloud)"
    assert s.next_command() is None
    assert capsys.readouterr().out == ""      # said once, not every minute

    outlook.dark = True
    clock.t += timedelta(minutes=1)
    command = s.next_command()
    assert command["w"] == "2026-09-28T10:05:00"
    s.handle_ack({"n": 2, "ok": 1, "w": command["w"]})
    assert s.sleep_reason(2) == "battery 10%, under 25%, dark day"


def test_its_own_light_sensor_outranks_the_forecasts():
    clock = Clock(at("10:05", day=28))
    sunny = Battery((at("10:04", day=28), 32000.0))
    s = scheduler(clock, Battery((at("10:04", day=28), 10.0)), light=sunny, daylight=Outlook(True))
    assert s.next_command() is None           # the forecasts say dark; the sensor says sun
    dim = Battery((at("10:04", day=28), 4000.0))
    s2 = scheduler(clock, Battery((at("10:04", day=28), 10.0)), light=dim, daylight=Outlook(True))
    assert s2.next_command() is not None      # overcast: sleep


def test_the_database_is_asked_once_a_minute_but_a_low_battery_holds_until_sent():
    clock = Clock(at("07:03", day=28))
    battery = Battery((at("07:02", day=28), 30.0))
    s = scheduler(clock, battery)
    for _ in range(50):                       # every pass of the radio loop
        assert s.next_command() is None
    assert battery.asked == 1

    battery.readings.append((at("07:04", day=28), 20.0))
    clock.t = at("07:04", day=28) + timedelta(seconds=30)
    assert s.next_command() is not None       # a poll went out instead of it...
    asked = battery.asked
    clock.t += timedelta(seconds=5)
    assert s.next_command() is not None       # ...so it's still due, without asking again
    assert battery.asked == asked


def test_a_refused_day_sleep_is_asked_again_later():
    clock = Clock(at("07:03", day=28))
    s = scheduler(clock, Battery((at("07:02", day=28), 10.0)))
    command = s.next_command()
    s.handle_ack({"n": 2, "ok": 0, "w": command["w"], "why": "clock not set"})
    assert s.next_command() is None


# ── nodes.json ───────────────────────────────────────────────────────────────

def node(**changes):
    n = {"name": "M0", "framework": "arduino", "board": "feather_m0", "sense_interval_s": 3,
         "log_interval_s": 300, "sleep_window": ["19:00", "07:00"],
         "pins": {"radio_cs": 9, "radio_irq": 6, "radio_rst": 11, "sd_cs": 10}}
    n.update(changes)
    return n


def test_which_nodes_sleep_through_a_low_battery_day():
    nodes = {
        2: node(),                                                 # LiPo gauge by default
        3: node(low_battery_sleep=40),
        4: node(low_battery_sleep=False),
        5: node(battery={"type": "pw0", "label": "CAR BATTERY"}),  # not a LiPo
        6: node(sleep_window=None),
    }
    del nodes[6]["sleep_window"]
    assert low_battery_sleep(nodes) == {2: 25, 3: 40}


@pytest.mark.parametrize("changes, message", [
    ({"low_battery_sleep": 0}, "from 1 to 99"),
    ({"low_battery_sleep": "25"}, "from 1 to 99"),
    ({"low_battery_sleep": 25, "battery": {"type": "pw0", "label": "CAR"}}, "MAX17048"),
])
def test_low_battery_sleep_mistakes(changes, message):
    with pytest.raises(NodeConfigError, match=message):
        check_node(2, node(**changes))


def test_low_battery_sleep_needs_a_window():
    n = node(low_battery_sleep=25)
    del n["sleep_window"]
    with pytest.raises(NodeConfigError, match="needs a sleep_window"):
        check_node(2, n)


# ── db.latest_value ──────────────────────────────────────────────────────────

def test_latest_battery_reading_from_the_database():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE readings (ts TEXT, node_id INTEGER, sensor_type TEXT, key TEXT, value REAL)")
    rows = [("2026-09-28T07:02:00", 2, "batt", "soc", 18.0),
            ("2026-09-28T07:01:00", 2, "batt", "soc", 19.0),   # synced late: newer row, older reading
            ("2026-09-28T07:02:00", 2, "batt", "v", 3.6),
            ("2026-09-28T07:02:00", 1, "batt", "soc", 90.0)]
    conn.executemany("INSERT INTO readings VALUES (?, ?, ?, ?, ?)", rows)
    assert db.latest_value(conn, 2, "batt", "soc", "2026-09-28T06:55:00") == 18.0
    assert db.latest_value(conn, 2, "batt", "soc", "2026-09-28T07:05:00") is None
