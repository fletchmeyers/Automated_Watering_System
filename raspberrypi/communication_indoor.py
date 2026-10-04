'''
Python 3 running on Raspberry Pi 3B

CommandManager:      forward commands from the Pi to the Pico and verify acknowledgement.
BatchReceiver:       collect incoming sensor packets, detect batch completion, send acks.
PollingTimer:        decide when to poll each node on a regular schedule.
NodeHealth:          stop spending radio time on nodes that aren't answering.
SleepScheduler:      put battery nodes to sleep overnight and leave them alone until they wake.
FragmentReassembler: stitch oversized packets back together from radio fragments.
SyncManager:         pull logged SD data from nodes, one chunk per request.
'''

import time
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from sync_indoor import COMMAND_FILE, DATA_FILE, PING_PROGRESS_FILE

import db

CMD_TIMEOUT =45   # seconds before giving up on an unacked command

# Seconds between resends of an unacked command. A sync chunk answers within
# ~2s, so a lost reply is retried quickly instead of costing a full 10s.
RETRY_INTERVAL      = 10
SYNC_RETRY_INTERVAL = 4

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

    Retries every RETRY_INTERVAL seconds (SYNC_RETRY_INTERVAL for sync
    chunks) up to CMD_TIMEOUT seconds total, then gives up
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
        retry = SYNC_RETRY_INTERVAL if (self.pending or {}).get("t") == "sync" else RETRY_INTERVAL
        if now - self._last_sent < retry:
            return False

        try:
            command = json.loads(cmd_path.read_text())
            if command.get("t") == "poll":
                # Stamp the time as it goes out, not when it was queued — a
                # poll retried for up to CMD_TIMEOUT would otherwise set the
                # node's clock that far behind.
                command["ts"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            if self._on_send is not None:
                self._on_send(command)   # may adjust the command, so before it's encoded
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

        if pkt_type == "batch_end" and pending_t == "poll":
            print(f"[CMD] Pico confirmed poll (batch received).")
            self._clear_pending()
            return True

        if pkt_type == "se" and pending_t == "sync":
            self._clear_pending()
            return True

        if pkt_type == "info" and pending_t == "info":
            self._clear_pending()
            return True

        if pkt_type == "sleep_ack" and pending_t == "sleep":
            self._clear_pending()
            return True

        return False

    def _clear_pending(self):
        Path(COMMAND_FILE).unlink(missing_ok=True)
        self.pending     = None
        self._first_sent = 0
        self._last_sent  = 0


def run_ping_test(radio, node_id=1, count=10, timeout=1.5, report_progress=True):
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

        if not report_progress:
            continue
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


class NodeHealth:
    '''
    Tracks which nodes are answering, so one that isn't — powered off, out of
    range, or asleep — stops tying up the radio. Every command waits up to
    CMD_TIMEOUT for a reply, and only one command can be outstanding, so a
    silent node polled every minute would otherwise cost ~45s of every
    minute for everyone else.

    After max_misses timed-out commands in a row a node is unreachable: it
    gets no polls, storage requests or sync chunks, just a quick ping every
    probe_interval seconds. Any packet heard from it makes it reachable again.
    '''
    def __init__(self, node_ids, max_misses=3, probe_interval=300):
        self.max_misses     = max_misses
        self.probe_interval = probe_interval
        self._misses        = {nid: 0 for nid in node_ids}
        self._last_probe    = {nid: 0.0 for nid in node_ids}

    def reachable(self, node_id):
        return self._misses.get(node_id, 0) < self.max_misses

    def missed(self, node_id):
        '''A command to node_id timed out.'''
        if node_id not in self._misses:
            return
        self._misses[node_id] += 1
        if self._misses[node_id] == self.max_misses:
            print(f"[HEALTH] Node {node_id} missed {self.max_misses} commands in a row — "
                  f"marking unreachable, checking every {self.probe_interval}s.")
            self._last_probe[node_id] = time.monotonic()

    def heard(self, node_id):
        '''Any packet from node_id arrived.'''
        if node_id not in self._misses:
            return
        if not self.reachable(node_id):
            print(f"[HEALTH] Node {node_id} is answering again.")
        self._misses[node_id] = 0

    def probe_due(self):
        '''Unreachable nodes due for a check.'''
        now = time.monotonic()
        return [nid for nid in self._misses
                if not self.reachable(nid) and now - self._last_probe[nid] >= self.probe_interval]

    def mark_probed(self, node_id):
        self._last_probe[node_id] = time.monotonic()


ISO_FORMAT = "%Y-%m-%dT%H:%M:%S"


class SleepScheduler:
    '''
    Puts battery nodes into deep sleep for a nightly window and keeps track
    of which are asleep, so nothing is sent to them until they wake.

    windows: {node_id: ("HH:MM", "HH:MM")} in the Pi's local time; a window
    may cross midnight (e.g. 19:00-07:00). A node that's inside its window,
    awake and answering gets {"t":"sleep","n":..,"w":"<wake time>"}. The wake
    time is absolute, so a retried command can't push it later.

    A sleep_ack with ok=1 marks the node asleep until w. ok=0 means it
    refused (e.g. its clock isn't set yet); it's asked again after
    retry_after seconds. A sleep command that times out counts as accepted:
    the node answers before switching its radio off, so a lost ack and a
    sleeping node look exactly the same from here.
    '''
    def __init__(self, windows, wake_grace=90, retry_after=600, now_fn=datetime.now):
        self.windows     = dict(windows)
        self.wake_grace  = wake_grace    # seconds after wake before commands resume
        self.retry_after = retry_after
        self._now        = now_fn
        self._asleep_until = {}          # node ID -> datetime it wakes
        self._not_before   = {}          # node ID -> no sleep command before this
        self._requested    = {}          # node ID -> wake datetime for a manual sleep

    @staticmethod
    def _at(day, hhmm):
        hour, minute = (int(x) for x in hhmm.split(":"))
        return day.replace(hour=hour, minute=minute, second=0, microsecond=0)

    def window_wake(self, node_id, now):
        '''If now is inside node_id's window, when that window ends; else None.'''
        if node_id not in self.windows:
            return None
        start_s, end_s = self.windows[node_id]
        start, end = self._at(now, start_s), self._at(now, end_s)
        if start <= end:                      # e.g. 01:00-05:00
            return end if start <= now < end else None
        if now >= start:                      # crosses midnight, evening side
            return end + timedelta(days=1)
        if now < end:                         # crosses midnight, morning side
            return end
        return None

    def asleep(self, node_id):
        until = self._asleep_until.get(node_id)
        return until is not None and self._now() < until + timedelta(seconds=self.wake_grace)

    def asleep_until(self, node_id):
        '''When node_id wakes, if it's asleep; else None.'''
        return self._asleep_until[node_id] if self.asleep(node_id) else None

    def request_now(self, node_id, minutes):
        '''Sleep node_id for `minutes` from now, regardless of its window.'''
        self._requested[node_id] = self._now() + timedelta(minutes=minutes)
        self._not_before.pop(node_id, None)

    def next_command(self, available=lambda node_id: True):
        '''Return a "sleep" command for the first node that's due one, or None.'''
        now = self._now()
        for node_id in sorted(set(self.windows) | set(self._requested)):
            if self.asleep(node_id) or not available(node_id):
                continue
            if node_id in self._not_before and now < self._not_before[node_id]:
                continue
            wake = self._requested.get(node_id) or self.window_wake(node_id, now)
            if wake is None or wake <= now:
                self._requested.pop(node_id, None)
                continue
            return {"t": "sleep", "n": node_id, "w": wake.strftime(ISO_FORMAT)}
        return None

    def handle_ack(self, ack):
        node_id = ack.get("n")
        if ack.get("ok"):
            self._went_to_sleep(node_id, ack.get("w"))
        else:
            self._not_before[node_id] = self._now() + timedelta(seconds=self.retry_after)
            print(f"[SLEEP] Node {node_id} refused to sleep ({ack.get('why', 'no reason given')}) "
                  f"— asking again in {self.retry_after}s.")

    def timed_out(self, command):
        print(f"[SLEEP] No ack from node {command.get('n')} — assuming it's asleep.")
        self._went_to_sleep(command.get("n"), command.get("w"))

    def _went_to_sleep(self, node_id, wake_str):
        try:
            wake = datetime.strptime(wake_str, ISO_FORMAT)
        except (TypeError, ValueError):
            print(f"[SLEEP] Node {node_id} sent an unreadable wake time: {wake_str!r}")
            return
        self._asleep_until[node_id] = wake
        self._requested.pop(node_id, None)
        print(f"[SLEEP] Node {node_id} asleep until {wake_str}.")


def _is_iso(value):
    '''True if value is a timestamp in ISO_FORMAT (not None, "unknown", etc.).'''
    try:
        datetime.strptime(value, ISO_FORMAT)
        return True
    except (TypeError, ValueError):
        return False


class BatchReceiver:
    '''
    Collects sensor packets arriving from the Pico within a single batch.

    A batch opens on receipt of a "ts" packet (poll response) and closes
    when batch_end arrives. If a sensor packet arrives before a ts packet
    (e.g. ts was dropped in radio transit), open_batch() is called defensively
    so the packet is not lost. The batch then takes the Pi's own clock as its
    timestamp — the node answers within a second or so of the poll, and the
    poll's ts it would have echoed came from that same clock. Likewise if the
    ts packet carries no usable time.

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

    def open_batch(self, ts_value=None):
        if not _is_iso(ts_value):
            ts_value = datetime.now().strftime(ISO_FORMAT)
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
            print("[BATCH] Sensor packet arrived before ts — stamping the batch with the Pi's clock.")
            self.open_batch()

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
    tying up the radio. Sessions for different nodes run side by side, taking
    turns chunk by chunk, so one node's backlog doesn't hold up another's.
    main.py only asks for the next chunk when nothing else is pending, so
    scheduled polls slot in between chunks.

    A chunk's lines are buffered and only stored once its "se" (sync end)
    packet arrives. If fewer lines arrived than the node says it sent, the
    same chunk is requested again on that node's next turn (up to
    max_retries) — the node only moves its cursor forward once the next
    request confirms the previous offset.

    Newer node firmware tags each line with its place in the chunk ("j"), so
    a retry only asks for the lines still missing: the request's "j" is a
    bitmask of the wanted lines (bit i = line i), which keeps it small enough
    for one radio packet. Untagged lines from older firmware are handled the
    old way, by asking for the whole chunk again. The confirmed cursor is kept per
    node across sessions, so the next session carries on without resending
    the last chunk (only a Pi restart loses it, which costs at most one
    duplicated chunk).

    Synced lines go to sensors.db and a sync archive file, never DATA_FILE.
    '''
    def __init__(self, node_ids, lines_per_session=2000, chunk_lines=20,
                 max_retries=3, db_conn=None):
        self.node_ids          = list(node_ids)
        self.lines_per_session = lines_per_session
        self.chunk_lines       = chunk_lines
        self.max_retries       = max_retries
        self.db_conn           = db_conn

        self.active   = None    # node ID of the chunk in flight (or last sent); None once its session ends
        self.awaiting = False   # a chunk has been requested and its "se" hasn't arrived
        self.sessions = {}      # node ID -> {"lines", "retries"} for each open session, in start order
        self._last_hour = {}    # node ID -> "YYYY-MM-DDTHH" of its last session
        self._requested = []    # node IDs with a manual "sync now" request
        self._cursors   = {}    # node ID -> {"g": gen, "o": offset} last stored
        self._buffer    = []

    def request_now(self, node_id):
        '''Start a session for node_id as soon as the radio is free, regardless of the hour.'''
        if node_id not in self._requested:
            self._requested.append(node_id)

    def next_command(self, reachable=lambda node_id: True):
        '''Return the next "sync" command to send, or None if there's nothing to do.
        Sessions only start or continue for nodes reachable() says are answering.'''
        self._start_sessions(reachable)
        node_id = self._next_turn(reachable)
        if node_id is None:
            return None
        self.active = node_id
        command = {"t": "sync", "n": node_id, "k": self.chunk_lines}
        command.update(self._cursors.get(node_id, {}))
        session = self.sessions[node_id]
        if session["chunk"] != self._chunk_id(command):
            session.update(chunk=self._chunk_id(command), have={}, count=None)
        self.awaiting = True
        return command

    @staticmethod
    def _chunk_id(command):
        '''Which chunk a request is for: same cursor and size means the same lines.'''
        return (command.get("g"), command.get("o"), command.get("k"))

    def _start_sessions(self, reachable):
        hour = datetime.now().strftime("%Y-%m-%dT%H")
        for node_id in list(self._requested):
            if node_id not in self.sessions and reachable(node_id):
                self._requested.remove(node_id)
                self._begin(node_id, hour)
        for node_id in self.node_ids:
            if (node_id not in self.sessions and self._last_hour.get(node_id) != hour
                    and reachable(node_id)):
                self._begin(node_id, hour)

    def _begin(self, node_id, hour):
        self._last_hour[node_id] = hour
        # have: tagged lines received so far for the chunk in progress, by
        # place in the chunk; count: its line count, once an "se" has said.
        self.sessions[node_id] = {"lines": 0, "retries": 0,
                                  "chunk": None, "have": {}, "count": None}
        print(f"[SYNC] Session started for node {node_id} (up to {self.lines_per_session} lines).")

    def _next_turn(self, reachable):
        '''The open session after the one served last, skipping (and ending)
        sessions for nodes that have stopped being available.'''
        order = list(self.sessions)
        if self.active in order:
            i = order.index(self.active) + 1
            order = order[i:] + order[:i]
        for node_id in order:
            if reachable(node_id):
                return node_id
            self._end_session(node_id, "node no longer available")
        return None

    def on_send(self, command):
        '''
        CommandManager hook, called before each (re)send of a request. Untagged
        lines start over with every send. If tagged lines of this chunk have
        already arrived, ask only for the rest: after a short chunk, or when
        the "se" was lost and CommandManager is resending.
        '''
        if command.get("t") != "sync":
            return
        self._buffer = []
        command.pop("j", None)
        session = self.sessions.get(command.get("n"))
        if (session is None or not session["have"]
                or session["chunk"] != self._chunk_id(command)):
            return
        total = session["count"] if session["count"] is not None else command.get("k", 0)
        if total > 32:
            return   # too many lines for the node's 32-bit mask — ask for them all
        command["j"] = sum(1 << i for i in range(total) if i not in session["have"])

    def collect(self, line):
        place = line.pop("j", None)
        session = self.sessions.get(self.active)
        if isinstance(place, int) and session is not None:
            session["have"][place] = line
        else:
            self._buffer.append(line)

    def handle_end(self, se):
        '''Process a chunk's "se" packet: store the chunk, or re-request it if lines went missing.'''
        if not self.awaiting:
            return
        self.awaiting = False
        node_id = self.active
        session = self.sessions.get(node_id)
        if session is None:
            return
        count  = se.get("c", 0)
        tagged = {i: line for i, line in session["have"].items() if i < count}
        lines  = [tagged[i] for i in sorted(tagged)] if tagged else self._buffer
        got    = len(lines)

        if got < count and session["retries"] < self.max_retries:
            session["retries"] += 1
            session["count"] = count
            what = f"the {count - got} missing" if tagged else "the whole chunk"
            print(f"[SYNC] Node {node_id}: got {got}/{count} lines — re-requesting {what} "
                  f"(retry {session['retries']}/{self.max_retries}).")
            self._buffer = []
            return
        if got < count:
            print(f"[SYNC] Node {node_id}: storing {got}/{count} lines after "
                  f"{self.max_retries} retries — {count - got} lost.")

        self._store(node_id, lines)
        self._buffer = []
        session.update(retries=0, chunk=None, have={}, count=None)
        self._cursors[node_id] = {"g": se.get("g"), "o": se.get("o")}
        session["lines"] += count

        if not se.get("m") or count == 0:
            self._end_session(node_id, "caught up")
        elif session["lines"] >= self.lines_per_session:
            self._end_session(node_id, "hourly limit reached, more waiting")

    def abort(self):
        '''The chunk request timed out — that node's session waits for the next hour.'''
        if self.active in self.sessions:
            self._end_session(self.active, "node stopped responding")

    def _end_session(self, node_id, reason):
        session = self.sessions.pop(node_id)
        print(f"[SYNC] Session for node {node_id} ended after "
              f"{session['lines']} lines ({reason}).")
        if node_id == self.active:
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
