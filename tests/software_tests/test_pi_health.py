# tests/software_tests/test_pi_health.py
"""The Pi's own health for the dashboard's Pi row (raspberrypi/pi_health.py)."""
import types

import pi_health
from pi_health import Cached, throttle_flags, warnings


def tools(throttled="0x0", synced="yes", states=None):
    '''Answers vcgencmd, timedatectl and systemctl like the Pi does.'''
    states = states or {}

    def run(args, **kw):
        if args[0] == "vcgencmd":
            out = f"throttled={throttled}"
        elif args[0] == "timedatectl":
            out = synced
        elif args[0] == "systemctl":
            out = states.get(args[2], "active")
        else:
            raise FileNotFoundError(args[0])
        return types.SimpleNamespace(stdout=out + "\n")
    return run


def fake_pi(tmp_path):
    (tmp_path / "proc").mkdir()
    (tmp_path / "proc/meminfo").write_text("MemTotal:  948304 kB\nMemFree:  100000 kB\nMemAvailable:  474152 kB\n")
    (tmp_path / "proc/uptime").write_text("93784.21 180000.00\n")
    (tmp_path / "sys/class/thermal/thermal_zone0").mkdir(parents=True)
    (tmp_path / "sys/class/thermal/thermal_zone0/temp").write_text("48312\n")
    (tmp_path / "sensors.db").write_bytes(b"x" * 2048)
    return dict(proc=tmp_path / "proc", sys_dir=tmp_path / "sys", disk=str(tmp_path), db_file=tmp_path / "sensors.db")


def test_reading_a_healthy_pi(tmp_path):
    health = pi_health.read(tools(), **fake_pi(tmp_path))
    assert health["cpu_temp"] == 48.3 and health["throttled"] == 0 and health["uptime_s"] == 93784
    assert health["memory"] == {"total": 948304 * 1024, "available": 474152 * 1024}
    assert health["db_bytes"] == 2048 and health["clock_synced"] is True
    assert health["services"] == {name: "active" for name in pi_health.SERVICES}
    assert health["disk"]["total"] > 0
    assert warnings(health) == []


def test_what_cant_be_read_is_left_out(tmp_path):
    def nothing(args, **kw):
        raise FileNotFoundError(args[0])
    health = pi_health.read(nothing, proc=tmp_path / "none", sys_dir=tmp_path / "none",
                            disk=str(tmp_path), db_file=tmp_path / "none.db")
    assert "cpu_temp" not in health and "throttled" not in health and "clock_synced" not in health
    assert "disk" in health and "now" in health
    assert warnings(health) == []


def test_the_throttle_bits():
    assert throttle_flags(0x0) == ([], [])
    assert throttle_flags(0x50005) == (["under-voltage", "throttled"], ["under-voltage", "throttled"])
    assert throttle_flags(0x50000) == ([], ["under-voltage", "throttled"])


def test_warnings_say_whats_wrong(tmp_path):
    health = pi_health.read(tools(throttled="0x50005", synced="no", states={"garden-radio": "failed"}),
                            **fake_pi(tmp_path))
    health["cpu_temp"] = 78.0
    health["disk"] = {"total": 100, "free": 5}
    assert warnings(health) == [
        "garden-radio is failed",
        "under-voltage now: the power supply is sagging",
        "CPU at 78.0 °C",
        "SD card nearly full (5% free)",
        "the clock isn't synced to internet time",
    ]


def test_an_under_voltage_since_boot_isnt_a_warning_now():
    assert warnings({"throttled": 0x50000}) == []


def test_cached_between_refreshes():
    calls, clock = [], types.SimpleNamespace(t=0.0)
    cached = Cached(max_age=20, get=lambda: calls.append(1) or {"clock_synced": True}, clock=lambda: clock.t)
    cached(); clock.t = 10; cached()
    assert len(calls) == 1
    clock.t = 25
    assert cached()["warnings"] == [] and len(calls) == 2
