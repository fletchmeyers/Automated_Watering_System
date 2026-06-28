'''
CircuitPython 10.0.3 running on Pico 2W RP2350

Main loop: read sensors on a timer, log to SD, keep latest reading in memory.
Listen for Pi commands at all times and dispatch them immediately.

Written by Fletcher Meyers
February 2026
'''

import time
from hardware_setup_garden import SENSE_INTERVAL, get_timestamp, NODE_ID, rfm69, rtc
from communication_garden import (
    SENSORS, PacketSender, store_latest_reading,
    append_to_sd, send_latest, send_bulk_sync,
)
from sync_garden import check_for_command, dispatch_command

sender        = PacketSender(NODE_ID, rfm69)
last_sense_at = time.monotonic() - SENSE_INTERVAL  # sense immediately on first loop

while True:
    now = time.monotonic()

    # ── Sense cycle ────────────────────────────────────────────────────────
    if now - last_sense_at >= SENSE_INTERVAL:
        last_sense_at = now
        ts      = get_timestamp()
        packets = []

        for sensor_name, sensor_fn in SENSORS:
            try:
                packets.append(sensor_fn())
            except Exception as e:
                print(f"[ERROR] Sensor '{sensor_name}' failed: {e}")

        store_latest_reading(packets)
        append_to_sd(packets, ts)

    # ── Radio listen (short timeout so sense loop stays on schedule) ───────
    command = check_for_command(rfm69, timeout=0.1)
    if command is not None:
        new_interval = dispatch_command(
            command, sender, rfm69, rtc,
            get_timestamp, send_latest, send_bulk_sync,
        )
        if new_interval is not None:
            SENSE_INTERVAL = new_interval

