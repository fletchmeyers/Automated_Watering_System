'''
Python 3.11.2 running on Raspberry Pi 3B

This code blinks the LEDs on the protoboard.
'''

import board 
import digitalio
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


while True:
    RLED.value = True
    time.sleep(0.5)
    RLED.value = False

    YLED.value = True
    time.sleep(0.5)
    YLED.value = False

    GLED.value = True
    time.sleep(0.5)
    GLED.value = False
    time.sleep(0.5)

    
    RLED.value = True
    YLED.value = True
    GLED.value = True
    time.sleep(0.5)

    RLED.value = False
    YLED.value = False
    GLED.value = False
    time.sleep(0.5)