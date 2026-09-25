'''
Python 3 running on Raspberry Pi 3B

CommandManager:      forward commands from the Pi to the Pico and verify acknowledgement.
BatchReceiver:       collect incoming sensor packets, detect batch completion, send acks.
PollingTimer:        decide when to poll each node on a regular schedule.
FragmentReassembler: stitch oversized packets back together from radio fragments.
SyncManager:         pull logged SD data from nodes, one chunk per request.
'''

import time
import json
from datetime import date, datetime
from pathlib import Path
from sync_indoor import COMMAND_FILE, DATA_FILE, PING_PROGRESS_FILE

import db

CMD_TIMEOUT =45   # seconds before giving up on an unacked command

ARCHIVE_DIR = Path(__file__).parent / "archive"
ARCHIVE_DIR.mkdir(exist_ok=True)

def _archive_path():
    return ARCHIVE_DIR / f"data_{date.today().isoformat()}.txt"


class PollingTimer:
    '''
    Tracks when each node is due for a poll.

    poll_interval  — seconds between polls per node.

    Call due_nodes() each loop iteration to get node IDs ready to be polled.
    '''
    def __init__(self, node_ids, poll_interval=60):
        self.node_ids      = list(node_ids)
        self.poll_interval = poll_interval
        now = time.monotonic()
        # Stagger initial polls so nodes don't all fire at once on startup
        self._last_poll = {nid: now - i * (poll_interval / max(len(node_ids), 1))
                           for i, nid in enumerate(node_ids)}

    def due_nodes(self):
        '''Return list of node IDs whose poll timer has elapsed.'''
        now = time.monotonic()
        return [nid for nid in self.node_ids
                if now - self._last_poll[nid] >= self.poll_interval]

    def mark_polled(self, node_id):
        self._last_poll[node_id] = time.monotonic()


class CommandManager:
    '''
    Reads the command file written by sync_indoor helpers, forwards the command
    to the Pico over radio, and waits for an acknowledgement packet.

    Retries every 10 seconds up to CMD_TIMEOUT seconds total, then gives up
    and deletes the command file with a warning so the loop isn't blocked
    indefinitely by an unresponsive node.

    Clears the command file only after a confirmed ack or timeout.

    on_send, if given, is called with the command dict every time it goes out
    over the radio (first send and every retry). timed_out holds the last
    command that was given up on, until the caller clears it.
    '''
    def __init__(self, on_send=None):
        self.pending     = None
        self.timed_out   = None
        self._on_send    = on_send
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
            self.timed_out   = self.pending
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
            if self._on_send is not None:
                self._on_send(command)
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

        if pkt_type == "batch_end" and pending_t == "poll":
            print(f"[CMD] Pico confirmed poll (batch received).")
            self._clear_pending()
            return True

        if pkt_type == "se" and pending_t == "sync":
            self._clear_pending()
            return True

        return False

    def _clear_pending(self):
        Path(COMMAND_FILE).unlink(missing_ok=True)
        self.pending     = None
        self._first_sent = 0
        self._last_sent  = 0


