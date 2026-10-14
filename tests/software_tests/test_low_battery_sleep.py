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


def scheduler(clock, battery, level=25):
    return SleepScheduler({2: ("19:00", "07:00")}, now_fn=clock, low_battery={2: level}, battery=battery)


def test_low_in_the_morning_sleeps_until_the_evening_check_in(capsys):
    clock = Clock(at("07:03", day=28))
    s = scheduler(clock, Battery((at("07:02", day=28), 18.4)))
    command = s.next_command()
    assert command == {"t": "sleep", "n": 2, "w": "2026-09-28T18:45:00"}   # 15 min before 19:00
    s.handle_ack({"n": 2, "ok": 1, "w": command["w"]})
    assert s.sleep_reason(2) == "battery 18%, under 25%"
    assert "checks in then, before its night's sleep" in capsys.readouterr().out

    # At the check-in it's awake and answering, and isn't sent back to sleep
    # for the day; the window then puts it to sleep for the night.
    clock.t = at("18:47", day=28)
    assert not s.asleep(2) and s.sleep_reason(2) is None
    assert s.next_command() is None
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
    assert s.next_command()["w"] == "2026-09-28T18:45:00"


def test_low_later_in_the_day_sleeps_too_but_not_right_before_the_check_in():
    clock = Clock(at("14:00", day=28))
    s = scheduler(clock, Battery((at("13:58", day=28), 20.0), (at("18:20", day=28), 19.0)))
    assert s.next_command()["w"] == "2026-09-28T18:45:00"
    s2 = scheduler(Clock(at("18:20", day=28)), Battery((at("18:20", day=28), 19.0)))
    assert s2.next_command() is None          # 25 minutes to the check-in: not worth it


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
