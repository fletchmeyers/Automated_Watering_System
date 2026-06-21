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
from sync_indoor import DATA_FILE, COMMAND_FILE, request_poll, request_bulk_sync

from communication_indoor import CommandManager, BatchReceiver, PollingTimer

# ── Config ────────────────────────────────────────────────────────────────────
NODE_IDS      = [1]    # add node IDs here as you expand the network
POLL_INTERVAL = 60     # seconds between polls per node
SYNC_INTERVAL = 3600   # seconds between automatic bulk SD syncs per node (0 = disabled)

# ── Startup ───────────────────────────────────────────────────────────────────
stale = Path(COMMAND_FILE)
if stale.exists():
    print(f"[STARTUP] Clearing stale command file: {stale.read_text()}")
    stale.unlink()

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
    for node_id in timer.due_nodes():
        if timer.sync_due(node_id):
            request_bulk_sync(node_id)
            timer.mark_synced(node_id)
        else:
            request_poll(node_id)
        timer.mark_polled(node_id)

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
            batch.close_batch(data)
            batch.flush(rfm69)
            cmd.check_and_forward(rfm69)

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

