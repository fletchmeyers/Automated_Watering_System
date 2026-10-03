'''
Designed for a Pico W RP2040 running CircuitPython 10.0.3
MOSFET opens/closes the circuit with the fluid solenoid and battery, toggling pin 26 here controls the MOSFET. 
'''

import time
import board
import digitalio

transistor = digitalio.DigitalInOut(board.GP26)
transistor.direction = digitalio.Direction.OUTPUT

while True:
    transistor.value = True	
    time.sleep(2)
    print("Transistor is closed: ", led.value)
    transistor.value = False
    time.sleep(2)
    print("Transistor is closed: ", led.value)

