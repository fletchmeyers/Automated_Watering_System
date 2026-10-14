'''
Python 3 running on Raspberry Pi 3B

The garden-wifi service: talks to nodes that are on Wi-Fi instead of the
radio (nodes.json "link": "wifi"; see arduino/src/wifi_link.h).

Each Wi-Fi node opens a TCP connection to this service when it starts and
says hello ({"t":"hello","n":<id>}). From then on the Pi is in charge, as
with the radio: every POLL_INTERVAL it sends the node a poll, and the node
answers with the same packets it would send over the radio — a "ts" header,
one packet per sensor, then batch_end — one JSON object per line. Readings
are stored exactly as main.py stores radio ones (BatchReceiver: sensors.db,
the day's archive file, data_from_pico.txt).

It runs separately from main.py's radio loop, so neither can hold the other
up. nodes.json is read again whenever it changes, so a newly added Wi-Fi
node, or a changed link preference, needs no restart.

A node with the radio too ("link": ["radio", "wifi"] or the other way
round) is polled here only while Wi-Fi is the link in use (links.py: the
first in its list that's up); otherwise it's just pinged each minute, so
its connection is known to be alive if the radio fails.

A node with a sleep_window is put to sleep from here while Wi-Fi is the
link in use (the same SleepScheduler as main.py's), and recorded in
node_info.json as asleep, so neither service polls it until it wakes. It
leaves Wi-Fi while it sleeps and reconnects when it wakes.

The dashboard's Refresh and ping test reach Wi-Fi nodes through a control
port only open to the Pi itself (CONTROL_PORT on 127.0.0.1): flask_api.py
sends one request line ({"t": "poll" or "ping", "n": <id>}) with
ask_wifi_service() and gets one reply line back.

Written by Fletcher Meyers
October 2026
'''

import asyncio
import json
import socket
import time
from datetime import datetime
from pathlib import Path

import db
from communication_indoor import BatchReceiver, ISO_FORMAT, SleepScheduler
from links import NodeList, RADIO_STATUS_FILE, WIFI_STATUS_FILE, active_link
from nodes import NODES_FILE, link_plan, links_of, sleep_windows, wifi_node_ids
from sync_indoor import (
    DATA_FILE, PING_PROGRESS_FILE, poll_progress, poll_progress_answered, poll_progress_failed,
    poll_progress_reading, poll_progress_sent, recorded_wake, update_node_info,
)

WIFI_PORT     = 5006   # also sent to each node by node_setup.py
POLL_INTERVAL = 60     # seconds between polls, the same as radio nodes
HELLO_TIMEOUT = 10     # seconds for a new connection to say which node it is
REPLY_TIMEOUT = 15     # seconds for a poll's batch_end to arrive
MAX_MISSES    = 3      # unanswered polls in a row before dropping the connection
CONTROL_PORT  = 5007   # on 127.0.0.1 only: on-demand polls and pings from flask_api.py
PING_TIMEOUT  = 2      # seconds to wait for each pong

# Replies that aren't readings. One can turn up during a poll if it came
# too late for what it answered (a pong after its ping timed out), and must
# not be stored as if it were a sensor.
NOT_READINGS = {"hello", "pong", "sleep_ack", "wifi_config_ack", "info", "set_interval_ack",
                "se", "data_ack"}

# Which Wi-Fi nodes are connected right now, for node_setup.py, main.py and
# the dashboard (links.py): {"<node>": {"ip", "since", "last_seen",
# "last_reading"}}; last_seen is the last time it answered anything.
STATUS_FILE = WIFI_STATUS_FILE


def _now():
    return datetime.now().strftime(ISO_FORMAT)


class Link:
    '''One node's open connection. Anything that talks to the node holds
    the lock, so the minute-by-minute poll and a dashboard request never
    read each other's replies.'''
    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer
        self.lock = asyncio.Lock()
        self.last_packets = []   # what the last poll brought back


