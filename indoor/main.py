'''
Python 3 running on Raspberry Pi 3B

Receive data via the RFM69 radio module and print it to the terminal. 

We'll probably want to have the Pi save the data to a .txt file (or whatever format) and use that file to update the website.
'''
from device_setup_indoor import rfm69, GLED, RLED, blink_led
import json
import threading


# Print out some chip state:
print(f"Temperature: {rfm69.temperature}C")
print(f"Frequency: {rfm69.frequency_mhz}mhz")
print(f"Bit rate: {rfm69.bitrate / 1000}kbit/s")
print(f"Frequency deviation: {rfm69.frequency_deviation}hz")


rfm69.send(bytes("Hello world!\r\n", "utf-8"))
print("Sent hello world message!")
print("Waiting for packets...")


current_ts = None

while True:
    packet = rfm69.receive(with_header=True)
    if packet is not None:
        try:
            payload = packet[4:].decode("utf-8")
            data = json.loads(payload)
            print("Parsed:", data, "Length:", len(packet))

            if data.get("t") == "ts":
                current_ts = data.get("v")
            else:
                if current_ts:
                    data["ts"] = current_ts
                with open("data_from_pico.txt", "a") as f:
                    f.write(json.dumps(data) + "\n")

            blink_led(GLED, times=2)

        except Exception as e:
            print("Bad packet:", e)
            blink_led(RLED, times=1)