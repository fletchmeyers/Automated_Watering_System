'''
Python 3 running on Raspberry Pi 3B

Receive data via the RFM69 radio module and print it to the terminal. 

We'll probably want to have the Pi save the data to a .txt file (or whatever format) and use that file to update the website.
'''
from device_setup_indoor import GLED, rfm69, blink_gled
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
while True:
    packet = rfm69.receive(with_header=True)

    if packet is not None:
        try:
            # RFM69 with_header=True prepends 4 header bytes
            payload = packet[4:].decode("utf-8")
            data = json.loads(payload)
            print("Parsed:", data, "Length:", len(packet))
            # TODO: write to file / database / MQTT
            threading.Thread(target=blink_gled, args=(GLED,), kwargs={"times": 2}, daemon=True).start()
            #print("Raw packet bytes:", packet)
            print("Length:", len(packet))

        except Exception as e:
            print("Bad packet:", e)
            threading.Thread(target=blink_gled, args=(GLED,), kwargs={"duration": 0.5}, daemon=True).start()
