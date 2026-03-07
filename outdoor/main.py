'''
CircuitPython 10.0.3 running on Pico 2W RP2350

Read sendor data and save it to the SD card (handled in device_setup.py). 
Send sensor data to Pi via radio.

Written by Fletcher Meyers 
February 2026

'''

import time
import json
from device_setup import SEND_INTERVAL, get_timestamp
from communication_garden import SENSORS, send_packet, write_batch_to_sd


while True:
    ts = get_timestamp()
    sd_buffer = []

    send_packet({"t": "ts", "v": ts})

    for sensor_name, sensor_function in SENSORS:

        try:
            data = sensor_function()
            send_packet(data)
            sd_buffer.append(data)

        except Exception as e:
            print(f"[ERROR] Sensor '{sensor_name}' failed:", e)


    if sd_buffer:
        write_batch_to_sd([json.dumps(p, separators=(",", ":")) for p in sd_buffer])


    time.sleep(SEND_INTERVAL)


