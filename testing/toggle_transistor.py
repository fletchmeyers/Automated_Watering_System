'''
Designed for a Pico W RP2040 running Micropython v1.25.0
MOSFET opens/closes the circuit with the fluid solenoid and battery, toggling pin 26 here controls the MOSFET. 
'''

import machine
import time

transistor = machine.Pin(26, machine.Pin.OUT)
led = machine.Pin("LED", machine.Pin.OUT)

while True:
    transistor.on()  
    led.on()
    time.sleep(3)
    transistor.off()  
    led.off()
    time.sleep(3)