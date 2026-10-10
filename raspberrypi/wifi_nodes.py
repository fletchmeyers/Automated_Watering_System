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
up. nodes.json is read again for every new connection, so a newly added
Wi-Fi node needs no restart.

Written by Fletcher Meyers
October 2026
'''

import asyncio
import json
import time
from datetime import datetime
from pathlib import Path

import db
from communication_indoor import BatchReceiver, ISO_FORMAT
from nodes import NODES_FILE, load_nodes, wifi_node_ids
from sync_indoor import DATA_FILE

WIFI_PORT     = 5006   # also sent to each node by node_setup.py
POLL_INTERVAL = 60     # seconds between polls, the same as radio nodes
HELLO_TIMEOUT = 10     # seconds for a new connection to say which node it is
REPLY_TIMEOUT = 15     # seconds for a poll's batch_end to arrive
MAX_MISSES    = 3      # unanswered polls in a row before dropping the connection

# Which Wi-Fi nodes are connected right now, for node_setup.py (and later the
# dashboard): {"<node>": {"ip", "since", "last_reading"}}.
STATUS_FILE = "/tmp/garden_wifi_nodes.json"


def _now():
    return datetime.now().strftime(ISO_FORMAT)


class WifiNodes:
    def __init__(self, db_conn, nodes_file=NODES_FILE, poll_interval=POLL_INTERVAL,
                 reply_timeout=REPLY_TIMEOUT, status_file=STATUS_FILE, data_file=DATA_FILE,
                 store=True):
        self.db_conn = db_conn
        self.store = store   # False: print each reading instead of saving it (bench tests)
        self.nodes_file = nodes_file
        self.poll_interval = poll_interval
        self.reply_timeout = reply_timeout
        self.status_file = Path(status_file)
        self.data_file = data_file
        self.links = {}      # node ID -> its connection's writer
        self.status = {}

    def _write_status(self):
        try:
            self.status_file.write_text(json.dumps(self.status, indent=2))
        except OSError as e:
            print(f"[WIFI] Could not write {self.status_file}: {e}")

    def _known_wifi_nodes(self):
        try:
            return set(wifi_node_ids(load_nodes(self.nodes_file, skip_invalid=True)))
        except Exception as e:
            print(f"[WIFI] Could not read the node list: {e}")
            return set()

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
            old.close()                  # the node reconnected; drop the stale link
        self.links[node_id] = writer
        self.status[str(node_id)] = {"ip": ip, "since": _now(), "last_reading": None}
        self._write_status()
        print(f"[WIFI] Node {node_id} connected from {ip}.")

        batch = BatchReceiver(self.data_file, db_conn=self.db_conn)
        misses = 0
        try:
            while not writer.is_closing():
                if await self.poll(node_id, reader, writer, batch):
                    misses = 0
                else:
                    misses += 1
                    print(f"[WIFI] Node {node_id} didn't answer a poll ({misses}/{MAX_MISSES}).")
                    if misses >= MAX_MISSES:
                        break
                await asyncio.sleep(self.poll_interval)
        except (ConnectionError, OSError) as e:
            print(f"[WIFI] Node {node_id}'s connection dropped: {e}")
        finally:
            if self.links.get(node_id) is writer:
                del self.links[node_id]
                self.status.pop(str(node_id), None)
                self._write_status()
                print(f"[WIFI] Node {node_id} disconnected.")
            writer.close()

    async def poll(self, node_id, reader, writer, batch):
        '''Ask for the latest reading; True once its batch_end has arrived and it's stored.'''
        await self._send(writer, {"t": "poll", "ts": _now(), "n": node_id})
        deadline = time.monotonic() + self.reply_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                line = await asyncio.wait_for(reader.readline(), remaining)
            except asyncio.TimeoutError:
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
            if not self.store:
                print(f"[WIFI] Node {node_id}: {json.dumps(packet)}")
                if kind == "batch_end":
                    self.status[str(node_id)]["last_reading"] = _now()
                    self._write_status()
                    return True
                continue
            if kind == "ts":
                batch.open_batch(packet.get("v"))
            elif kind == "batch_end":
                batch.close_batch(packet)
                written = batch.flush(radio=None)   # no radio: nothing to ack over the air
                if written:
                    self.status[str(node_id)]["last_reading"] = _now()
                    self._write_status()
                return True
            elif kind == "hello":
                continue
            elif "q" in packet:
                batch.collect(packet)


async def serve(port=WIFI_PORT, nodes_file=NODES_FILE, store=True):
    db_conn = None
    if store:
        db_conn = db.get_connection()
        db_conn.execute("PRAGMA busy_timeout = 30000")   # main.py writes too
    nodes = WifiNodes(db_conn, nodes_file=nodes_file, store=store)
    nodes._write_status()
    server = await asyncio.start_server(nodes.handle, host="0.0.0.0", port=port)
    print(f"[WIFI] Waiting for Wi-Fi nodes from {Path(nodes_file).name} on port {port}"
          + ("" if store else " (not saving readings)") + ".")
    async with server:
        await server.serve_forever()


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