class WifiNodes:
    def __init__(self, db_conn, nodes_file=NODES_FILE, poll_interval=POLL_INTERVAL,
                 reply_timeout=REPLY_TIMEOUT, status_file=STATUS_FILE, data_file=DATA_FILE,
                 store=True, radio_status_file=RADIO_STATUS_FILE):
        self.db_conn = db_conn
        self.store = store   # False: print each reading instead of saving it (bench tests)
        self.nodes_file = nodes_file
        self.node_list = NodeList(nodes_file)
        self.radio_status_file = radio_status_file
        self.poll_interval = poll_interval
        self.reply_timeout = reply_timeout
        self.status_file = Path(status_file)
        self.data_file = data_file
        self.links = {}      # node ID -> its Link
        self.status = {}
        self.sleeper = SleepScheduler({})   # windows refreshed from the node list as it changes
        self._polled = {}    # node ID -> True if Wi-Fi is the link in use (polled), False if only pinged
        self._empty = {}     # node ID -> True while its polls come back with nothing to report

    def _write_status(self):
        try:
            self.status_file.write_text(json.dumps(self.status, indent=2))
        except OSError as e:
            print(f"[WIFI] Could not write {self.status_file}: {e}")

    def _known_wifi_nodes(self):
        return set(wifi_node_ids(self.node_list.get()))

    def _seen(self, node_id, reading=False):
        entry = self.status.get(str(node_id))
        if entry is not None:
            entry["last_seen"] = _now()
            if reading:
                entry["last_reading"] = entry["last_seen"]
            self._write_status()

    def _wifi_in_use(self, node_id):
        '''Whether the minute's poll goes over Wi-Fi (not the radio).'''
        node = self.node_list.get().get(node_id)
        return node is None or active_link(node_id, node, wifi_file=self.status_file,
                                           radio_file=self.radio_status_file) == "wifi"

    @staticmethod
    async def _send(writer, command):
        writer.write((json.dumps(command, separators=(",", ":")) + "\n").encode())
        await writer.drain()

    async def handle(self, reader, writer):
        '''One node's connection, from hello until it drops.'''
        peer = writer.get_extra_info("peername")
        ip = peer[0] if peer else "?"
        try:
            line = await asyncio.wait_for(reader.readline(), HELLO_TIMEOUT)
            hello = json.loads(line)
        except (asyncio.TimeoutError, ValueError, ConnectionError, OSError):
            print(f"[WIFI] {ip} connected but didn't say hello; closing.")
            writer.close()
            return
        node_id = hello.get("n") if isinstance(hello, dict) and hello.get("t") == "hello" else None
        if node_id not in self._known_wifi_nodes():
            print(f"[WIFI] {ip} says it's node {node_id}, which isn't a Wi-Fi node in "
                  f"{Path(self.nodes_file).name}; closing.")
            writer.close()
            return

        old = self.links.get(node_id)
        if old is not None:
            old.writer.close()           # the node reconnected; drop the stale link
        link = self.links[node_id] = Link(reader, writer)
        self.status[str(node_id)] = {"ip": ip, "since": _now(), "last_seen": _now(),
                                     "last_reading": None}
        self._write_status()
        print(f"[WIFI] Node {node_id} connected from {ip}.")

        batch = BatchReceiver(self.data_file, db_conn=self.db_conn)
        misses = 0
        try:
            while not writer.is_closing():
                if self._asleep(node_id):
                    # Just woken (within the grace time), or about to drop the
                    # connection as it goes to sleep: leave it be, but notice
                    # when it hangs up.
                    try:
                        if not await asyncio.wait_for(reader.readline(), self.poll_interval):
                            break
                    except asyncio.TimeoutError:
                        pass
                    continue
                async with link.lock:
                    if self._wifi_in_use(node_id) and await self._maybe_sleep(node_id, link):
                        continue
                    self._say_role(node_id)
                    if self._wifi_in_use(node_id):
                        what = "a poll"
                        answered = await self.poll(node_id, reader, writer, batch)
                    else:   # the radio is in use: just check the connection
                        what = "a ping"
                        answered = (await self.ping(node_id, count=1, quiet=True))["hits"] == 1
                if answered:
                    misses = 0
                else:
                    misses += 1
                    print(f"[WIFI] Node {node_id} didn't answer {what} ({misses}/{MAX_MISSES}).")
                    if misses >= MAX_MISSES:
                        break
                await asyncio.sleep(self.poll_interval)
        except (ConnectionError, OSError) as e:
            if not self._asleep(node_id):
                print(f"[WIFI] Node {node_id}'s connection dropped: {e}")
        finally:
            self._polled.pop(node_id, None)
            if self.links.get(node_id) is link:
                del self.links[node_id]
                self.status.pop(str(node_id), None)
                self._write_status()
                until = self.sleeper.asleep_until(node_id)
                print(f"[WIFI] Node {node_id} disconnected"
                      + (f" (asleep until {until.strftime(ISO_FORMAT)})." if until else "."))
            writer.close()

    def _say_role(self, node_id):
        '''Say (once, and again whenever it changes) whether a node with the
        radio too is polled over Wi-Fi or only pinged while the radio's in use.'''
        node = self.node_list.get().get(node_id, {})
        if len(links_of(node)) < 2:
            return
        polled = self._wifi_in_use(node_id)
        if self._polled.get(node_id) != polled:
            self._polled[node_id] = polled
            print(f"[WIFI] Node {node_id}: " + (
                "Wi-Fi is the link in use; polling it over Wi-Fi every minute." if polled else
                "the radio is the link in use; pinging it over Wi-Fi each minute to keep the backup ready."))

    def _asleep(self, node_id):
        return self.sleeper.asleep(node_id) or recorded_wake(node_id) is not None

    async def _maybe_sleep(self, node_id, link):
        '''Send a sleep command if the node is due one; True if it went to sleep.'''
        self.sleeper.windows = sleep_windows(self.node_list.get())
        command = self.sleeper.next_command(lambda n: n == node_id)
        if command is None:
            return False
        await self._send(link.writer, command)
        ack = await self._reply(link, "sleep_ack", self.reply_timeout)
        if ack is None:
            self.sleeper.timed_out(command)
        else:
            self.sleeper.handle_ack(ack)
        until = self.sleeper.asleep_until(node_id)
        update_node_info(node_id, sleep_until=until.strftime(ISO_FORMAT) if until else None)
        return until is not None

    @staticmethod
    async def _reply(link, kind, timeout):
        '''The next packet of type kind from the node, or None after timeout.'''
        deadline = time.monotonic() + timeout
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                line = await asyncio.wait_for(link.reader.readline(), remaining)
            except asyncio.TimeoutError:
                return None
            if not line:
                raise ConnectionError("the node closed the connection")
            try:
                packet = json.loads(line)
            except ValueError:
                continue
            if isinstance(packet, dict) and packet.get("t") == kind:
                return packet
        return None

    def _poll_command(self, node_id):
        command = {"t": "poll", "ts": _now(), "n": node_id}
        plan = link_plan(self.node_list.get().get(node_id, {}))
        if plan:
            command["lp"] = plan   # how the node uses its two links (link_power.h)
        return command

    async def poll(self, node_id, reader, writer, batch, progress=False):
        '''Ask for the latest reading; True once its batch_end has arrived and
        it's stored. progress: it's the dashboard's Refresh, which shows each
        step (sync_indoor.poll_progress).'''
        await self._send(writer, self._poll_command(node_id))
        if progress:
            poll_progress_sent(node_id, "wifi")
        link = self.links.get(node_id)
        got = []
        deadline = time.monotonic() + self.reply_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if progress:
                    poll_progress_failed(node_id, f"no answer over Wi-Fi in {self.reply_timeout} s")
                return False
            try:
                line = await asyncio.wait_for(reader.readline(), remaining)
            except asyncio.TimeoutError:
                if progress:
                    poll_progress_failed(node_id, f"no answer over Wi-Fi in {self.reply_timeout} s")
                return False
            if not line:
                raise ConnectionError("the node closed the connection")
            try:
                packet = json.loads(line)
            except ValueError:
                continue
            if not isinstance(packet, dict):
                continue
            kind = packet.get("t")
            if kind in NOT_READINGS:
                continue   # e.g. a late pong: not part of this poll's answer
            if not self.store:
                print(f"[WIFI] Node {node_id}: {json.dumps(packet)}")
                if kind == "batch_end":
                    self._seen(node_id, reading=True)
                    if link is not None:
                        link.last_packets = got
                    return True
                if "q" in packet and kind != "ts":
                    got.append(packet)
                continue
            if kind == "ts":
                batch.open_batch(packet.get("v"))
                if progress:
                    poll_progress(node_id, "heard")
            elif kind == "batch_end":
                batch.close_batch(packet)
                written = batch.flush(radio=None)   # no radio: nothing to ack over the air
                if progress:
                    poll_progress_answered(node_id, written)
                self._seen(node_id, reading=bool(written))
                if self._empty.get(node_id, False) != (not written):
                    # Say so when a node starts (or stops) answering with no
                    # readings: otherwise an empty answer logs nothing at all.
                    self._empty[node_id] = not written
                    print(f"[WIFI] Node {node_id} " + (
                        "answers its polls with nothing to report (no sensors answering?)."
                        if not written else "is sending readings again."))
                if link is not None:
                    link.last_packets = written or []
                return True
            elif "q" in packet:
                batch.collect(packet)
                if progress:
                    poll_progress_reading(node_id)

    async def ping(self, node_id, count=10, timeout=PING_TIMEOUT, progress_file=PING_PROGRESS_FILE,
                   quiet=False):
        '''count pings over the node's connection, each waiting up to timeout
        for its pong: the same result as main.py's radio ping test. quiet:
        a connection check, not a test (no progress file, nothing printed).'''
        link = self.links[node_id]
        results = []
        if progress_file and not quiet:
            Path(progress_file).unlink(missing_ok=True)   # not the last test's count
        for q in range(count):
            sent_at = time.monotonic()
            await self._send(link.writer, {"t": "ping", "q": q, "n": node_id})
            rtt_ms = None
            deadline = sent_at + timeout
            while (remaining := deadline - time.monotonic()) > 0:
                try:
                    line = await asyncio.wait_for(link.reader.readline(), remaining)
                except asyncio.TimeoutError:
                    break
                if not line:
                    raise ConnectionError("the node closed the connection")
                try:
                    packet = json.loads(line)
                except ValueError:
                    continue
                if isinstance(packet, dict) and packet.get("t") == "pong" and packet.get("pq") == q:
                    rtt_ms = round((time.monotonic() - sent_at) * 1000, 1)
                    self._seen(node_id)
                    break
            results.append({"q": q, "ok": rtt_ms is not None, "rtt_ms": rtt_ms})
            if progress_file and not quiet:
                try:
                    Path(progress_file).write_text(json.dumps(
                        {"done": q + 1, "count": count, "hits": sum(r["ok"] for r in results)}))
                except OSError:
                    pass
        rtts = [r["rtt_ms"] for r in results if r["ok"]]
        if not quiet:
            print(f"[WIFI] Ping test for node {node_id}: {len(rtts)}/{count} pongs.")
        return {"count": count, "hits": len(rtts), "misses": count - len(rtts),
                "avg_rtt_ms": round(sum(rtts) / len(rtts), 1) if rtts else None,
                "results": results, "node_id": node_id}

    async def run_request(self, request):
        '''A request from flask_api.py: {"t": "poll" or "ping", "n": node}.'''
        node_id = request.get("n")
        link = self.links.get(node_id)
        if link is None:
            return {"status": "offline"}
        try:
            async with link.lock:
                if request.get("t") == "poll":
                    batch = BatchReceiver(self.data_file, db_conn=self.db_conn)
                    if await self.poll(node_id, link.reader, link.writer, batch, progress=True):
                        return {"status": "ok", "packets": link.last_packets}
                    return {"status": "timeout", "packets": []}
                if request.get("t") == "ping":
                    count = max(1, min(int(request.get("count", 10)), 50))
                    return {"status": "ok", **await self.ping(node_id, count)}
        except (ConnectionError, OSError):
            if request.get("t") == "poll":
                poll_progress_failed(node_id, "the Wi-Fi connection dropped")
            return {"status": "offline"}
        return {"status": "error", "error": f"unknown request {request.get('t')!r}"}

    async def control(self, reader, writer):
        '''One request on the control port, one reply, then close.'''
        try:
            request = json.loads(await asyncio.wait_for(reader.readline(), 5))
            reply = await self.run_request(request if isinstance(request, dict) else {})
        except (asyncio.TimeoutError, ValueError) as e:
            reply = {"status": "error", "error": str(e)}
        try:
            writer.write((json.dumps(reply) + "\n").encode())
            await writer.drain()
        finally:
            writer.close()


