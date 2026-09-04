/*
 * board_config_pico.h
 *
 * Board-specific config for: Raspberry Pi Pico family running Arduino
 * (earlephilhower core) — covers Pico, Pico W, Pico 2, Pico 2 W, since
 * they're pin-compatible; the exact variant is selected in platformio.ini,
 * not here.
 *
 * SPI/CS/RST pins mirror the CircuitPython Pico 2W garden node
 * (hardware_setup_garden.py) for consistency — NOT yet confirmed against
 * actual wiring on this board. Update before first flash.
 */

#ifndef BOARD_CONFIG_H
#define BOARD_CONFIG_H

#include <Arduino.h>
#include <ArduinoJson.h>
#include <Adafruit_seesaw.h>
#include <Adafruit_MAX1704X.h>
#include <Adafruit_LTR390.h>
#include <Adafruit_SHT4x.h>
#include <Adafruit_SGP40.h>
#include <Adafruit_INA238.h>

// ── Node identity ────────────────────────────────────────────────────────
// Must be unique across every radio node the Pi talks to (garden Pico is 1,
// Feather M0 is 2).
#define NODE_ID 3

// ── Radio ────────────────────────────────────────────────────────────────
// TODO: confirm against actual wiring once this board is on the bench.
// Proposed to mirror the garden Pico's SPI/CS/RST GPIOs for consistency.
#define RFM69_CS   22
#define RFM69_INT  21   // TODO: pick an interrupt-capable GPIO once wired
#define RFM69_RST  27

#define RADIO_FREQ_MHZ 915.0

// Must exactly match the other nodes' encryption_key.
static const uint8_t RADIO_ENCRYPT_KEY[16] = {
  0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08,
  0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08
};

// ── Sensing ──────────────────────────────────────────────────────────────
#define DEFAULT_SENSE_INTERVAL_MS 3000

// ── Sensor list ──────────────────────────────────────────────────────────
// Same sensor set/gating pattern as the Feather M0 — fill in real
// hardware as it's wired up; anything absent just gets skipped at boot.

#define SOIL_ADDR_0 0x37
#define SOIL_ADDR_1 0x38
#define SOIL_ADDR_2 0x39

extern Adafruit_seesaw   soil_0, soil_1, soil_2;
extern Adafruit_MAX17048 max17;
extern Adafruit_LTR390   ltr;
extern Adafruit_SHT4x    sht40;
extern Adafruit_SGP40    sgp40;
extern Adafruit_INA238   ina_0, ina_1, ina_2, ina_3;

// TODO: RP2040/RP2350's ADC is 12-bit, not the M0's 10-bit — if this node
// gets a battery-divider sensor like vbat_read(), that math needs
// /4096.0, not /1024.0. No divider on this node yet, so nothing to fix
// here until one exists.

struct SensorEntry {
  const char *tag;
  bool (*init_fn)();
  void (*read_fn)(JsonObject &pkt);
  bool ok;
};

inline bool soil_0_init() { return soil_0.begin(SOIL_ADDR_0); }
inline void soil_0_read(JsonObject &pkt) {
  pkt["m"]   = soil_0.touchRead(0);
  pkt["tmp"] = soil_0.getTemp();
}
inline bool soil_1_init() { return soil_1.begin(SOIL_ADDR_1); }
inline void soil_1_read(JsonObject &pkt) {
  pkt["m"]   = soil_1.touchRead(0);
  pkt["tmp"] = soil_1.getTemp();
}
inline bool soil_2_init() { return soil_2.begin(SOIL_ADDR_2); }
inline void soil_2_read(JsonObject &pkt) {
  pkt["m"]   = soil_2.touchRead(0);
  pkt["tmp"] = soil_2.getTemp();
}

inline bool max17_init() { return max17.begin(); }
inline void max17_read(JsonObject &pkt) {
  pkt["v"]   = max17.cellVoltage();
  pkt["soc"] = max17.cellPercent();
}

inline bool ltr_init() { return ltr.begin(); }
inline void ltr_read(JsonObject &pkt) {
  ltr.setMode(LTR390_MODE_ALS);
  pkt["lux"] = ltr.readALS();
  ltr.setMode(LTR390_MODE_UVS);
  pkt["uv"]  = ltr.readUVS();
}

static float last_temp_c = 25.0;
static float last_rh_pct = 50.0;

inline bool sht40_init() { return sht40.begin(); }
inline void sht40_read(JsonObject &pkt) {
  sensors_event_t humidity, temp;
  sht40.getEvent(&humidity, &temp);
  pkt["tmp"] = temp.temperature;
  pkt["rh"]  = humidity.relative_humidity;
  last_temp_c = temp.temperature;
  last_rh_pct = humidity.relative_humidity;
}

inline bool sgp40_init() { return sgp40.begin(); }
inline void sgp40_read(JsonObject &pkt) {
  pkt["voc"] = sgp40.measureRaw(last_temp_c, last_rh_pct);
}

inline bool ina_0_init() { return ina_0.begin(0x40); }
inline bool ina_1_init() { return ina_1.begin(0x41); }
inline bool ina_2_init() { return ina_2.begin(0x44); }
inline bool ina_3_init() { return ina_3.begin(0x45); }
inline void ina_0_read(JsonObject &pkt) { pkt["v"] = ina_0.readBusVoltage(); pkt["ma"] = ina_0.readCurrent(); }
inline void ina_1_read(JsonObject &pkt) { pkt["v"] = ina_1.readBusVoltage(); pkt["ma"] = ina_1.readCurrent(); }
inline void ina_2_read(JsonObject &pkt) { pkt["v"] = ina_2.readBusVoltage(); pkt["ma"] = ina_2.readCurrent(); }
inline void ina_3_read(JsonObject &pkt) { pkt["v"] = ina_3.readBusVoltage(); pkt["ma"] = ina_3.readCurrent(); }

#endif