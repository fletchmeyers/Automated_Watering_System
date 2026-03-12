from device_setup_indoor import rfm69, GLED, RLED, blink_led
from sync_indoor import sensor_health_report, DATA_FILE, check_and_forward_command
import json

print(f"Temperature: {rfm69.temperature}C")
print(f"Frequency: {rfm69.frequency_mhz}mhz")
print(f"Bit rate: {rfm69.bitrate / 1000}kbit/s")
print(f"Frequency deviation: {rfm69.frequency_deviation}hz")
print("Waiting for packets...")

current_ts = None
    
while True:
    # Check if sync_indoor.py left a command for us to forward
    check_and_forward_command(rfm69)

    packet = rfm69.receive(with_header=True, timeout=1.0)  # use a timeout so we loop regularly
    if packet is None:
        continue

    try:
        payload = packet[4:].decode("utf-8")
        data = json.loads(payload)
        print("Parsed:", data, "Length:", len(packet))

        pkt_type = data.get("t")

        if pkt_type == "ts":
            current_ts = data.get("v")

        elif pkt_type == "sync_ack":
            current_ts = data.get("ts")
            print(f"[SYNC] Pico RTC confirmed: {current_ts}")

        else:
            if current_ts:
                data["ts"] = current_ts
            with open(DATA_FILE, "a") as f:
                f.write(json.dumps(data) + "\n")

        blink_led(GLED, times=2)

    except Exception as e:
        print("Bad packet:", e)
        blink_led(RLED, times=2)