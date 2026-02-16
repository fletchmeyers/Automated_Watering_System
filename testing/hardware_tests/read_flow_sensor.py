'''
Written for Pico W running CircuitPython 10.1.0-beta.1
Read flow sensor (hall effect sensor), light up the green LED when sensor is reporting flow and print out state of sensor once a second.
'''


import board
import time
import digitalio


led = digitalio.DigitalInOut(board.GP17)
#led = digitalio.DigitalInOut(board.LED)      #Replace previous line with this to blink the Pico's onboard LED instead of the one soldered to the flow sensor 
led.direction = digitalio.Direction.OUTPUT


hall_pin = digitalio.DigitalInOut(board.GP18)
hall_pin.direction = digitalio.Direction.INPUT
hall_pin.pull = digitalio.Pull.DOWN 

while True:
    hall_state = hall_pin.value
    if hall_state == 1:
        print("Magnet detected")
        led.value = 1
    else:
        print("No magnet detected")
        led.value = 0
        
    time.sleep(1)


