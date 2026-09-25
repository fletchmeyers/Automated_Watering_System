'''
Python 3 running on Raspberry Pi 3B

Main loop: poll nodes on a timer, receive sensor bursts, write data to file.
Logged SD data is pulled from nodes in hourly sync sessions between polls.

Written by Fletcher Meyers
March 2026
'''

import json
from pathlib import Path

from hardware_setup_indoor import rfm69, GLED, YLED, RLED, blink_led
from sync_indoor import (
    DATA_FILE, COMMAND_FILE, request_poll, request_sync_chunk,
    POLL_RESULT_FILE, PING_REQUEST_FILE, PING_RESULT_FILE, SYNC_REQUEST_FILE,
)

from communication_indoor import (
    CommandManager, BatchReceiver, PollingTimer, FragmentReassembler, SyncManager,
    run_ping_test,
)

import db

# ── Config ────────────────────────────────────────────────────────────────────
NODE_IDS      = [1, 2] # add node IDs here as you expand the network
POLL_INTERVAL = 60     # seconds between polls per node

SYNC_NODE_IDS       = [1]    # nodes with logged data to pull (the M0 has no storage yet)
SYNC_LINES_PER_HOUR = 2000   # cap per hourly session — a backlog drains over several hours
SYNC_CHUNK_LINES    = 20     # lines per chunk request; keep a chunk well under the 10s retry

# ── Startup ───────────────────────────────────────────────────────────────────
stale = Path(COMMAND_FILE)
if stale.exists():
    print(f"[STARTUP] Clearing stale command file: {stale.read_text()}")
    stale.unlink()

stale_ping = Path(PING_REQUEST_FILE)
if stale_ping.exists():
    print(f"[STARTUP] Clearing stale ping request file: {stale_ping.read_text()}")
    stale_ping.unlink()

print(f"Temperature: {rfm69.temperature}C")
print(f"Frequency: {rfm69.frequency_mhz}mhz")
print(f"Bit rate: {rfm69.bitrate / 1000}kbit/s")
print(f"Frequency deviation: {rfm69.frequency_deviation}hz")

db_conn = db.get_connection()

sync  = SyncManager(SYNC_NODE_IDS, lines_per_session=SYNC_LINES_PER_HOUR,
                    chunk_lines=SYNC_CHUNK_LINES, db_conn=db_conn)
cmd   = CommandManager(on_send=sync.on_send)
batch = BatchReceiver(DATA_FILE, db_conn=db_conn)
timer = PollingTimer(NODE_IDS, poll_interval=POLL_INTERVAL)
frags = FragmentReassembler()

