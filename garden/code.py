'''
CircuitPython 10.0.3 running on Pico 2W RP2350

Read sensor data and save it to the SD card (handled in device_setup.py).
Send sensor data to Pi via radio.
After each batch, listen for a data_ack and any command from the Pi in a
single window. Fall back to SD if no ack is received.

Written by Fletcher Meyers
February 2026
'''

import time
import json
from hardware_setup_garden import SEND_INTERVAL, get_timestamp, NODE_ID, rfm69, rtc
from communication_garden import SENSORS, PacketSender, write_batch_to_sd, check_ack, ACK_WAIT
from sync_garden import handle_sync, interruptible_sleep, indefinite_sleep, handle_set_interval

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

    sender.send_batch_end(expected=len(SENSORS), sent=len(sd_buffer))
    batch_end_q = sender.sequence - 1

    acked, command = check_ack(rfm69, batch_end_q)
    if not acked and sd_buffer:
        write_batch_to_sd([json.dumps(p, separators=(",", ":")) for p in sd_buffer])

    # Handle a command that arrived during the ack window
    if command is not None:
        if command.get("t") == "sync":
            handle_sync(command, rtc, sender, get_timestamp)
        elif command.get("t") == "set_interval":
            new_interval = handle_set_interval(command, sender)
            if new_interval is not None:
                SEND_INTERVAL = new_interval

    # Sleep for the remainder of the interval, still listening for late commands
    if SEND_INTERVAL == 0:
        command = indefinite_sleep(rfm69, chunk=0.5)
    else:
        remaining = max(0, SEND_INTERVAL - ACK_WAIT - 0.5)
        command = interruptible_sleep(rfm69, remaining, chunk=0.5)

    if command is not None:
        if command.get("t") == "sync":
            handle_sync(command, rtc, sender, get_timestamp)
        elif command.get("t") == "set_interval":
            new_interval = handle_set_interval(command, sender)
            if new_interval is not None:
                SEND_INTERVAL = new_interval