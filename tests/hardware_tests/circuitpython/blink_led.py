"""Example for Pico running CircuitPython 10.0.3. Blinks the built-in LED."""
import time
import board
import digitalio

led = digitalio.DigitalInOut(board.LED)
led.direction = digitalio.Direction.OUTPUT

while True:
    led.value = True	
    time.sleep(2)
    print("LED is on: ", led.value)
    led.value = False
    time.sleep(2)
    print("LED is on: ", led.value)

