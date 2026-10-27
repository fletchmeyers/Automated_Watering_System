'''
Python 3 running on Raspberry Pi 3B

The Pi's own health at a glance, for the dashboard's Pi row (garden-api's
/api/pi_health): its four garden services, the CPU temperature, whether
its power supply has sagged (under-voltage) or it has slowed itself down,
memory, SD card space, the size of sensors.db, how long it's been up, and
whether its clock is synced to internet time (every timestamp it stores
is on that clock).

read() gathers it all; each part that can't be read is left out rather
than failing the rest. warnings() says what's worth looking at, in words.
Only standard tools: /proc and /sys, vcgencmd, systemctl and timedatectl.

Written by Fletcher Meyers
October 2026
'''

import os
import shutil
import subprocess
import time
from pathlib import Path

SERVICES = ("garden-radio", "garden-wifi", "garden-api", "garden-usb")
DB_FILE = Path(__file__).parent / "sensors.db"

CPU_HOT_C     = 75      # the Pi 3B slows itself down at 80 °C
DISK_LOW_FRAC = 0.10    # under 10% of the SD card free
MEM_LOW_FRAC  = 0.10    # under 10% of memory available

# vcgencmd get_throttled's bits: what's happening now, and what has
# happened since boot
THROTTLE_NOW = {0: "under-voltage", 1: "CPU speed capped", 2: "throttled", 3: "temperature limit"}
THROTTLE_SINCE_BOOT = {16 + bit: word for bit, word in THROTTLE_NOW.items()}


def _out(args, run):
    return run(args, capture_output=True, text=True, timeout=5).stdout.strip()


def throttle_flags(value):
    '''vcgencmd get_throttled's number as ([now], [since boot]) in words.'''
    return ([w for b, w in THROTTLE_NOW.items() if value >> b & 1],
            [w for b, w in THROTTLE_SINCE_BOOT.items() if value >> b & 1])


def read(run=subprocess.run, proc=Path("/proc"), sys_dir=Path("/sys"), disk="/", db_file=DB_FILE):
    '''The Pi's health now, as a dict; anything that can't be read is left out.'''
    health = {"now": time.strftime("%Y-%m-%dT%H:%M:%S")}

    def attempt(key, get):
        try:
            value = get()
            if value is not None:
                health[key] = value
        except (OSError, ValueError, IndexError, AttributeError, subprocess.SubprocessError):
            pass

    attempt("cpu_temp", lambda: round(int((sys_dir / "class/thermal/thermal_zone0/temp").read_text()) / 1000, 1))
    attempt("throttled", lambda: int(_out(["vcgencmd", "get_throttled"], run).split("=")[1], 16))
    attempt("load", lambda: round(os.getloadavg()[0], 2))
    attempt("cpus", os.cpu_count)

    def memory():
        info = {}
        for line in (proc / "meminfo").read_text().splitlines():
            name, value = line.split(":", 1)
            info[name] = int(value.split()[0]) * 1024
        return {"total": info["MemTotal"], "available": info["MemAvailable"]}
    attempt("memory", memory)

    def disk_space():
        usage = shutil.disk_usage(disk)
        return {"total": usage.total, "free": usage.free}
    attempt("disk", disk_space)
    attempt("db_bytes", lambda: db_file.stat().st_size)
    attempt("uptime_s", lambda: int(float((proc / "uptime").read_text().split()[0])))

    def synced():
        answer = _out(["timedatectl", "show", "-p", "NTPSynchronized", "--value"], run)
        return {"yes": True, "no": False}.get(answer)
    attempt("clock_synced", synced)

    def services():
        return {name: _out(["systemctl", "is-active", name], run) or "unknown" for name in SERVICES}
    attempt("services", services)
    return health


def warnings(health):
    '''What's worth looking at, in words: an empty list when all's well.'''
    found = []
    for name, state in (health.get("services") or {}).items():
        if state != "active":
            found.append(f"{name} is {state}")
    if "throttled" in health:
        now, _ = throttle_flags(health["throttled"])
        if "under-voltage" in now:
            found.append("under-voltage now: the power supply is sagging")
        elif now:
            found.append(", ".join(now) + " now")
    temp = health.get("cpu_temp")
    if temp is not None and temp >= CPU_HOT_C:
        found.append(f"CPU at {temp} °C")
    disk = health.get("disk")
    if disk and disk["total"] and disk["free"] / disk["total"] < DISK_LOW_FRAC:
        found.append(f"SD card nearly full ({disk['free'] / disk['total']:.0%} free)")
    memory = health.get("memory")
    if memory and memory["total"] and memory["available"] / memory["total"] < MEM_LOW_FRAC:
        found.append(f"memory low ({memory['available'] / memory['total']:.0%} available)")
    if health.get("clock_synced") is False:
        found.append("the clock isn't synced to internet time")
    return found


class Cached:
    '''read(), at most once every max_age seconds (the dashboard asks on every refresh).'''
    def __init__(self, max_age=20, get=read, clock=time.monotonic):
        self.max_age, self._get, self._clock = max_age, get, clock
        self._at, self._value = None, None

    def __call__(self):
        now = self._clock()
        if self._at is None or now - self._at >= self.max_age:
            health = self._get()
            health["warnings"] = warnings(health)
            self._at, self._value = now, health
        return self._value
