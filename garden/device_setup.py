'''
Designed for a Pico W RP2040 running CircuitPython 10.0.3
Set up SPI for microSD, I2C bus, and other sensors (flow meter, battery monitors)


'''

import time
import board
import sdcardio
import busio
import storage
from adafruit_pcf8523.pcf8523 import PCF8523
import microcontroller
import adafruit_ltr390

i2c = board.STEMMA_I2C() 
ltr = adafruit_ltr390.LTR390(i2c)


# setup for RTC
rtc = PCF8523(i2c)

#  list of days to print to the text file on boot
days = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

# SPI SD_CS pin
SD_CS = board.GP17

#  SPI setup for SD card
spi = busio.SPI(board.GP18, board.GP19, board.GP16)
sdcard = sdcardio.SDCard(spi, SD_CS)
vfs = storage.VfsFat(sdcard)
try:
    storage.mount(vfs, "/sd")
    print("sd card mounted")
except ValueError:
    print("no SD card")

#  to update the RTC, call set_clock and input parameters in this order: year, mon, date, hour, min, sec. 
#   wday, yday, isdst can also be set if we decide they'd be useful.
#  RTC will remain set through power cycles, as long as the coincell battery doesn't die or disconnect

def set_clock(year, mon, date, hour, min, sec):
    #                     year, mon, date, hour, min, sec, wday, yday, isdst
    t = time.struct_time((year, mon, date, hour, min, sec,   -1,    -1))
    print("Setting time to:", t)#
    rtc.datetime = t
    print()






#  variable to hold RTC datetime
t = rtc.datetime

time.sleep(1)

def read_cpu_temp():
    temperature_celsius = microcontroller.cpu.temperature
    temperature_fahrenheit = microcontroller.cpu.temperature * 9 / 5 + 32
    return temperature_fahrenheit


def write_date_to_sd(days, t):
#  initial write to the SD card on startup
    try:
        with open("/sd/data.txt", "a") as f:
            #  writes the date
            f.write('The date is {} {}/{}/{}\n'.format(days[t.tm_wday], t.tm_mon, t.tm_mday, t.tm_year))
            #  writes the start time
            f.write('Start time: {}:{}:{}\n'.format(t.tm_hour, t.tm_min, t.tm_sec))

            print("initial write to SD card complete, starting to log")
    except ValueError:
        print("initial write to SD card failed - check card")
