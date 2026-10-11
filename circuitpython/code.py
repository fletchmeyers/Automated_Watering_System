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
from hardware_setup_garden import (
    SENSE_INTERVAL, LOG_INTERVAL, get_timestamp, NODE_ID, rfm69, rtc,
    LINKS, BATTERY_SAVER, WIFI_CHECK_HOURS,
)
from communication_garden import (
    SENSORS, PacketSender, SerialLink, store_latest_reading,
    append_to_sd, send_latest, send_sync_chunk, send_storage_info,
)
from sync_garden import check_for_command, dispatch_command
from wifi_link import WifiLink
from link_power import LinkPower

sender        = PacketSender(NODE_ID, rfm69)
# The Pi can also send commands over USB (see boot.py and the Pi's
# usb_sync.py). usb_cdc.data is None until boot.py has run after a reset.
usb           = SerialLink(usb_cdc.data) if usb_cdc.data else None
usb_sender    = PacketSender(NODE_ID, usb) if usb else None
last_sense_at = time.monotonic() - SENSE_INTERVAL  # sense immediately on first loop
last_log_at   = time.monotonic() - LOG_INTERVAL    # log the first reading too

# Wi-Fi (wifi_link.py), if nodes.json gives this node it. With the radio
# too, link_power.py says which to keep powered (both, unless battery saver).
wifi_link   = WifiLink(NODE_ID) if "wifi" in LINKS else None
wifi_sender = PacketSender(NODE_ID, wifi_link) if wifi_link else None
power       = LinkPower(LINKS, BATTERY_SAVER, WIFI_CHECK_HOURS) if len(LINKS) > 1 else None
radio_on    = rfm69 is not None


def new_wifi_settings(link):
    '''True (once) if settings.toml has Wi-Fi settings this board hasn't
    run with before: node_setup.py just wrote them, and is waiting to see the
    node connect. Remembered in the chip's own non-volatile memory.'''
    try:
        import microcontroller
        stamp = f"{link.ssid}|{link.host}|{link.port}".encode()[:120]
        stored = bytes(microcontroller.nvm[0:1 + len(stamp)])
        if stored == bytes([len(stamp)]) + stamp:
            return False
        microcontroller.nvm[0:1 + len(stamp)] = bytes([len(stamp)]) + stamp
        return True
    except Exception:
        return False


if wifi_link is not None and wifi_link.configured:
    if power is None:
        wifi_link.start()             # Wi-Fi only
    elif new_wifi_settings(wifi_link):
        power.keep("wifi")            # on for a while, so node_setup.py can see it connect

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
        # A board without a battery-backed clock reads 2000 until the Pi's
        # first poll sets it — don't log readings with that date.
        if now - last_log_at >= LOG_INTERVAL and ts >= "2024":
            last_log_at = now
            append_to_sd(packets, ts)

    # ── Which links are powered (battery saver switches one off) ──────────
    if power is not None:
        want_radio, want_wifi = power.plan()
        if rfm69 is not None and want_radio != radio_on:
            if not want_radio:
                rfm69.sleep()   # the next receive() switches it back on
            radio_on = want_radio
            print("[LINKS] Radio on." if radio_on else "[LINKS] Radio off (battery saver).")
        if wifi_link.configured and want_wifi != wifi_link.on:
            if want_wifi:
                wifi_link.start()
            else:
                wifi_link.stop()

    # ── Radio listen (short timeout so sense loop stays on schedule) ───────
    if rfm69 is not None and radio_on:
        command = check_for_command(rfm69, timeout=0.1)
    else:
        command = None   # no radio, or it's switched off: USB (and Wi-Fi) only
        time.sleep(0.1)
    if command is not None and power is not None and command.get("n") == NODE_ID:
        power.heard("radio", command)
    if command is not None:
        new_interval = dispatch_command(
            command, sender, rfm69, rtc,
            get_timestamp, send_latest, send_sync_chunk,
            NODE_ID, send_storage_info,
        )
        if new_interval is not None:
            SENSE_INTERVAL = new_interval

    # ── Wi-Fi commands (replies go back over the same connection) ──────────
    if wifi_link is not None:
        command = wifi_link.receive()
        if command is not None:
            command.setdefault("n", NODE_ID)
            if power is not None:
                power.heard("wifi", command)
            new_interval = dispatch_command(
                command, wifi_sender, rfm69, rtc,
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