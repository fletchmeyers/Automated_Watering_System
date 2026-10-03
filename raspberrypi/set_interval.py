# set_interval.py  — run on the Pi
import json
import sys
from pathlib import Path

COMMAND_FILE = "/tmp/pico_command.json"

if len(sys.argv) != 2:
    print("Usage: python3 set_interval.py <seconds>  (0 = indefinite sleep)")
    sys.exit(1)

try:
    interval = int(sys.argv[1])
    if interval < 0:
        raise ValueError
except ValueError:
    print("Interval must be a positive integer.")
    sys.exit(1)

command = {"t": "set_interval", "v": interval}
Path(COMMAND_FILE).write_text(json.dumps(command))
print(f"[INTERVAL] Command written: {command}")