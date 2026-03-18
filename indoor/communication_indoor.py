'''
Python 3 running on Raspberry Pi 3B

CommandManager: forward commands from the Pi to the Pico and verify acknowledgement.
'''

import time
import json
from pathlib import Path
from sync_indoor import COMMAND_FILE, sensor_health_report


class CommandManager:
    def __init__(self):
        self.pending = None
        self._last_sent = 0

    def check_and_forward(self, radio):
        cmd_path = Path(COMMAND_FILE)
        if not cmd_path.exists():
            return False
        now = time.monotonic()
        if now - self._last_sent < 10:  # wait at least 10s between attempts
            return False
        try:
            command = json.loads(cmd_path.read_text())
            packet = json.dumps(command, separators=(",", ":"))
            radio.send(bytes(packet, "utf-8"))
            print(f"[CMD] Sent {len(packet)} bytes, waiting for ack...")
            self.pending = command
            self._last_sent = now
            return True
        except Exception as e:
            print(f"[CMD] Failed to send command: {e}")
            return False
    def handle_ack(self, data):
        print(f"[CMD] handle_ack called with: {data}, pending: {self.pending}")
        if self.pending is None:
            return

        pkt_type = data.get("t")
        confirmed_v = data.get("v")
        pending_t = self.pending.get("t")

        if pkt_type == "sync_ack" and pending_t == "sync":
            print(f"[CMD] Sync confirmed by Pico")
            Path(COMMAND_FILE).unlink(missing_ok=True)
            self.pending = None
        elif pkt_type == "set_interval_ack" and pending_t == "set_interval":
            if confirmed_v == self.pending.get("v"):
                print(f"[CMD] Pico confirmed: set_interval v={confirmed_v}")
                Path(COMMAND_FILE).unlink(missing_ok=True)
                self.pending = None
            else:
                print(f"[CMD] Ack mismatch — expected {self.pending.get('v')}, got {confirmed_v}")