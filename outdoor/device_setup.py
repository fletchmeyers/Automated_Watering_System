'''
CircuitPython 10.0.3 running on Pico 2W RP2350
Set up SPI for microSD and radio, I2C bus, and other sensors (flow meter, battery monitors)


'''

import time
import json
import board
import busio
import digitalio
import storage
import sdcardio

from adafruit_pcf8523.pcf8523 import PCF8523
import adafruit_rfm69
import adafruit_max1704x
import adafruit_ltr390
from adafruit_seesaw.seesaw import Seesaw



# CONFIG
NODE_ID = 1
SEND_INTERVAL = 5
RADIO_FREQ_MHZ = 915.0

sequence = 0



# SPI SETUP
spi = busio.SPI(clock=board.GP18, MOSI=board.GP19, MISO=board.GP16)

# Radio pins
radio_cs = digitalio.DigitalInOut(board.GP22)
radio_reset = digitalio.DigitalInOut(board.GP26)
rfm69 = adafruit_rfm69.RFM69(spi, radio_cs, radio_reset, RADIO_FREQ_MHZ)
rfm69.tx_power = 13
rfm69.encryption_key = b"\x01\x02\x03\x04\x05\x06\x07\x08\x01\x02\x03\x04\x05\x06\x07\x08"

# SD card
SD_CS = board.GP17
sdcard = sdcardio.SDCard(spi, SD_CS)
vfs = storage.VfsFat(sdcard)
storage.mount(vfs, "/sd")





def try_init(name, init_fn):
    try:
        return init_fn()
    except Exception as e:
        print(f"[WARN] Could not init {name}: {e}")
        return None
    

# I2C + SENSORS
i2c = board.STEMMA_I2C()
rtc = try_init("RTC", lambda: PCF8523(i2c))#fixed address: 0x68
max17 = try_init("MAX1704x", lambda: adafruit_max1704x.MAX17048(i2c) )#fixed address: 0x36
ltr = try_init("LTR390", lambda: adafruit_ltr390.LTR390(i2c)) #fixed address: 0x53
soil_0 = try_init("Soil_0", lambda: Seesaw(i2c, addr=0x37))
soil_1 = try_init("Soil_1", lambda: Seesaw(i2c, addr=0x38))
soil_2 = try_init("Soil_2", lambda: Seesaw(i2c, addr=0x39))




def get_timestamp():
    t = rtc.datetime
    return "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}".format(
        t.tm_year, t.tm_mon, t.tm_mday,
        t.tm_hour, t.tm_min, t.tm_sec
    )


