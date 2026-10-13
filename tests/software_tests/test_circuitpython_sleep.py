# tests/software_tests/test_circuitpython_sleep.py
"""CircuitPython nodes sleeping on the Pi's command (sync_garden.handle_sleep)."""
import json
from datetime import datetime

import pytest

import sync_garden
from communication_garden import PacketSender
from communication_indoor import FragmentReassembler, SleepScheduler
from sync_garden import _seconds, dispatch_command

NOW = "2026-10-13T19:00:05"


class Radio:
    def __init__(self):
        self.sent = []

    def send(self, data):
        assert len(data) <= 60
        self.sent.append(bytes(data))

    def packet(self):
        frags = FragmentReassembler()
        packet = None
        for payload in self.sent:
            packet = frags.feed(payload) if payload[:1] == b"~" else payload
        return json.loads(packet)


@pytest.fixture(autouse=True)
def no_wake():
    sync_garden.requested_wake = None
    yield
    sync_garden.requested_wake = None


def sleep(w, now=NOW):
    radio = Radio()
    command = {"t": "sleep", "n": 1, "w": w}
    dispatch_command(command, PacketSender(1, radio), None, None, lambda: now,
                     None, None, 1)
    return radio.packet()


@pytest.mark.parametrize("a, b", [
    ("2026-10-13T19:00:05", "2026-10-14T07:00:00"),
    ("2028-02-28T23:00:00", "2028-03-01T01:00:00"),   # across a leap day
    ("2026-12-31T22:00:00", "2027-01-01T06:30:00"),   # across new year
])
def test_seconds_between_times_match_datetime(a, b):
    expected = (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()
    assert _seconds(b) - _seconds(a) == expected


def test_accepts_a_wake_time_and_acks_it():
    ack = sleep("2026-10-14T07:00:00")
    assert ack == {"t": "sleep_ack", "q": 0, "n": 1, "ok": 1, "w": "2026-10-14T07:00:00"}
    assert sync_garden.requested_wake == "2026-10-14T07:00:00"


@pytest.mark.parametrize("w, now, why", [
    ("2026-10-14T07:00:00", "2000-01-01T00:01:00", "clock not set"),
    ("2026-10-13T18:00:00", NOW, "wake time already passed"),
    ("2026-10-15T07:00:00", NOW, "longer than SLEEP_MAX_S"),
    ("tomorrow", NOW, "bad wake time"),
    (None, NOW, "bad wake time"),
])
def test_refuses_a_bad_sleep(w, now, why):
    ack = sleep(w, now)
    assert ack["ok"] == 0 and ack["why"] == why
    assert sync_garden.requested_wake is None


def test_the_pi_records_the_ack():
    sleeper = SleepScheduler({1: ("19:00", "07:00")}, now_fn=lambda: datetime(2026, 10, 13, 19, 0, 5))
    sleeper.handle_ack(sleep("2026-10-14T07:00:00"))
    assert sleeper.asleep_until(1) == datetime(2026, 10, 14, 7, 0, 0)


def test_other_nodes_sleep_commands_are_ignored():
    radio = Radio()
    dispatch_command({"t": "sleep", "n": 2, "w": "2026-10-14T07:00:00"}, PacketSender(1, radio),
                     None, None, lambda: NOW, None, None, 1)
    assert radio.sent == [] and sync_garden.requested_wake is None