def run_ping_test(radio, node_id=1, count=10, timeout=1.5):
    '''
    Fire `count` bare ping packets at the Pico back-to-back, each waiting up
    to `timeout` seconds for a matching pong, and return hit/miss + round-trip
    time for each. Deliberately bypasses CommandManager — that class is built
    for one outstanding command with retries over tens of seconds, not a tight
    burst of sub-second round trips, and bending it to fit would add more
    complexity than it'd save.

    Only call this when cmd.pending is None (same guard main.py already uses
    before issuing a scheduled poll) so this can't collide with a command
    CommandManager is mid-flight on.

    Any non-pong packet received while waiting (e.g. a stray batch packet)
    is ignored for matching purposes and effectively dropped — acceptable
    here since this is a short, deliberately blocking diagnostic, not part
    of normal data flow.
    '''
    results = []
    for q in range(count):
        packet = json.dumps({"t": "ping", "q": q, "n": node_id}, separators=(",", ":"))
        sent_at = time.monotonic()
        radio.send(packet.encode("utf-8"))

        deadline = sent_at + timeout
        rtt_ms = None
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            resp = radio.receive(with_header=True, timeout=max(0, remaining))
            if resp is None:
                break
            try:
                data = json.loads(resp[4:].decode("utf-8"))
                if data.get("t") == "pong" and data.get("pq") == q:
                    rtt_ms = round((time.monotonic() - sent_at) * 1000, 1)
                    break
            except Exception:
                continue

        results.append({"q": q, "ok": rtt_ms is not None, "rtt_ms": rtt_ms})

        hits_so_far = sum(1 for r in results if r["ok"])
        try:
            Path(PING_PROGRESS_FILE).write_text(json.dumps({
                "done":  q + 1,
                "count": count,
                "hits":  hits_so_far,
            }))
        except Exception as e:
            print(f"[PING] Could not write progress: {e}")

    hits = sum(1 for r in results if r["ok"])
    rtts = [r["rtt_ms"] for r in results if r["ok"]]
    avg_rtt = round(sum(rtts) / len(rtts), 1) if rtts else None

    print(f"[PING] Test complete: {hits}/{count} pongs, avg {avg_rtt}ms")

    return {
        "count":      count,
        "hits":       hits,
        "misses":     count - hits,
        "avg_rtt_ms": avg_rtt,
        "results":    results,
    }


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

    If db_conn is provided, every flushed batch is also written to SQLite
    (sensors.db) alongside the existing flat-file writes. A failure writing
    to the DB is logged and otherwise ignored — the flat files remain the
    source of truth while the DB migration is still in progress.
    '''

    _SKIP_TYPES = {"ts", "batch_end", "set_interval_ack", "se"}

    def __init__(self, data_file=DATA_FILE, db_conn=None):
        self.data_file = data_file
        self.db_conn = db_conn
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
        return self._flush(radio=radio, send_ack=True)

    def _flush(self, radio, send_ack):
        if not self._received:
            self._reset()
            return None

        written = list(self._received)  # snapshot before _reset() clears it

        with open(self.data_file, "a") as f:
            for pkt in self._received:
                f.write(json.dumps(pkt) + "\n")

        # Also append to today's untrimmed archive (never rotated/trimmed by cron)
        with open(_archive_path(), "a") as f:
            for pkt in self._received:
                f.write(json.dumps(pkt) + "\n")

        print(f"[BATCH] Wrote {len(self._received)} packets to file.")

        if self.db_conn is not None:
            try:
                db.insert_batch(self.db_conn, self._received)
                print(f"[DB] Wrote {len(self._received)} packets to sensors.db.")
            except Exception as e:
                print(f"[DB] Failed to write batch to sensors.db: {e}")

        if send_ack and radio is not None and self._batch_end_q is not None:
            ack = {"t": "data_ack", "q": self._batch_end_q}
            if self._chunk is not None:
                ack["chk"] = self._chunk
            radio.send(bytes(json.dumps(ack, separators=(",", ":")), "utf-8"))
            print(f"[BATCH] Sent data_ack (q={self._batch_end_q}"
                  + (f", chk={self._chunk}" if self._chunk is not None else "") + ").")
            time.sleep(0.2)

        self._reset()
        return written



class FragmentReassembler:
    '''
    Rebuilds payloads a node had to split across several radio packets
    (PacketSender.send_raw() in communication_garden.py). Each fragment
    starts with a plain-text header "~<node>.<msg>.<i>.<k>|" — fragment i of
    k for message msg. Messages still incomplete after max_age seconds are
    dropped, since a lost fragment means the rest will never be usable.
    '''
    def __init__(self, max_age=15):
        self.max_age = max_age
        self._pending = {}   # (node, msg) -> {"k": total, "parts": {i: bytes}, "at": first seen}

    def feed(self, payload):
        '''Take one fragment; return the full payload once all its fragments are in, else None.'''
        now = time.monotonic()
        for key in [key for key, entry in self._pending.items() if now - entry["at"] > self.max_age]:
            entry = self._pending.pop(key)
            print(f"[FRAG] Dropped incomplete message node={key[0]} msg={key[1]} "
                  f"({len(entry['parts'])}/{entry['k']} fragments).")

        try:
            header, body = payload[1:].split(b"|", 1)
            node, msg, i, k = (int(x) for x in header.split(b"."))
        except ValueError:
            print(f"[FRAG] Bad fragment header: {payload[:16]!r}")
            return None

        entry = self._pending.setdefault((node, msg), {"k": k, "parts": {}, "at": now})
        entry["parts"][i] = body
        if not all(j in entry["parts"] for j in range(entry["k"])):
            return None
        del self._pending[(node, msg)]
        return b"".join(entry["parts"][j] for j in range(entry["k"]))


class SyncManager:
    '''
    Pulls logged SD data from nodes one chunk at a time, entirely driven from
    the Pi (see send_sync_chunk() in communication_garden.py for the node side).

    Each node gets one session per clock hour, capped at lines_per_session
    lines, so a large backlog is worked through over many hours rather than
    tying up the radio. main.py only asks for the next chunk when nothing
    else is pending, so scheduled polls slot in between chunks.

    A chunk's lines are buffered and only stored once its "se" (sync end)
    packet arrives. If fewer lines arrived than the node says it sent, the
    same chunk is requested again (up to max_retries) — the node only moves
    its cursor forward once the next request confirms the previous offset.
    The confirmed cursor is kept per node across sessions, so the next
    session carries on without resending the last chunk (only a Pi restart
    loses it, which costs at most one duplicated chunk).

    Synced lines go to sensors.db and a sync archive file, never DATA_FILE —
    that file is pushed to git every 5 minutes and isn't meant for backlog.
    '''
    def __init__(self, node_ids, lines_per_session=2000, chunk_lines=20,
                 max_retries=3, db_conn=None):
        self.node_ids          = list(node_ids)
        self.lines_per_session = lines_per_session
        self.chunk_lines       = chunk_lines
        self.max_retries       = max_retries
        self.db_conn           = db_conn

        self.active   = None    # node ID with a session in progress
        self.awaiting = False   # a chunk has been requested and its "se" hasn't arrived
        self._last_hour     = {}      # node ID -> "YYYY-MM-DDTHH" of its last session
        self._requested     = []      # node IDs with a manual "sync now" request
        self._cursors       = {}      # node ID -> {"g": gen, "o": offset} last stored
        self._buffer        = []
        self._retries       = 0
        self._session_lines = 0

    def request_now(self, node_id):
        '''Start a session for node_id as soon as the radio is free, regardless of the hour.'''
        if node_id not in self._requested:
            self._requested.append(node_id)

    def next_command(self):
        '''Return the next "sync" command to send, or None if there's nothing to do.'''
        if self.active is None and not self._start_session():
            return None
        command = {"t": "sync", "n": self.active, "k": self.chunk_lines}
        command.update(self._cursors.get(self.active, {}))
        self.awaiting = True
        return command

    def _start_session(self):
        hour = datetime.now().strftime("%Y-%m-%dT%H")
        if self._requested:
            node_id = self._requested.pop(0)
        else:
            due = [nid for nid in self.node_ids if self._last_hour.get(nid) != hour]
            if not due:
                return False
            node_id = due[0]
        self._last_hour[node_id] = hour
        self.active         = node_id
        self._buffer        = []
        self._retries       = 0
        self._session_lines = 0
        print(f"[SYNC] Session started for node {node_id} (up to {self.lines_per_session} lines).")
        return True

    def on_send(self, command):
        '''CommandManager hook: a (re)sent chunk request starts that chunk over.'''
        if command.get("t") == "sync":
            self._buffer = []

    def collect(self, line):
        self._buffer.append(line)

    def handle_end(self, se):
        '''Process a chunk's "se" packet: store the chunk, or re-request it if lines went missing.'''
        if not self.awaiting:
            return
        self.awaiting = False
        node_id = self.active
        count   = se.get("c", 0)
        got     = len(self._buffer)

        if got < count and self._retries < self.max_retries:
            self._retries += 1
            print(f"[SYNC] Node {node_id}: got {got}/{count} lines — re-requesting chunk "
                  f"(retry {self._retries}/{self.max_retries}).")
            return
        if got < count:
            print(f"[SYNC] Node {node_id}: storing {got}/{count} lines after "
                  f"{self.max_retries} retries — {count - got} lost.")

        self._store(node_id, self._buffer)
        self._buffer  = []
        self._retries = 0
        self._cursors[node_id] = {"g": se.get("g"), "o": se.get("o")}
        self._session_lines += count

        if not se.get("m") or count == 0:
            self._end_session("caught up")
        elif self._session_lines >= self.lines_per_session:
            self._end_session("hourly limit reached, more waiting")

    def abort(self):
        '''The chunk request timed out — give up until the next hour.'''
        if self.active is not None:
            self._end_session("node stopped responding")

    def _end_session(self, reason):
        print(f"[SYNC] Session for node {self.active} ended after "
              f"{self._session_lines} lines ({reason}).")
        self.active   = None
        self.awaiting = False
        self._buffer  = []

    def _store(self, node_id, lines):
        if not lines:
            return
        for line in lines:
            line.setdefault("n", node_id)

        with open(ARCHIVE_DIR / f"sync_{date.today().isoformat()}.txt", "a") as f:
            for line in lines:
                f.write(json.dumps(line) + "\n")

        if self.db_conn is not None:
            try:
                db.insert_batch(self.db_conn, lines)
            except Exception as e:
                print(f"[DB] Failed to write sync chunk to sensors.db: {e}")
