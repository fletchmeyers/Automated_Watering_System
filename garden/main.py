'''
Designed for a Pico W RP2040 running CircuitPython 10.0.3

Read data from the sensors on the I2C bus in the garden and saves the data to an SD card mounted in the datalogger shield.
I don't think CircuitPython can access the BTLE capabilities of the wifi chip on the Pico W, so we'll have to add some other hardware piece to get this data to the indoor Raspberry Pi.
We can either get a regular radio module or we can add a second Pico W to the stack running Micropython, have that read the data in the SD card, and send it to the Raspberry Pi via BTLE.

Written by Fletcher Meyers 
May 2025
'''


import time
from device_setup import i2c, ltr, rtc, SD_CS, spi, sdcard, vfs, get_temp, ss



while True:
    try:
        #  variable for RTC datetime
        t = rtc.datetime
        #  append SD card text file
        with open("/sd/data.txt", "a") as f:
            #read data from LTR390 UV sensor:
            UV = ltr.uvs
            ambient_light = ltr.light
            UVI = ltr.uvi
            lux = ltr.lux
            

            #write UV sensor data to SD card (no timestamp)
            f.write('UV: {}, Ambient light:{}, UVI:{}, lux:{}\n'.format(UV, ambient_light, UVI, lux))
            
            #read soil humidity sensor:
            touch = ss.moisture_read()
            ss_temp = ss.get_temp()
            f.write('Soil humidity sensor 4: {}, temp:{}*F\n'.format(touch, ss_temp))

            #  read temp data from onboard cpu temp sensor
            temp = get_temp()
            #  write temp data followed by the time, comma-delimited
            f.write('{}*F,{}:{}:{}\n'.format(temp, t.tm_hour, t.tm_min, t.tm_sec))
            print("data written to sd card")
        #  repeat every 30 seconds
        time.sleep(30)
    except ValueError:
        print("data error - cannot write to SD card")
        time.sleep(10)

