/*
 * board_config_rp2.h
 *
 * Board config for the RP2040 / RP2350 boards (Earle Philhower's Arduino-Pico
 * core): Pico, Pico W, Pico 2, Pico 2 W and Feather RP2040 Adalogger. Each
 * has its own PlatformIO environment in platformio.ini; nodes.json picks one
 * with "board".
 *
 * The per-node settings (node ID, radio and SD pins, intervals, storage)
 * come from nodes.json: raspberrypi/node_setup.py passes them as -D build
 * flags. The values below are only defaults for a plain `pio run`.
 *
 * The radio uses the board's default SPI bus (Pico: SCK GP18, MOSI GP19,
 * MISO GP16; Feather: the SCK/MO/MI header pins). An SD card on its own bus,
 * like the Adalogger's built-in slot, is given as SD_SPI_SCK/MOSI/MISO and
 * goes on the second SPI bus (SPI1).
 */

#ifndef BOARD_CONFIG_H
#define BOARD_CONFIG_H

#include <Arduino.h>
#include <ArduinoJson.h>

// ── Node identity ────────────────────────────────────────────────────────
#ifndef NODE_ID
  #define NODE_ID 3
#endif

// ── Radio ────────────────────────────────────────────────────────────────
#ifndef RFM69_CS
  #define RFM69_CS   17
#endif
#ifndef RFM69_INT
  #define RFM69_INT  21
#endif
#ifndef RFM69_RST
  #define RFM69_RST  20
#endif

#define RADIO_FREQ_MHZ 915.0

// Must exactly match the Pico node's encryption_key in hardware_setup_garden.py
static const uint8_t RADIO_ENCRYPT_KEY[16] = {
  0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08,
  0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08
};

// ── Sensing ──────────────────────────────────────────────────────────────
#ifndef DEFAULT_SENSE_INTERVAL_MS
  #define DEFAULT_SENSE_INTERVAL_MS 3000
#endif

// ── Reading log (see node_log.h) ─────────────────────────────────────────
// microSD if nodes.json gives an SD card pin, otherwise no log.
#ifndef LOG_BACKEND
  #ifdef SD_CS
    #define LOG_BACKEND LOG_SD
  #else
    #define LOG_BACKEND LOG_NONE
  #endif
#endif
#ifndef LOG_INTERVAL_MS
  #define LOG_INTERVAL_MS (5UL * 60 * 1000)
#endif

// ── Clock ────────────────────────────────────────────────────────────────
// No RTC: the clock is millis()-based, set by the Pi's polls, and starts
// unset after every reset (nothing is logged until the first poll). With a
// PCF8523 on the I2C bus, nodes.json "rtc": "pcf8523" adds
// -D BOARD_HAS_PCF8523 and it carries the time across resets.
#ifdef BOARD_HAS_PCF8523
  #include <RTClib.h>
  extern RTC_PCF8523 ext_rtc;
#endif

// Sleep (see node_sleep.h) switches the radio off but keeps the chip awake
// on that clock — no RTC alarm to wake it from a deeper sleep.

#include "sensors.h"

#endif
