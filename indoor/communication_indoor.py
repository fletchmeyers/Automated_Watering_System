'''
Python 3 running on Raspberry Pi 3B

CommandManager: forward commands from the Pi to the Pico and verify acknowledgement.
BatchReceiver: collect incoming sensor packets, detect batch completion, send data_ack.
'''

import time
import json
from pathlib import Path
from sync_indoor import COMMAND_FILE


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
            time.sleep(0.5)  # let Pico finish processing data_ack and enter listen window
            radio.send(bytes(packet, "utf-8"))
            print(f"[CMD] Sent {len(packet)} bytes, waiting for ack...")
            self.pending = command
            self._last_sent = now
            return True
        except Exception as e:
            print(f"[CMD] Failed to send command: {e}")
            return False

    def handle_ack(self, data) -> bool:
        '''
        Handle a potential command ack packet.
        Returns True if the packet was a recognised command ack (and was consumed).
        Returns False if the packet is not a command ack and should be handled elsewhere.
        '''
        if self.pending is None:
            return False

        print(f"[CMD] handle_ack called with: {data}, pending: {self.pending}")

        pkt_type = data.get("t")
        confirmed_v = data.get("v")
        pending_t = self.pending.get("t")

        if pkt_type == "sync_ack" and pending_t == "sync":
            print(f"[CMD] Sync confirmed by Pico")
            Path(COMMAND_FILE).unlink(missing_ok=True)
            self.pending = None
            return True
        elif pkt_type == "set_interval_ack" and pending_t == "set_interval":
            if confirmed_v == self.pending.get("v"):
                print(f"[CMD] Pico confirmed: set_interval v={confirmed_v}")
                Path(COMMAND_FILE).unlink(missing_ok=True)
                self.pending = None
                return True
            else:
                print(f"[CMD] Ack mismatch — expected {self.pending.get('v')}, got {confirmed_v}")
                return True  # still consumed — it was a set_interval_ack, just mismatched

        return False


class BatchReceiver:
    '''
    Tracks packets arriving from the Pico within a single batch.

    A batch opens on receipt of a `ts` packet and closes when either:
      - a `batch_end` packet arrives, or
      - the expected packet count (from `batch_end.expected`) is reached.

    On close, sends a `data_ack` to the Pico.

    Packets that should not be written to the data file (ts, batch_end, and
    any packet consumed by CommandManager as a command ack) are filtered here.
    '''

    _SKIP_TYPES = {"ts", "batch_end", "sync_ack", "set_interval_ack"}

    def __init__(self, data_file):
        self.data_file = data_file
        self._reset()

    def _reset(self):
        self._current_ts = None
        self._received = []     # sensor packets collected this batch
        self._expected = None   # from batch_end.expected
        self._sent = None       # from batch_end.sent (Pico's own count)
        self._batch_end_q = None

    def open_batch(self, ts_value):
        '''Called when a `ts` packet arrives, marking the start of a new batch.'''
        if self._received:
            print(f"[BATCH] Warning: opening new batch with {len(self._received)} "
                  f"unwritten packets from previous batch — flushing.")
            self._flush(radio=None, send_ack=False)
        self._reset()
        self._current_ts = ts_value
        print(f"[BATCH] New batch opened. ts={ts_value}")

    def collect(self, data):
        '''
        Accept a sensor packet into the current batch.
        Attaches the current timestamp and appends to the buffer.
        Returns True if the batch is now complete (hit expected count without batch_end).
        '''
        if self._current_ts:
            data["ts"] = self._current_ts
        self._received.append(data)

        if self._expected is not None and len(self._received) >= self._expected:
            print(f"[BATCH] Expected count reached ({self._expected}) before batch_end.")
            return True
        return False

    def close_batch(self, batch_end_packet):
        '''
        Called when a `batch_end` packet arrives.
        Records expected/sent counts from the Pico for comparison.
        '''
        self._expected = batch_end_packet.get("expected")
        self._sent = batch_end_packet.get("sent")
        self._batch_end_q = batch_end_packet.get("q")

        if self._sent is not None and len(self._received) < self._sent:
            print(f"[BATCH] Pico sent {self._sent} packets but Pi received "
                  f"{len(self._received)} — {self._sent - len(self._received)} dropped in radio.")
        if self._sent is not None and self._expected is not None and self._sent < self._expected:
            print(f"[BATCH] Pico reported {self._sent} sent of {self._expected} expected "
                  f"— {self._expected - self._sent} sensor(s) failed on Pico.")

    def flush(self, radio):
        '''Write buffered packets to the data file and send data_ack to Pico.'''
        self._flush(radio=radio, send_ack=True)

    def _flush(self, radio, send_ack):
        if not self._received:
            self._reset()
            return

        with open(self.data_file, "a") as f:
            for pkt in self._received:
                f.write(json.dumps(pkt) + "\n")
        print(f"[BATCH] Wrote {len(self._received)} packets to file.")

        if send_ack and radio is not None and self._batch_end_q is not None:
            ack = json.dumps({"t": "data_ack", "q": self._batch_end_q}, separators=(",", ":"))
            radio.send(bytes(ack, "utf-8"))
            print(f"[BATCH] Sent data_ack (q={self._batch_end_q}).")
            time.sleep(0.2)  # give Pi radio time to switch back to RX before next batch opens

        self._reset()