# ── Main loop ─────────────────────────────────────────────────────────────────
while True:

    # ── A sync chunk that timed out ends that node's session for the hour ──
    if cmd.timed_out is not None:
        if cmd.timed_out.get("t") == "sync":
            sync.abort()
        cmd.timed_out = None

    # ── Manual "sync now" request (sync_indoor.py sync / API) ─────────────
    sync_req = Path(SYNC_REQUEST_FILE)
    if sync_req.exists():
        try:
            sync.request_now(json.loads(sync_req.read_text()).get("n", 1))
        except Exception as e:
            print(f"[SYNC] Could not parse sync request: {e}")
        sync_req.unlink(missing_ok=True)

    # ── Issue the next poll, or the next sync chunk if no poll is due ─────
    # Only issue a new command if nothing is currently outstanding, and
    # nothing (e.g. a manual poll from the Flask API) is sitting in
    # COMMAND_FILE waiting to be forwarded. Without this guard,
    # request_poll()/request_sync_chunk() will happily overwrite
    # COMMAND_FILE even while CommandManager is still waiting on an ack for
    # a previous command (automatic OR a manual poll from the Flask API).
    # That overwrite is silent and the Pico never hears about the command it
    # replaced. This was the root cause of polls occasionally vanishing:
    # POLL_INTERVAL and CMD_TIMEOUT are both 60s, so a freshly-due poll and
    # a timeout-cleanup for the previous poll could land in the same loop
    # iteration, and the new command would get wiped out one line after
    # being written — deliberately skipping affected nodes here (rather than
    # calling mark_polled) means they stay "due" and get retried on the next
    # iteration once cmd.pending clears, instead of waiting a full
    # POLL_INTERVAL again. Only one node is polled per iteration for the
    # same reason — a second request_poll() would overwrite the first.
    # Polls come first, so a long sync session never starves them.
    if cmd.pending is None and not Path(COMMAND_FILE).exists():
        due = timer.due_nodes()
        if due:
            request_poll(due[0])
            timer.mark_polled(due[0])
        else:
            chunk_request = sync.next_command()
            if chunk_request is not None:
                request_sync_chunk(chunk_request)

    # ── Run a ping test if one's been requested and nothing else is busy ──
    # Same cmd.pending is None guard as above, for the same reason: a ping
    # burst blocks this loop for up to a couple seconds, so it must not
    # start while CommandManager is mid-flight on something else.
    ping_req = Path(PING_REQUEST_FILE)
    if cmd.pending is None and ping_req.exists():
        try:
            req = json.loads(ping_req.read_text())
        except Exception as e:
            print(f"[PING] Could not parse ping request: {e}")
            req = {}
        node_id = req.get("n", 1)
        count   = req.get("count", 10)
        print(f"[PING] Running ping test: node={node_id}, count={count}")
        try:
            result = run_ping_test(rfm69, node_id=node_id, count=count)
        except Exception as e:
            print(f"[PING] Test failed with exception: {e}")
            blink_led(RLED, times=3)
            result = {
                "count": count, "hits": 0, "misses": count,
                "avg_rtt_ms": None, "results": [], "error": str(e),
            }
        result["node_id"] = node_id   # ← new — so a stale/leftover result can be identified
        Path(PING_RESULT_FILE).write_text(json.dumps(result))
        ping_req.unlink(missing_ok=True)

    # ── Forward any pending command to the Pico ───────────────────────────
    timeout = 6.0 if cmd.pending else 1.0
    cmd.check_and_forward(rfm69)

    # ── Listen for one packet ─────────────────────────────────────────────
    packet = rfm69.receive(with_header=True, timeout=timeout)

    if packet is None:
        continue

    try:
        payload = packet[4:]
        if payload[:1] == b"~":
            # One fragment of a packet too big for a single radio send —
            # wait for the rest before handling it.
            payload = frags.feed(payload)
            if payload is None:
                continue

        data     = json.loads(payload.decode("utf-8"))
        pkt_type = data.get("t")

        if pkt_type == "ts":
            batch.open_batch(data.get("v"))

        elif pkt_type == "err":
            print(f"[ERROR] Node {data.get('n')} could not send packet "
                  f"q={data.get('q')} ({data.get('sz')} bytes — over radio limit).")
            blink_led(RLED, times=1)

        elif pkt_type == "batch_end":
            # Capture this before handle_ack() clears cmd.pending — we need
            # to know whether the completed batch was a poll response before
            # that state disappears.
            was_poll = (cmd.pending is not None and cmd.pending.get("t") == "poll")

            batch.close_batch(data)
            written = batch.flush(rfm69)
            cmd.handle_ack(data)
            cmd.check_and_forward(rfm69)

            if was_poll and written:
                try:
                    Path(POLL_RESULT_FILE).write_text(json.dumps(written))
                except Exception as e:
                    print(f"[POLL] Could not write poll result: {e}")

        elif pkt_type == "se":
            sync.handle_end(data)
            cmd.handle_ack(data)

        elif cmd.handle_ack(data):
            blink_led(YLED, times=2)

        elif sync.awaiting and "q" not in data:
            # Logged SD lines are sent as-is — no sequence number, unlike
            # every live packet — so that's what marks them as sync data.
            sync.collect(data)

        else:
            complete = batch.collect(data)
            if complete:
                batch.flush(rfm69)
                cmd.check_and_forward(rfm69)

        blink_led(GLED, times=1)

    except Exception as e:
        print("Bad packet:", e)
        blink_led(RLED, times=2)