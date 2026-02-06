#Example for Pico running CircuitPython 10.0.3.
# This code runs the LTR390 light sensor through the stemmaQT port on the adalogger shield. Other than the pin definition for the I2C bus, it's identical to the example code on the Adafruit guide: https://learn.adafruit.com/adafruit-ltr390-uv-sensor/python-circuitpython
import adafruit_ltr390

i2c = board.STEMMA_I2C() 
ltr = adafruit_ltr390.LTR390(i2c)

while True:
    print("UV:", ltr.uvs, "\t\tAmbient Light:", ltr.light)
    print("UVI:", ltr.uvi, "\t\tLux:", ltr.lux)
    time.sleep(1.0)

