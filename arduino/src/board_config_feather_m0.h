/*
 * board_config_feather_m0.h
 *
 * Board-specific config for: Feather M0 (Basic Proto, no built-in radio)
 * + RFM69HCW Radio FeatherWing (stacked, requires a soldered IRQ jumper).
 *
 * This is the ONLY file that should need to change to bring up a different
 * board later — pins, node identity, and the sensor list all live here.
 * packet_protocol.h/.cpp should never need to know which board it's on.
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
// Must be unique across every radio node the Pi talks to (Pico node is 1).
#define NODE_ID 2

// ── Radio ────────────────────────────────────────────────────────────────
// Feather M0 + RFM69 FeatherWing. These match this board's actual jumper
// wiring (FeatherWing pad -> Feather pin): CS -> D10, IRQ -> D6, RST -> D11.
#define RFM69_CS   10
#define RFM69_INT  6
#define RFM69_RST  11

#define RADIO_FREQ_MHZ 915.0

// Must exactly match the Pico node's encryption_key in hardware_setup_garden.py
static const uint8_t RADIO_ENCRYPT_KEY[16] = {
  0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08,
  0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08
};

// ── Sensing ──────────────────────────────────────────────────────────────
// Seconds between sense cycles. Mirrors SENSE_INTERVAL in hardware_setup_garden.py
// — can be overwritten at runtime by a set_interval command.
#define DEFAULT_SENSE_INTERVAL_MS 3000

// ── Sensor list ──────────────────────────────────────────────────────────
// Each sensor is an {init_fn, read_fn, ok} entry. init_fn runs once in
// setup() and sets `ok`; read_fn is only called if `ok` is true. This is
// the Arduino equivalent of the Pico's try_init()-gated SENSORS list —
// a sensor that isn't physically present just gets skipped, rather than
// failing sketch startup. Add a new sensor by writing an init_fn/read_fn
// pair here and a row to SENSOR_LIST in packet_protocol.cpp.

#define SOIL_ADDR_0 0x37
#define SOIL_ADDR_1 0x38 
#define SOIL_ADDR_2 0x39

extern Adafruit_seesaw soil_0, soil_1, soil_2;
extern Adafruit_MAX17048 max17;
extern Adafruit_LTR390   ltr;
extern Adafruit_SHT4x    sht40;
extern Adafruit_SGP40    sgp40;
extern Adafruit_INA238   ina_0, ina_1, ina_2, ina_3;

#define VBAT_PIN A7

struct SensorEntry {
  const char *tag;                  // packet "t" value, e.g. "vbat"
  bool (*init_fn)();                // returns true if the sensor is present
  void (*read_fn)(JsonObject &pkt); // fills in the rest of the packet fields
  bool ok;                          // set by init_fn at startup
};

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

// Shared with sgp40_read() below for humidity compensation — updated every
// cycle sht40 reads successfully, left at reasonable defaults otherwise.
// Mirrors make_sgp40_compensated_fn()'s behavior on the Pico, just without
// the Python closure — plain statics do the same job here. Depends on
// SENSOR_LIST running "sht" before "voc" each cycle (see packet_protocol.cpp).
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