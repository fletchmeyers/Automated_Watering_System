'''
This code should read the hall effect of the flow sensor. It doesn't do that and I'm not sure if it's an issue with the code, with the wiring, or something else entirely!
'''

import machine
import time

# On the Pico W, the onboard LED is on GPIO pin 15
led = machine.Pin("LED", machine.Pin.OUT)
tz = machine.Pin(28, machine.Pin.OUT)
gled = machine.Pin(17, machine.Pin.OUT)

# Define the GPIO pin connected to the Hall sensor
hall_pin = machine.Pin(0, machine.Pin.IN)

while True:
    hall_state = hall_pin.value()
    if hall_state == 1:
        print("Magnet detected")
        gled.value(0)
    else:
        print("No magnet detected")
        gled.value(1)
    led.toggle()      # flip LED state
    tz.toggle()
    time.sleep(3)   # wait 0.5 seconds


