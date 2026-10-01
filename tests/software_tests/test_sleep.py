# tests/software_tests/test_sleep.py
from datetime import datetime, timedelta

from communication_indoor import SleepScheduler


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def at(hhmm, day=27):
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime(2026, 9, day, h, m)


def test_overnight_window_wake_times():
    s = SleepScheduler({2: ("19:00", "07:00")})
    assert s.window_wake(2, at("18:59")) is None
    assert s.window_wake(2, at("19:00")) == at("07:00", day=28)
    assert s.window_wake(2, at("23:30")) == at("07:00", day=28)
    assert s.window_wake(2, at("03:00", day=28)) == at("07:00", day=28)
    assert s.window_wake(2, at("07:00", day=28)) is None
    assert s.window_wake(1, at("23:30")) is None            # node 1 has no window


def test_same_day_window():
    s = SleepScheduler({2: ("01:00", "05:00")})
    assert s.window_wake(2, at("00:59")) is None
    assert s.window_wake(2, at("02:00")) == at("05:00")
    assert s.window_wake(2, at("05:00")) is None


def test_sleep_command_only_inside_window_and_only_to_available_nodes():
    clock = Clock(at("18:00"))
    s = SleepScheduler({2: ("19:00", "07:00")}, now_fn=clock)
    assert s.next_command() is None

    clock.t = at("19:05")
    assert s.next_command(available=lambda n: False) is None
    assert s.next_command() == {"t": "sleep", "n": 2, "w": "2026-09-28T07:00:00"}


def test_ack_puts_node_to_sleep_until_wake_plus_grace():
    clock = Clock(at("19:05"))
    s = SleepScheduler({2: ("19:00", "07:00")}, wake_grace=90, now_fn=clock)
    s.handle_ack({"t": "sleep_ack", "n": 2, "ok": 1, "w": "2026-09-28T07:00:00"})

    assert s.asleep(2)
    assert s.asleep_until(2) == at("07:00", day=28)
    assert s.next_command() is None                          # no second sleep command

    clock.t = at("07:01", day=28)
    assert s.asleep(2)                                       # still inside the grace period
    clock.t = at("07:02", day=28)
    assert not s.asleep(2)
    assert s.next_command() is None                          # window over — stays awake


def test_refusal_waits_before_asking_again():
    clock = Clock(at("19:05"))
    s = SleepScheduler({2: ("19:00", "07:00")}, retry_after=600, now_fn=clock)
    s.handle_ack({"t": "sleep_ack", "n": 2, "ok": 0, "w": "2026-09-28T07:00:00", "why": "clock not set"})
    assert not s.asleep(2)
    assert s.next_command() is None
    clock.t += timedelta(minutes=10)
    assert s.next_command()["n"] == 2


def test_timed_out_sleep_command_counts_as_asleep():
    clock = Clock(at("19:05"))
    s = SleepScheduler({2: ("19:00", "07:00")}, now_fn=clock)
    command = s.next_command()
    s.timed_out(command)
    assert s.asleep(2)
    assert s.asleep_until(2) == at("07:00", day=28)


def test_manual_request_outside_window():
    clock = Clock(at("12:00"))
    s = SleepScheduler({2: ("19:00", "07:00")}, now_fn=clock)
    s.request_now(2, 5)
    assert s.next_command() == {"t": "sleep", "n": 2, "w": "2026-09-27T12:05:00"}
    s.handle_ack({"t": "sleep_ack", "n": 2, "ok": 1, "w": "2026-09-27T12:05:00"})
    assert s.asleep(2)

    clock.t = at("12:10")
    assert not s.asleep(2)
    assert s.next_command() is None                          # one-off, not repeated


def test_expired_manual_request_is_dropped():
    clock = Clock(at("12:00"))
    s = SleepScheduler({}, now_fn=clock)
    s.request_now(3, 1)
    clock.t = at("12:02")
    assert s.next_command() is None
