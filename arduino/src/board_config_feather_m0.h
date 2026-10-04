/*
 * board_config_feather_m0.h
 *
 * Board-specific config for: Feather M0 (Basic Proto, no built-in radio)
 * + RFM69HCW Radio FeatherWing (stacked, requires a soldered IRQ jumper).
 *
 * This is the ONLY file that should need to change to bring up a different
 * board later — pins, node identity and board-specific readings live here
 * (the sensors shared by every board are in sensors.h).
 * packet_protocol.h/.cpp should never need to know which board it's on.
 *
 * The per-node settings (node ID, radio and SD pins, intervals, storage)
 * come from nodes.json: raspberrypi/node_setup.py passes them as -D build
 * flags. The values below are only the defaults for a plain `pio run`.
 */

#ifndef BOARD_CONFIG_H
#define BOARD_CONFIG_H

#include <Arduino.h>
#include <ArduinoJson.h>
#include <RTClib.h>

// ── Node identity ────────────────────────────────────────────────────────
// Must be unique across every radio node the Pi talks to (Pico node is 1).
#ifndef NODE_ID
  #define NODE_ID 2
#endif

// ── Radio ────────────────────────────────────────────────────────────────
// Feather M0 + RFM69 FeatherWing. These match this board's actual jumper
// wiring (FeatherWing pad -> Feather pin): CS -> D9, IRQ -> D6, RST -> D11.
// CS moved off D10 to make room for the Adalogger's SD card. D9 is also the
// battery divider pin (A7), so the vbat reading is dropped while CS is on
// D9 — move CS to D5 or D12 to get it back.
#ifndef RFM69_CS
  #define RFM69_CS   9
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
// Seconds between sense cycles. Mirrors SENSE_INTERVAL in hardware_setup_garden.py
// — can be overwritten at runtime by a set_interval command.
#ifndef DEFAULT_SENSE_INTERVAL_MS
  #define DEFAULT_SENSE_INTERVAL_MS 3000
#endif

// ── Reading log (see node_log.h) ─────────────────────────────────────────
// Adalogger FeatherWing (Adafruit 2922) microSD, CS on D10. FAT16/FAT32
// cards only. Without the SD card, LOG_FLASH_SAMD keeps ~2000 readings in
// the top LOG_FLASH_BYTES of spare internal flash instead.
#ifndef LOG_BACKEND
  #define LOG_BACKEND    LOG_SD
  #define SD_CS          10
#endif
#ifndef LOG_FLASH_BYTES
  #define LOG_FLASH_BYTES (128UL * 1024)
#endif
#ifndef LOG_INTERVAL_MS                      // overridable with -D for quick bench tests
  #define LOG_INTERVAL_MS (5UL * 60 * 1000)  // one logged snapshot every 5 minutes
#endif

// ── Clock ────────────────────────────────────────────────────────────────
// The SAMD21's built-in RTC, running from the Feather's 32 kHz crystal,
// keeps time in standby sleep where millis() stops. The Adalogger's PCF8523
// (I2C 0x68, coin-cell backed) carries the time across resets.
#include <RTCZero.h>
#define BOARD_HAS_RTC
extern RTCZero rtc;
#ifndef BOARD_HAS_PCF8523
  #define BOARD_HAS_PCF8523
#endif
extern RTC_PCF8523 ext_rtc;

// Sleep (see node_sleep.h) uses the SAMD21's standby mode. Build with
// -D SLEEP_USE_STANDBY=0 for bench tests: it stays awake on a timer
// instead, so the USB serial console keeps working through a "sleep".
#ifndef SLEEP_USE_STANDBY
  #define SLEEP_USE_STANDBY 1
#endif

// ── Board-specific readings ──────────────────────────────────────────────
// The shared sensor list is in sensors.h.

#include "sensors.h"

#define VBAT_PIN 9   // A7 — a plain number so #if can compare it with RFM69_CS

// Battery voltage via the Feather's onboard resistor divider — needs no
// extra hardware, so this one is always "present".
inline bool vbat_init() {
  pinMode(VBAT_PIN, INPUT);
  return true;
}
inline void vbat_read(JsonObject &pkt) {
  float measured = analogRead(VBAT_PIN);
  measured *= 2.0;          // divider halves the voltage
  measured *= 3.3;          // reference voltage
  measured /= 1024.0;       // 10-bit ADC
  pkt["v"] = measured;
}

#endif
