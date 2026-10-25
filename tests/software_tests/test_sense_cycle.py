# tests/software_tests/test_sense_cycle.py
"""A sense cycle a sensor at a time (circuitpython/sense_cycle.py), so the
radio is checked between reads."""
from sense_cycle import SenseCycle
from sensor_guard import STRIKES, SensorGuard


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def sensor(name, clock, seconds=0.2, fail=False, calls=None):
    def read():
        clock.t += seconds
        if calls is not None:
            calls.append(name)
        if fail:
            raise OSError("I2C timeout")
        return {"t": name}
    return name, read


def test_one_sensor_per_step_then_the_whole_snapshot():
    clock, calls = Clock(), []
    cycle = SenseCycle([sensor(n, clock, calls=calls) for n in ("sht", "voc", "uv")],
                       SensorGuard(clock), clock=clock)
    cycle.start("2026-10-25T17:00:00")
    assert cycle.busy
    assert cycle.step() is None and calls == ["sht"]       # back to the radio after each read
    assert cycle.step() is None and calls == ["sht", "voc"]
    packets, ts = cycle.step()
    assert [p["t"] for p in packets] == ["sht", "voc", "uv"] and ts == "2026-10-25T17:00:00"
    assert not cycle.busy


def test_the_longest_a_command_waits_is_one_read():
    '''Five sensors at 0.3 s each: 1.5 s in one go (a ping's whole wait),
    0.3 s at most between radio checks one at a time.'''
    clock = Clock()
    cycle = SenseCycle([sensor(f"s{i}", clock, seconds=0.3) for i in range(5)], SensorGuard(clock), clock=clock)
    cycle.start("ts")
    gaps = []
    while cycle.busy:
        before = clock.t
        cycle.step()
        gaps.append(clock.t - before)
    assert max(gaps) <= 0.3 + 1e-9 and sum(gaps) >= 1.5 - 1e-9


def test_failures_count_towards_offline_and_offline_sensors_are_skipped_free():
    clock, calls, notes = Clock(), [], []
    guard = SensorGuard(clock)
    cycle = SenseCycle([sensor("sht", clock, fail=True, calls=calls), sensor("uv", clock, calls=calls)],
                       guard, note=notes.append, clock=clock)
    for _ in range(STRIKES):
        packets, _ = cycle.run_all("ts")
        assert [p["t"] for p in packets] == ["uv"]          # a failed read leaves nothing
    assert guard.offline() == ["sht"] and any("sht offline" in n for n in notes)
    calls.clear()
    cycle.start("ts")
    cycle.step()                                            # sht is skipped: uv is read straight away
    assert calls == ["uv"]


def test_the_watchdog_is_fed_after_every_read():
    clock, fed = Clock(), []
    cycle = SenseCycle([sensor("a", clock), sensor("b", clock)], SensorGuard(clock),
                       feed=lambda: fed.append(1), clock=clock)
    cycle.run_all("ts")
    assert len(fed) == 2


def test_no_sensors_is_an_empty_snapshot():
    cycle = SenseCycle([], SensorGuard(Clock()))
    cycle.start("ts")
    assert cycle.step() == ([], "ts")
