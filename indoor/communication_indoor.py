'''
Python 3 running on Raspberry Pi 3B

CommandManager: forward commands from the Pi to the Pico and verify acknowledgement.
BatchReceiver:  collect incoming sensor packets, detect batch completion, send acks.
PollingTimer:   decide when to poll each node on a regular schedule.
'''

import time
import json
from pathlib import Path
from sync_indoor import COMMAND_FILE, DATA_FILE

CMD_TIMEOUT = 60   # seconds before giving up on an unacked command


class PollingTimer:
    '''
    Tracks when each node is due for a poll and when a bulk sync is due.

    poll_interval  — seconds between polls per node.
    sync_interval  — seconds between bulk SD syncs per node (0 or None = disabled).

    Call due_nodes() each loop iteration to get node IDs ready to be polled.
    Call sync_due(node_id) to check whether a bulk sync should replace the next poll.
    '''
    def __init__(self, node_ids, poll_interval=60, sync_interval=3600):
        self.node_ids      = list(node_ids)
        self.poll_interval = poll_interval
        self.sync_interval = sync_interval
        now = time.monotonic()
        # Stagger initial polls so nodes don't all fire at once on startup
        self._last_poll = {nid: now - i * (poll_interval / max(len(node_ids), 1))
                           for i, nid in enumerate(node_ids)}
        self._last_sync = {nid: now for nid in node_ids}

    def due_nodes(self):
        '''Return list of node IDs whose poll timer has elapsed.'''
        now = time.monotonic()
        return [nid for nid in self.node_ids
                if now - self._last_poll[nid] >= self.poll_interval]

    def sync_due(self, node_id):
        '''Return True if a bulk sync is due for this node.'''
        if not self.sync_interval:
            return False
        return time.monotonic() - self._last_sync[node_id] >= self.sync_interval

    def mark_polled(self, node_id):
        self._last_poll[node_id] = time.monotonic()

    def mark_synced(self, node_id):
        self._last_sync[node_id] = time.monotonic()


class CommandManager:
    '''
    Reads the command file written by sync_indoor helpers, forwards the command
    to the Pico over radio, and waits for an acknowledgement packet.

    Retries every 10 seconds up to CMD_TIMEOUT seconds total, then gives up
    and deletes the command file with a warning so the loop isn't blocked
    indefinitely by an unresponsive node.

    Clears the command file only after a confirmed ack or timeout.
    '''
    def __init__(self):
        self.pending     = None
        self._last_sent  = 0
        self._first_sent = 0

    def check_and_forward(self, radio):
        cmd_path = Path(COMMAND_FILE)
        if not cmd_path.exists():
            self.pending = None
            return False

        now = time.monotonic()

        # Give up if the command has been pending too long
        if self._first_sent and now - self._first_sent >= CMD_TIMEOUT:
            print(f"[CMD] Timed out after {CMD_TIMEOUT}s waiting for ack on "
                  f"{self.pending.get('t')!r} — giving up.")
            cmd_path.unlink(missing_ok=True)
            self.pending     = None
            self._first_sent = 0
            self._last_sent  = 0
            return False

        # Rate-limit retries
        if now - self._last_sent < 10:
            return False

        try:
            command = json.loads(cmd_path.read_text())
            packet  = json.dumps(command, separators=(",", ":"))
            time.sleep(0.5)  # let Pico finish any in-progress work before listening
            radio.send(bytes(packet, "utf-8"))
            print(f"[CMD] Sent: {packet}")
            self.pending    = command
            self._last_sent = now
            if not self._first_sent:
                self._first_sent = now
            return True
        except Exception as e:
            print(f"[CMD] Failed to send command: {e}")
            return False

    def handle_ack(self, data) -> bool:
        '''
        Returns True if data is a recognised ack for the pending command (consumed).
        Returns False if the packet should be handled elsewhere.
        '''
        if self.pending is None:
            return False

        pkt_type  = data.get("t")
        pending_t = self.pending.get("t")

        if pkt_type == "set_interval_ack" and pending_t == "set_interval":
            confirmed_v = data.get("v")
            if confirmed_v == self.pending.get("v"):
                print(f"[CMD] Pico confirmed set_interval v={confirmed_v}.")
            else:
                print(f"[CMD] set_interval_ack mismatch — expected {self.pending.get('v')}, "
                      f"got {confirmed_v}.")
            self._clear_pending()
            return True

        if pkt_type == "sync_complete" and pending_t == "sync_request":
            chunks = data.get("chunks", "?")
            print(f"[CMD] Bulk sync complete ({chunks} chunks).")
            self._clear_pending()
            return True

        return False

    def _clear_pending(self):
        Path(COMMAND_FILE).unlink(missing_ok=True)
        self.pending     = None
        self._first_sent = 0
        self._last_sent  = 0


