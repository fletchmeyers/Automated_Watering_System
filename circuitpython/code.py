'''
CircuitPython 10.0.3 running on Pico 2W RP2350

Main loop: read sensors on a timer, keep latest reading in memory, log to SD
on a slower timer.
Listen for Pi commands at all times and dispatch them immediately.

A "sleep" command from the Pi (its nightly sleep_window) switches the radio
and Wi-Fi off until the wake time it gives, still logging a reading every
LOG_INTERVAL so the next day's sync collects the night (sleep_until()).

The chip's watchdog (node_watchdog.py) starts first, before the sensors and
SD card are set up, and is fed on every pass: if anything hangs for 8 s the
board resets itself, and if the code crashes it prints why and resets, so a
node in the garden recovers without anyone going out to it.

Written by Fletcher Meyers
February 2026
'''

import time
import node_watchdog

node_watchdog.start()

import usb_cdc
from hardware_setup_garden import (
    SENSE_INTERVAL, LOG_INTERVAL, get_timestamp, NODE_ID, rfm69, rtc,
    LINKS, BATTERY_SAVER, WIFI_CHECK_HOURS,
)
node_watchdog.feed()   # setting up the sensors and SD card can take a moment
import communication_garden
import wifi_link as wifi_link_module
from communication_garden import (
    SENSORS, PacketSender, SerialLink, store_latest_reading,
    append_to_sd, send_latest, send_sync_chunk, send_storage_info,
)
import sync_garden
from sync_garden import check_for_command, dispatch_command
from wifi_link import WifiLink
from link_power import LinkPower
from sensor_guard import SensorGuard

# Sending a packet (and waiting to join Wi-Fi) feeds the watchdog too.
communication_garden.feed = node_watchdog.feed
wifi_link_module.feed = node_watchdog.feed

sender        = PacketSender(NODE_ID, rfm69)
# The Pi can also send commands over USB (see boot.py and the Pi's
# usb_sync.py). usb_cdc.data is None until boot.py has run after a reset.
usb           = SerialLink(usb_cdc.data) if usb_cdc.data else None
usb_sender    = PacketSender(NODE_ID, usb) if usb else None
guard         = SensorGuard()   # takes a sensor that keeps failing or dragging offline (sensor_guard.py)
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


def handle(command, reply_sender):
    '''Act on a command addressed to this node; a new sense interval sticks.'''
    global SENSE_INTERVAL
    if command.get("t") == "poll" and command.get("n") == NODE_ID:
        # Every poll says how to send over the radio: "m" in "lp" for
        # MessagePack, none for JSON. This answer already goes out that way.
        communication_garden.set_radio_format("m" in str(command.get("lp", "")))
    new_interval = dispatch_command(
        command, reply_sender, rfm69, rtc,
        get_timestamp, send_latest, send_sync_chunk,
        NODE_ID, send_storage_info,
    )
    if new_interval is not None:
        SENSE_INTERVAL = new_interval


def sense(now):
    '''Read every sensor, keep it for the next poll, and log it if due.'''
    global last_sense_at, last_log_at
    last_sense_at = now
    ts      = get_timestamp()
    packets = []

    for sensor_name, sensor_fn in SENSORS:
        if not guard.due(sensor_name):
            continue   # offline: tried again later
        started = time.monotonic()
        ok = True
        try:
            packets.append(sensor_fn())
        except Exception as e:
            ok = False
            print(f"[ERROR] Sensor '{sensor_name}' failed: {e}")
        note = guard.result(sensor_name, ok, time.monotonic() - started)
        if note:
            print(note)
        node_watchdog.feed()   # a few slow sensors in a row mustn't add up to a reset

    store_latest_reading(packets)
    communication_garden.sensors_offline = guard.offline()   # told to the Pi with each poll

    # Polls get every reading; the SD log only keeps one every
    # LOG_INTERVAL, since everything on it has to go back over the radio.
    # A board without a battery-backed clock reads 2000 until the Pi's
    # first poll sets it — don't log readings with that date.
    if now - last_log_at >= LOG_INTERVAL and ts >= "2024":
        last_log_at = now
        append_to_sd(packets, ts)


def sleep_until(wake):
    '''The Pi's sleep command (already answered): radio and Wi-Fi off until
    wake ("YYYY-MM-DDTHH:MM:SS" on this node's clock), logging a reading
    every LOG_INTERVAL. The chip itself stays awake, like the Arduino Pico
    nodes: CircuitPython's deeper sleeps restart code.py on waking, and the
    radio and Wi-Fi are most of the power anyway. Ctrl+C still works.'''
    global radio_on
    print(f"[SLEEP] Radio and Wi-Fi off until {wake} (the chip stays awake, "
          f"logging a reading every {LOG_INTERVAL} s).")
    if rfm69 is not None:
        rfm69.sleep()
    radio_on = False
    if wifi_link is not None and wifi_link.on:
        wifi_link.stop()

    while get_timestamp() < wake:
        node_watchdog.feed()
        now = time.monotonic()
        if now - last_log_at >= LOG_INTERVAL:
            sense(now)
        time.sleep(1)

    # Links back on: the radio's next receive() switches it on; with both
    # links, loop_once() powers whichever link_power.py wants, counting
    # quiet time from now.
    radio_on = rfm69 is not None
    if power is not None:
        power.reset()
    elif wifi_link is not None and wifi_link.configured:
        wifi_link.start()
    print(f"[SLEEP] Awake at {get_timestamp()}, links back on.")


def loop_once():
    global radio_on
    now = time.monotonic()

    # ── Sense cycle ────────────────────────────────────────────────────────
    if now - last_sense_at >= SENSE_INTERVAL:
        sense(now)

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
        handle(command, sender)

    # ── Wi-Fi commands (replies go back over the same connection) ──────────
    if wifi_link is not None:
        command = wifi_link.receive()
        if command is not None:
            command.setdefault("n", NODE_ID)
            if power is not None:
                power.heard("wifi", command)
            handle(command, wifi_sender)

    # ── USB commands (replies go back over USB, not the radio) ─────────────
    if usb is not None:
        command = usb.receive()
        if command is not None:
            command.setdefault("n", NODE_ID)   # over a cable, it can only be for us
            handle(command, usb_sender)

    # ── Sleep, if the Pi asked for it (the ack has already gone out) ──────
    if sync_garden.requested_wake is not None:
        wake = sync_garden.requested_wake
        sync_garden.requested_wake = None
        sleep_until(wake)


try:
    while True:
        node_watchdog.feed()
        loop_once()
except KeyboardInterrupt:
    # Ctrl+C at the console (a person, or node_setup.py reaching the REPL):
    # hand it over without the watchdog resetting the board under them.
    node_watchdog.stop()
    raise
except Exception as e:
    if not node_watchdog.running():
        raise   # no_watchdog: stay at the console with the error, for debugging
    try:
        import traceback
        traceback.print_exception(e)
    except Exception:
        print(f"[ERROR] {type(e).__name__}: {e}")
    print("[WATCHDOG] The code crashed; restarting the board in 5 s.")
    time.sleep(5)   # time to read the error on the console
    import microcontroller
    microcontroller.reset()
