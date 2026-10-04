'''
CircuitPython 10.0.3 running on Pico 2W RP2350
Set up SPI for microSD and radio, I2C bus, and other sensors (flow meter, battery monitors)
'''

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
import adafruit_sht4x
import adafruit_sgp40
import adafruit_ina23x


# CONFIG
# This node's ID, intervals and pins come from node_config.py, which the Pi's
# node_setup.py writes from nodes.json. The values below are only used if
# it's missing (a board set up by hand).
try:
    from node_config import NODE_ID, SENSE_INTERVAL, LOG_INTERVAL, USE_SD, PINS
except ImportError:
    NODE_ID = 1
    SENSE_INTERVAL = 3   # seconds between sensor reads; updated via set_interval command
    LOG_INTERVAL = 60    # seconds between SD log writes (a subset of sensor reads) —
                         # every logged line goes back over the radio, so keep this
                         # well under SYNC_LINES_PER_HOUR in main.py
    USE_SD = True
    PINS = {
        "spi_sck": "GP18", "spi_mosi": "GP19", "spi_miso": "GP16",
        "radio_cs": "GP22", "radio_rst": "GP27", "sd_cs": "GP17",
    }
RADIO_FREQ_MHZ = 915.0

sequence = 0


def pin(name):
    '''The board pin nodes.json names for name, e.g. "radio_cs" -> board.GP22.'''
    return getattr(board, PINS[name])


# SPI SETUP — a bad pin name in nodes.json leaves the board running (no
# radio or SD) with a warning, rather than stopping it from starting at all.
try:
    spi = busio.SPI(clock=pin("spi_sck"), MOSI=pin("spi_mosi"), MISO=pin("spi_miso"))
except Exception as e:
    print(f"[WARN] SPI bus not set up (check the pins in nodes.json): {e}")
    spi = None

# Radio — without one (e.g. a board on the bench) the node still runs and
# answers over USB, so it can be set up and tested from the Pi.
try:
    radio_cs = digitalio.DigitalInOut(pin("radio_cs"))
    radio_reset = digitalio.DigitalInOut(pin("radio_rst"))
    rfm69 = adafruit_rfm69.RFM69(spi, radio_cs, radio_reset, RADIO_FREQ_MHZ)
    rfm69.tx_power = 20   # RFM69HCW maximum — the link to the Pi needs the margin
    rfm69.encryption_key = b"\x01\x02\x03\x04\x05\x06\x07\x08\x01\x02\x03\x04\x05\x06\x07\x08"
except Exception as e:
    print(f"[WARN] No radio found, USB only: {e}")
    rfm69 = None

# SD card — without one, the node still runs; it just has no log to sync.
# Boards with a built-in card slot on its own SPI bus (e.g. the Feather
# RP2040 Adalogger) give that bus's pins as sd_sck/sd_mosi/sd_miso.
if USE_SD:
    try:
        sd_spi = (busio.SPI(clock=pin("sd_sck"), MOSI=pin("sd_mosi"), MISO=pin("sd_miso"))
                  if "sd_sck" in PINS else spi)
        sdcard = sdcardio.SDCard(sd_spi, pin("sd_cs"))
        storage.mount(storage.VfsFat(sdcard), "/sd")
    except Exception as e:
        print(f"[WARN] SD card not mounted, readings won't be logged: {e}")


def try_init(name, init_fn):
    try:
        return init_fn()
    except Exception as e:
        print(f"[WARN] Could not init {name}: {e}")
        return None


# I2C + SENSORS — the board's STEMMA QT pins unless nodes.json gives
# i2c_scl/i2c_sda. With nothing wired up (no pull-ups) there's no bus at all.
try:
    i2c = busio.I2C(pin("i2c_scl"), pin("i2c_sda")) if "i2c_scl" in PINS else board.STEMMA_I2C()
except Exception as e:
    print(f"[WARN] No I2C bus, so no sensors (set i2c_scl/i2c_sda in nodes.json?): {e}")
    i2c = None


def i2c_init(name, init_fn):
    '''try_init() for a sensor on the I2C bus; skipped quietly when there's no bus.'''
    return try_init(name, init_fn) if i2c is not None else None


rtc    = i2c_init("RTC",      lambda: PCF8523(i2c))
if rtc is None:
    # No PCF8523: keep time on the chip's own clock instead. It starts at
    # 2000 after every reset until the Pi's first poll sets it (code.py
    # doesn't log readings until then).
    import rtc as chip_clock
    rtc = chip_clock.RTC()
max17  = i2c_init("MAX1704x", lambda: adafruit_max1704x.MAX17048(i2c))
ltr    = i2c_init("LTR390",   lambda: adafruit_ltr390.LTR390(i2c))
soil_0 = i2c_init("Soil_0",   lambda: Seesaw(i2c, addr=0x37))
soil_1 = i2c_init("Soil_1",   lambda: Seesaw(i2c, addr=0x38))
soil_2 = i2c_init("Soil_2",   lambda: Seesaw(i2c, addr=0x39))
sht40  = i2c_init("SHT40",    lambda: adafruit_sht4x.SHT4x(i2c))
if sht40:
    sht40.mode = adafruit_sht4x.Mode.NOHEAT_HIGHPRECISION
sgp40     = i2c_init("SGP40",      lambda: adafruit_sgp40.SGP40(i2c))
ina238_0  = i2c_init("INA238_0x40", lambda: adafruit_ina23x.INA23X(i2c, address=0x40))
ina238_1  = i2c_init("INA238_0x41", lambda: adafruit_ina23x.INA23X(i2c, address=0x41))
ina238_2  = i2c_init("INA238_0x44", lambda: adafruit_ina23x.INA23X(i2c, address=0x44))
ina238_3  = i2c_init("INA238_0x45", lambda: adafruit_ina23x.INA23X(i2c, address=0x45))


def get_timestamp(clock=None):
    t = (clock if clock is not None else rtc).datetime
    return "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}".format(
        t.tm_year, t.tm_mon, t.tm_mday,
        t.tm_hour, t.tm_min, t.tm_sec
    )

