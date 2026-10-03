'''
This code toggles the transistor pin that controls the solenoid circuit and blinks the onboard LED. 
When the LED is on, the solenoid circuit should be closed. 
If you don't hear the solenoid opening and closing, check continuity between the middle and left pins of the transistor while the code is running. There should be continuity when the circuit should be closed. 
'''

import board
import digitalio
import time

transistor = digitalio.DigitalInOut(board.GP26)
transistor.direction = digitalio.Direction.OUTPUT
led = digitalio.DigitalInOut(board.LED)
led.direction = digitalio.Direction.OUTPUT
button = digitalio.DigitalInOut(board.GP2)
button.direction = digitalio.Direction.INPUT         
button.pull = digitalio.Pull.UP  

transistor.value = False  
led.value = False


while True:
    if button.value == False:
            transistor.value = True  
            led.value = True
    else:
        transistor.value = False  
        led.value = False
    time.sleep(0.1)


