from hardware_setup_indoor import rfm69, GLED, YLED, RLED, blink_led
from sync_indoor import sensor_health_report, DATA_FILE, COMMAND_FILE
from communication_indoor import CommandManager, BatchReceiver
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
batch = BatchReceiver(DATA_FILE)

while True:
    timeout = 6.0 if cmd.pending else 1.0
    packet = rfm69.receive(with_header=True, timeout=timeout)

    if packet is None:
        continue

    try:
        payload = packet[4:].decode("utf-8")
        data = json.loads(payload)
        pkt_type = data.get("t")

        if pkt_type == "ts":
            batch.open_batch(data.get("v"))

        elif pkt_type == "batch_end":
            batch.close_batch(data)
            batch.flush(rfm69)
            # Forward any pending command now that the Pico is in its listen window
            cmd.check_and_forward(rfm69)
            blink_led(YLED, times=2)

        elif cmd.handle_ack(data):
            # Consumed as a command ack — nothing further to do
            blink_led(YLED, times=2)

        else:
            complete = batch.collect(data)
            if complete:
                # Hit expected count before batch_end arrived
                batch.flush(rfm69)
                cmd.check_and_forward(rfm69)
                blink_led(YLED, times=2)

        blink_led(GLED, times=1)

    except Exception as e:
        print("Bad packet:", e)
        blink_led(RLED, times=2)