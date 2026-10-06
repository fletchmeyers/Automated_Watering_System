/*
 * board_config_esp32.h
 *
 * Board config for the ESP32 Feathers (Espressif's Arduino core): the
 * ESP32-S2 Feather (native USB) and the ESP32 Feather V2 (USB-serial chip).
 * Each has its own PlatformIO environment in platformio.ini; nodes.json
 * picks one with "board".
 *
 * The per-node settings (node ID, radio and SD pins, intervals, storage)
 * come from nodes.json: raspberrypi/node_setup.py passes them as -D build
 * flags. The values below are only defaults for a plain `pio run` — the
 * radio pins in particular depend on how the RFM69 FeatherWing's jumpers
 * are soldered, so set them in nodes.json.
 *
 * The radio uses the board's default SPI pins (the Feather SCK/MO/MI
 * header) unless nodes.json gives spi_sck/spi_mosi/spi_miso.
 */

#ifndef BOARD_CONFIG_H
#define BOARD_CONFIG_H

#include <Arduino.h>
#include <ArduinoJson.h>

// ── Node identity ────────────────────────────────────────────────────────
#ifndef NODE_ID
  #define NODE_ID 13
#endif

// ── Radio ────────────────────────────────────────────────────────────────
#ifndef RFM69_CS
  #define RFM69_CS   10
#endif
#ifndef RFM69_INT
  #define RFM69_INT  6
#endif
#ifndef RFM69_RST
  #define RFM69_RST  11
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
// microSD (e.g. an Adalogger FeatherWing) if nodes.json gives an SD card
// pin, otherwise no log.
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
// No RTC chip: the clock is millis()-based, set by the Pi's polls, and
// starts unset after every reset (nothing is logged until the first poll).
// With a PCF8523, nodes.json "rtc": "pcf8523" adds -D BOARD_HAS_PCF8523.
#ifdef BOARD_HAS_PCF8523
  #include <RTClib.h>
  extern RTC_PCF8523 ext_rtc;
#endif

// Sleep (see node_sleep.h) switches the radio off but keeps the chip awake.

// ── Board power ──────────────────────────────────────────────────────────
// Both Feathers switch power to their STEMMA QT port (and NeoPixel) with a
// pin that has to be driven high before any I2C sensor can answer.
inline void board_early_init() {
#if defined(PIN_I2C_POWER)
  pinMode(PIN_I2C_POWER, OUTPUT);
  digitalWrite(PIN_I2C_POWER, HIGH);
#elif defined(NEOPIXEL_I2C_POWER)
  pinMode(NEOPIXEL_I2C_POWER, OUTPUT);
  digitalWrite(NEOPIXEL_I2C_POWER, HIGH);
#endif
  delay(10);   // let the sensors power up
}

#include "sensors.h"

// ── Board-specific readings ──────────────────────────────────────────────
// The Feather V2 has a battery divider on BATT_MONITOR (A13): half the
// battery voltage, read in calibrated millivolts.
#ifdef BATT_MONITOR
  #define VBAT_PIN BATT_MONITOR
  inline bool vbat_init() { return true; }
  inline void vbat_read(JsonObject &pkt) {
    pkt["v"] = analogReadMilliVolts(BATT_MONITOR) * 2 / 1000.0;
  }
#endif

#endif
