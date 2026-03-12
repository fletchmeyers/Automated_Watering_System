'''
CircuitPython 10.0.3 running on Pico 2W RP2350

Read sensor data and save it to the SD card (handled in device_setup.py).
Send sensor data to Pi via radio.
At the end of each loop, briefly listen for a sync/poll command from the Pi.

Written by Fletcher Meyers
February 2026
'''

import time
import json
from hardware_setup_garden import SEND_INTERVAL, get_timestamp, NODE_ID, rfm69, rtc
from communication_garden import SENSORS, PacketSender, write_batch_to_sd
from sync_garden import handle_sync, interruptible_sleep

sender = PacketSender(NODE_ID, rfm69)

while True:
    ts = get_timestamp()
    sd_buffer = []

    sender.send({"t": "ts", "v": ts})
    time.sleep(0.1)

    for sensor_name, sensor_function in SENSORS:
        try:
            data = sensor_function()
            sender.send(data)
            time.sleep(0.1)
            sd_buffer.append(data)
        except Exception as e:
            print(f"[ERROR] Sensor '{sensor_name}' failed:", e)

    if sd_buffer:
        write_batch_to_sd([json.dumps(p, separators=(",", ":")) for p in sd_buffer])

    # Listen briefly for a command from the Pi.
    # timeout is kept short so it doesn't significantly stretch SEND_INTERVAL.
    command = interruptible_sleep(rfm69, SEND_INTERVAL, chunk=0.5)
    if command is not None and command.get("t") == "sync":
        handle_sync(command, rtc, sender, get_timestamp)
