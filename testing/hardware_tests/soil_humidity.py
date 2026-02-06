#Example for Pico running CircuitPython 10.0.3.
# This code runs the soil humidity sensor through the stemmaQT port on the adalogger shield. Other than the pin definition for the I2C bus, it's identical to the example code on the Adafruit guide: https://learn.adafruit.com/adafruit-stemma-soil-sensor-i2c-capacitive-moisture-sensor/python-circuitpython-test
import time

import board

from adafruit_seesaw.seesaw import Seesaw

i2c_bus = board.STEMMA_I2C()  

ss = Seesaw(i2c_bus, addr=0x36) #!Might need to replace 0x36 with 37, 38, or 39 depending on which solder pads are joined on the back of the sensor.
while True:
    # read moisture level through capacitive touch pad
    touch = ss.moisture_read()

    # read temperature from the temperature sensor
    temp = ss.get_temp()

    print("temp: " + str(temp) + "  moisture: " + str(touch))
    time.sleep(1)