def ask_wifi_service(request, timeout=60, port=CONTROL_PORT):
    '''For flask_api.py (not async): send garden-wifi one request and return
    its reply, or {"status": "unavailable"} if the service isn't running.'''
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
            s.sendall((json.dumps(request) + "\n").encode())
            reply = s.makefile("rb").readline()
        return json.loads(reply)
    except (OSError, ValueError):
        return {"status": "unavailable"}


async def serve(port=WIFI_PORT, nodes_file=NODES_FILE, store=True, control_port=CONTROL_PORT):
    db_conn = None
    if store:
        db_conn = db.get_connection()
        db_conn.execute("PRAGMA busy_timeout = 30000")   # main.py writes too
    nodes = WifiNodes(db_conn, nodes_file=nodes_file, store=store)
    nodes._write_status()
    server = await asyncio.start_server(nodes.handle, host="0.0.0.0", port=port)
    control = await asyncio.start_server(nodes.control, host="127.0.0.1", port=control_port)
    print(f"[WIFI] Waiting for Wi-Fi nodes from {Path(nodes_file).name} on port {port}"
          + ("" if store else " (not saving readings)") + ".")
    async with server, control:
        await asyncio.gather(server.serve_forever(), control.serve_forever())


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Poll the nodes that are on Wi-Fi.")
    parser.add_argument("--nodes", default=str(NODES_FILE), metavar="FILE",
                        help="node list to accept (default: the Pi's own, raspberrypi/nodes.json)")
    parser.add_argument("--port", type=int, default=WIFI_PORT)
    parser.add_argument("--no-store", action="store_true",
                        help="print readings instead of saving them (bench tests)")
    args = parser.parse_args()
    asyncio.run(serve(args.port, args.nodes, store=not args.no_store))
