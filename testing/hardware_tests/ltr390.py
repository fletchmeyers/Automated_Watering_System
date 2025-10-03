# SPDX-FileCopyrightText: 2021 by Bryan Siepert, written for Adafruit Industries
#
# SPDX-License-Identifier: Unlicense


#!This is copied directly from the Adafruit guide for this sensor using circuit python - convert this to Micropython before putting it on a Micropython board! I'll unfuck this soon.


import time
import machine
import adafruit_ltr390

i2c = machine.I2C(0, scl=machine.Pin(21), sda=machine.Pin(20))
# i2c = board.STEMMA_I2C()  # For using the built-in STEMMA QT connector on a microcontroller
ltr = adafruit_ltr390.LTR390(i2c)

while True:
    print("UV:", ltr.uvs, "\t\tAmbient Light:", ltr.light)
    print("UVI:", ltr.uvi, "\t\tLux:", ltr.lux)
    time.sleep(1.0)
