'''
Designed for a Raspberry Pi 3B running Raspian Bookworm

Telemetry connection with a Pico W RP2040 -
    Scan for connection
    If connection is present, print out received data

Planned expansion -
    Host a website that displays data from the Pico W 
    Connect other sensors directly to the Pi 
    Add other peripheral systems to the network

Written by Fletcher Meyers - May 2025
#!NEEDS UPDATE - this file is written to work with an MCU running Micropython, not Circuitpython!

'''

import asyncio
from bleak import BleakScanner, BleakClient

PICO_NAME = "PicoTemp"
TEMP_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
TEMP_CHAR_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"

async def main():
    print("Scanning for PicoTemp...")
    devices = await BleakScanner.discover()
    pico = next((d for d in devices if d.name and PICO_NAME in d.name), None)

    if not pico:
        print("PicoTemp not found.")
        return

    print(f"Connecting to {pico.name} ({pico.address})...")
    async with BleakClient(pico) as client:
        print("Connected.")

        def handle_notify(_, data):
            try:
                temp_str = data.decode("utf-8")
                temp_val = float(temp_str)
                print(f"Received temp: {temp_val:.2f} °C")
            except Exception as e:
                print("Decode error:", e)

        await client.start_notify(TEMP_CHAR_UUID, handle_notify)
        print("Receiving data... Press Ctrl+C to stop.")
        while True:
            await asyncio.sleep(1)

asyncio.run(main())
