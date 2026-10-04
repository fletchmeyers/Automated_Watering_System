'''
CircuitPython 10.0.3 running on Pico 2W RP2350

Main loop: read sensors on a timer, keep latest reading in memory, log to SD
on a slower timer.
Listen for Pi commands at all times and dispatch them immediately.

Written by Fletcher Meyers
February 2026
'''

import time
import usb_cdc
from hardware_setup_garden import SENSE_INTERVAL, LOG_INTERVAL, get_timestamp, NODE_ID, rfm69, rtc
from communication_garden import (
    SENSORS, PacketSender, SerialLink, store_latest_reading,
    append_to_sd, send_latest, send_sync_chunk, send_storage_info,
)
from sync_garden import check_for_command, dispatch_command

sender        = PacketSender(NODE_ID, rfm69)
# The Pi can also send commands over USB (see boot.py and the Pi's
# usb_sync.py). usb_cdc.data is None until boot.py has run after a reset.
usb           = SerialLink(usb_cdc.data) if usb_cdc.data else None
usb_sender    = PacketSender(NODE_ID, usb) if usb else None
last_sense_at = time.monotonic() - SENSE_INTERVAL  # sense immediately on first loop
last_log_at   = time.monotonic() - LOG_INTERVAL    # log the first reading too

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

        # Polls get every reading; the SD log only keeps one every
        # LOG_INTERVAL, since everything on it has to go back over the radio.
        if now - last_log_at >= LOG_INTERVAL:
            last_log_at = now
            append_to_sd(packets, ts)

    # ── Radio listen (short timeout so sense loop stays on schedule) ───────
    command = check_for_command(rfm69, timeout=0.1)
    if command is not None:
        new_interval = dispatch_command(
            command, sender, rfm69, rtc,
            get_timestamp, send_latest, send_sync_chunk,
            NODE_ID, send_storage_info,
        )
        if new_interval is not None:
            SENSE_INTERVAL = new_interval

    # ── USB commands (replies go back over USB, not the radio) ─────────────
    if usb is not None:
        command = usb.receive()
        if command is not None:
            command.setdefault("n", NODE_ID)   # over a cable, it can only be for us
            new_interval = dispatch_command(
                command, usb_sender, rfm69, rtc,
                get_timestamp, send_latest, send_sync_chunk,
                NODE_ID, send_storage_info,
            )
            if new_interval is not None:
                SENSE_INTERVAL = new_interval