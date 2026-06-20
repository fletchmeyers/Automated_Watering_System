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


class PollingTimer:
    '''
    Tracks when each node is due for a poll and when a bulk sync is due.

    poll_interval    — how often (seconds) to send a "poll" to each node.
    sync_interval    — how often (seconds) to request a bulk SD sync from each node.
                       Set to 0 or None to disable automatic bulk sync.

    Call due_nodes() each loop iteration to get a list of node IDs ready to be polled.
    Call sync_due(node_id) to check if a bulk sync should be requested instead of a poll.
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

    Retries no more than once per 10 seconds to avoid flooding a slow Pico.
    Clears the command file only after a confirmed ack.
    '''
    def __init__(self):
        self.pending    = None
        self._last_sent = 0

    def check_and_forward(self, radio):
        cmd_path = Path(COMMAND_FILE)
        if not cmd_path.exists():
            return False
        now = time.monotonic()
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

        pkt_type   = data.get("t")
        pending_t  = self.pending.get("t")

        if pkt_type == "sync_ack" and pending_t == "sync":
            print("[CMD] Sync confirmed by Pico.")
            Path(COMMAND_FILE).unlink(missing_ok=True)
            self.pending = None
            return True

        if pkt_type == "set_interval_ack" and pending_t == "set_interval":
            confirmed_v = data.get("v")
            if confirmed_v == self.pending.get("v"):
                print(f"[CMD] Pico confirmed set_interval v={confirmed_v}.")
            else:
                print(f"[CMD] set_interval_ack mismatch — expected {self.pending.get('v')}, "
                      f"got {confirmed_v}.")
            Path(COMMAND_FILE).unlink(missing_ok=True)
            self.pending = None
            return True

        if pkt_type == "sync_complete" and pending_t == "sync_request":
            chunks = data.get("chunks", "?")
            print(f"[CMD] Bulk sync complete ({chunks} chunks).")
            Path(COMMAND_FILE).unlink(missing_ok=True)
            self.pending = None
            return True

        return False


class BatchReceiver:
    '''
    Collects sensor packets arriving from the Pico within a single batch.

    A batch opens on receipt of a "ts" packet (poll response) or the first
    data packet of a bulk sync chunk. It closes when batch_end arrives.

    For bulk sync chunks, sends a per-chunk data_ack carrying the chunk number
    so the Pico can advance to the next chunk.

    For poll responses (no chunk field), sends a plain data_ack as before.

    Packets in _SKIP_TYPES are not written to the data file.
    '''

    _SKIP_TYPES = {"ts", "batch_end", "sync_ack", "set_interval_ack", "sync_complete"}

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
        Returns True if expected count reached before batch_end.
        '''
        if self._current_ts:
            data["ts"] = self._current_ts
        self._received.append(data)

        if self._expected is not None and len(self._received) >= self._expected:
            print(f"[BATCH] Expected count {self._expected} reached before batch_end.")
            return True
        return False

    def close_batch(self, batch_end_packet):
        self._expected    = batch_end_packet.get("expected")
        self._sent        = batch_end_packet.get("sent")
        self._batch_end_q = batch_end_packet.get("q")
        self._chunk       = batch_end_packet.get("chunk")  # None for poll responses

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
            # Include chunk number if this is part of a bulk sync so the Pico
            # knows which chunk is being acked and can advance its cursor.
            ack = {"t": "data_ack", "q": self._batch_end_q}
            if self._chunk is not None:
                ack["chunk"] = self._chunk
            radio.send(bytes(json.dumps(ack, separators=(",", ":")), "utf-8"))
            print(f"[BATCH] Sent data_ack (q={self._batch_end_q}"
                  + (f", chunk={self._chunk}" if self._chunk is not None else "") + ").")
            time.sleep(0.2)

        self._reset()