class BatchReceiver:
    '''
    Collects sensor packets arriving from the Pico within a single batch.

    A batch opens on receipt of a "ts" packet (poll response) and closes
    when batch_end arrives. If a sensor packet arrives before a ts packet
    (e.g. ts was dropped in radio transit), open_batch() is called defensively
    with a placeholder so the packet is not lost.

    For bulk sync chunks, sends a per-chunk data_ack carrying the chunk number
    so the Pico can advance to the next chunk.
    For poll responses (no chunk field), sends a plain data_ack.
    '''

    _SKIP_TYPES = {"ts", "batch_end", "set_interval_ack", "sync_complete"}

    def __init__(self, data_file=DATA_FILE):
        self.data_file = data_file
        self._reset()

    def _reset(self):
        self._current_ts  = None
        self._received    = []
        self._expected    = None
        self._sent        = None
        self._batch_end_q = None
        self._chunk       = None   # present only during bulk sync

    def open_batch(self, ts_value):
        if self._received:
            print(f"[BATCH] Warning: {len(self._received)} unwritten packets — flushing.")
            self._flush(radio=None, send_ack=False)
        self._reset()
        self._current_ts = ts_value
        print(f"[BATCH] New batch opened. ts={ts_value}")

    def collect(self, data):
        '''
        Accept a sensor packet. Attaches current ts and appends to buffer.
        If no batch is open (ts packet was dropped), opens one with a placeholder
        so the sensor packet is not silently discarded.
        Returns True if expected count reached before batch_end.
        '''
        if self._current_ts is None:
            print("[BATCH] Sensor packet arrived before ts — opening batch with placeholder.")
            self.open_batch("unknown")

        data["ts"] = self._current_ts
        self._received.append(data)

        if self._expected is not None and len(self._received) >= self._expected:
            print(f"[BATCH] Expected count {self._expected} reached before batch_end.")
            return True
        return False

    def close_batch(self, batch_end_packet):
        # Keys shortened on Pico side to stay under 60-byte radio limit
        self._expected    = batch_end_packet.get("exp")
        self._sent        = batch_end_packet.get("snt")
        self._batch_end_q = batch_end_packet.get("q")
        self._chunk       = batch_end_packet.get("chk")  # None for poll responses

        if self._sent is not None and len(self._received) < self._sent:
            dropped = self._sent - len(self._received)
            print(f"[BATCH] {dropped} packet(s) dropped in radio "
                  f"(Pico sent {self._sent}, Pi received {len(self._received)}).")
        if (self._sent is not None and self._expected is not None
                and self._sent < self._expected):
            failed = self._expected - self._sent
            print(f"[BATCH] {failed} sensor(s) failed on Pico "
                  f"({self._sent}/{self._expected} sent).")

    def flush(self, radio):
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
            ack = {"t": "data_ack", "q": self._batch_end_q}
            if self._chunk is not None:
                ack["chk"] = self._chunk
            radio.send(bytes(json.dumps(ack, separators=(",", ":")), "utf-8"))
            print(f"[BATCH] Sent data_ack (q={self._batch_end_q}"
                  + (f", chk={self._chunk}" if self._chunk is not None else "") + ").")
            time.sleep(0.2)

        self._reset()

