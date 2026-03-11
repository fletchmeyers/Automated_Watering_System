import board
import busio
import digitalio
import adafruit_rfm69
import json
import threading
import time


RLED = digitalio.DigitalInOut(board.D19)
RLED.direction = digitalio.Direction.OUTPUT
YLED = digitalio.DigitalInOut(board.D20)
YLED.direction = digitalio.Direction.OUTPUT
GLED = digitalio.DigitalInOut(board.D21)
GLED.direction = digitalio.Direction.OUTPUT

RLED.value = False
YLED.value = False
GLED.value = False

# Radio setup
CS = digitalio.DigitalInOut(board.D25)
RESET = digitalio.DigitalInOut(board.D24)
RADIO_FREQ_MHZ = 915.0 

# Define the onboard LED
LED = digitalio.DigitalInOut(board.D13)
LED.direction = digitalio.Direction.OUTPUT

# Initialize SPI bus.
spi = busio.SPI(board.SCK, MOSI=board.MOSI, MISO=board.MISO)

# Initialze RFM radio
rfm69 = adafruit_rfm69.RFM69(spi, CS, RESET, RADIO_FREQ_MHZ)

# Optionally set an encryption key (16 byte AES key). MUST match both
# on the transmitter and receiver (or be set to None to disable/the default).
rfm69.encryption_key = b"\x01\x02\x03\x04\x05\x06\x07\x08\x01\x02\x03\x04\x05\x06\x07\x08"

led_lock = threading.Lock()

def blink_led(led, times=1, duration=0.15):
    threading.Thread(
        target=_blink_led_blocking,
        args=(led,),
        kwargs={"times": times, "duration": duration},
        daemon=True
    ).start()

def _blink_led_blocking(led, times=1, duration=0.15):
    if not led_lock.acquire(blocking=False):
        return
    try:
        for _ in range(times):
            led.value = True
            time.sleep(duration)
            led.value = False
            time.sleep(duration)
    finally:
        led_lock.release()