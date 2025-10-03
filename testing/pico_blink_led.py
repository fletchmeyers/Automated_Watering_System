'''
This code blinks the onboard LED on the pico. If you run this and don't see the LED blinking, either there's an issue with how you push code to your pico or your pico is broken!
'''

import machine
import time

# Use the "LED" alias (preferred way on Pico W)
led = machine.Pin("LED", machine.Pin.OUT)

while True:
    led.on()
    time.sleep(0.5)
    led.off()
    time.sleep(0.5)
