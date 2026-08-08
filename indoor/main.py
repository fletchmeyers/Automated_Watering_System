'''
Python 3 running on Raspberry Pi 3B

Main loop: poll nodes on a timer, receive sensor bursts, write data to file.
Bulk SD sync is requested automatically on a longer interval.

Written by Fletcher Meyers
March 2026
'''

import json
from pathlib import Path

from hardware_setup_indoor import rfm69, GLED, YLED, RLED, blink_led
from sync_indoor import (
    DATA_FILE, COMMAND_FILE, request_poll, request_bulk_sync,
    POLL_RESULT_FILE, PING_REQUEST_FILE, PING_RESULT_FILE,
)

from communication_indoor import CommandManager, BatchReceiver, PollingTimer, run_ping_test

# ── Config ────────────────────────────────────────────────────────────────────
NODE_IDS      = [1]    # add node IDs here as you expand the network
POLL_INTERVAL = 60     # seconds between polls per node
SYNC_INTERVAL = 3600   # seconds between automatic bulk SD syncs per node (0 = disabled)

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

cmd   = CommandManager()
batch = BatchReceiver(DATA_FILE)
timer = PollingTimer(NODE_IDS, poll_interval=POLL_INTERVAL, sync_interval=SYNC_INTERVAL)

# ── Main loop ─────────────────────────────────────────────────────────────────
while True:

    # ── Issue polls for any nodes whose timer has elapsed ─────────────────
    # Only issue a new poll/sync if nothing is currently outstanding. Without
    # this guard, request_poll()/request_bulk_sync() will happily overwrite
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
    # POLL_INTERVAL again.
    if cmd.pending is None:
        for node_id in timer.due_nodes():
            if timer.sync_due(node_id):
                request_bulk_sync(node_id)
                timer.mark_synced(node_id)
            else:
                request_poll(node_id)
            timer.mark_polled(node_id)

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
        result = run_ping_test(rfm69, node_id=node_id, count=count)
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
        payload  = packet[4:].decode("utf-8")
        data     = json.loads(payload)
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

        elif cmd.handle_ack(data):
            blink_led(YLED, times=2)

        else:
            complete = batch.collect(data)
            if complete:
                batch.flush(rfm69)
                cmd.check_and_forward(rfm69)

        blink_led(GLED, times=1)

    except Exception as e:
        print("Bad packet:", e)
        blink_led(RLED, times=2)