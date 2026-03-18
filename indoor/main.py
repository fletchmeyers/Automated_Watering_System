from hardware_setup_indoor import rfm69, GLED, YLED, RLED, blink_led
from sync_indoor import sensor_health_report, DATA_FILE, COMMAND_FILE
from communication_indoor import CommandManager
import json
from pathlib import Path

stale = Path(COMMAND_FILE)
if stale.exists():
    print(f"[STARTUP] Clearing stale command file: {stale.read_text()}")
    stale.unlink()




print(f"Temperature: {rfm69.temperature}C")
print(f"Frequency: {rfm69.frequency_mhz}mhz")
print(f"Bit rate: {rfm69.bitrate / 1000}kbit/s")
print(f"Frequency deviation: {rfm69.frequency_deviation}hz")


cmd = CommandManager()
current_ts = None

while True:
    forwarded = cmd.check_and_forward(rfm69)

    timeout = 6.0 if cmd.pending else 1.0
    packet = rfm69.receive(with_header=True, timeout=timeout)

    if forwarded and packet is None:
        sensor_health_report(DATA_FILE)
        continue

    if packet is None:
        continue

    try:
        payload = packet[4:].decode("utf-8")
        data = json.loads(payload)
        pkt_type = data.get("t")

        if pkt_type == "ts":
            current_ts = data.get("v")
        elif pkt_type == "sync_ack":
            current_ts = data.get("ts")
            print(f"[SYNC] Pico RTC confirmed: {current_ts}")
            cmd.handle_ack(data)
            blink_led(YLED, times=2)  
            sensor_health_report(DATA_FILE)
        elif pkt_type == "set_interval_ack":
            cmd.handle_ack(data)
            blink_led(YLED, times=2)  
        else:
            if current_ts:
                data["ts"] = current_ts
            with open(DATA_FILE, "a") as f:
                f.write(json.dumps(data) + "\n")
            print("Received:", data) # Take this out when we've got a nicer dashboard

        blink_led(GLED, times=2)

    except Exception as e:
        print("Bad packet:", e)
        blink_led(RLED, times=2)