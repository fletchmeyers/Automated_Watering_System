/*
 * sensors.h
 *
 * The sensors every Arduino node looks for at boot, shared by all the
 * board_config_*.h files. Each is an {init_fn, read_fn, ok} entry in
 * SENSOR_LIST (packet_protocol.cpp); one that isn't wired up just fails
 * its init and is skipped, so the same firmware suits any mix of sensors.
 * Board-specific readings (like the Feather M0's battery divider) stay in
 * that board's config file.
 */

#ifndef SENSORS_H
#define SENSORS_H

#include <Arduino.h>
#include <ArduinoJson.h>
#include <Adafruit_seesaw.h>
#include <Adafruit_MAX1704X.h>
#include <Adafruit_LTR390.h>
#include <Adafruit_SHT4x.h>
#include <Adafruit_SGP40.h>
#include <Adafruit_INA238.h>

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

// read_fn returns false when the read plainly didn't work (the library
// said so, or gave its can't-reach-it value: 65535 from the seesaw, NaN from
// the battery gauge or power monitors); sensor_guard.h counts those, and
// slow reads, towards taking the sensor offline.
struct SensorEntry {
  const char *tag;                  // packet "t" value, e.g. "vbat"
  bool (*init_fn)();                // returns true if the sensor is present
  bool (*read_fn)(JsonObject &pkt); // fills in the rest of the packet fields; false if it failed
  bool ok;                          // set by init_fn at startup
  // sensor_guard.cpp's bookkeeping (zero to start)
  uint8_t strikes;                  // bad reads in a row
  bool off;                         // taken offline
  uint8_t tries;                    // retries since
  uint32_t retry_at;                // clock_now() to try it again
};

// Soil moisture is the middle of SOIL_SAMPLES quick touch reads: a few
// wild ones (from a noisy USB charger, say) are thrown away rather than
// averaged in. Same as moisture() in the CircuitPython node.
#define SOIL_SAMPLES 9
inline uint16_t soil_moisture(Adafruit_seesaw &soil) {
  uint16_t reads[SOIL_SAMPLES];
  for (uint8_t i = 0; i < SOIL_SAMPLES; i++) {
    uint16_t value = soil.touchRead(0);
    uint8_t j = i;
    for (; j > 0 && reads[j - 1] > value; j--) reads[j] = reads[j - 1];   // keep them sorted
    reads[j] = value;
  }
  return reads[SOIL_SAMPLES / 2];
}

// The seesaw's touchRead() gives 65535 when it can't reach the sensor.
inline bool soil_read(Adafruit_seesaw &soil, JsonObject &pkt) {
  uint16_t m = soil_moisture(soil);
  if (m == 65535) return false;
  pkt["m"]   = m;
  pkt["tmp"] = soil.getTemp();
  return true;
}

inline bool soil_0_init() { return soil_0.begin(SOIL_ADDR_0); }
inline bool soil_0_read(JsonObject &pkt) { return soil_read(soil_0, pkt); }

inline bool soil_1_init() { return soil_1.begin(SOIL_ADDR_1); }
inline bool soil_1_read(JsonObject &pkt) { return soil_read(soil_1, pkt); }

inline bool soil_2_init() { return soil_2.begin(SOIL_ADDR_2); }
inline bool soil_2_read(JsonObject &pkt) { return soil_read(soil_2, pkt); }

inline bool max17_init() { return max17.begin(); }
inline bool max17_read(JsonObject &pkt) {
  float v = max17.cellVoltage();
  if (isnan(v)) return false;
  pkt["v"]   = v;
  pkt["soc"] = max17.cellPercent();
  return true;
}

inline bool ltr_init() { return ltr.begin(); }
inline bool ltr_read(JsonObject &pkt) {
  ltr.setMode(LTR390_MODE_ALS);
  pkt["lux"] = ltr.readALS();
  ltr.setMode(LTR390_MODE_UVS);
  pkt["uv"]  = ltr.readUVS();
  return true;   // no failure value to check: only a slow read counts
}

// Shared with sgp40_read() below for humidity compensation — updated every
// cycle sht40 reads successfully, left at reasonable defaults otherwise.
// Mirrors make_sgp40_compensated_fn()'s behavior on the Pico, just without
// the Python closure — plain statics do the same job here. Depends on
// SENSOR_LIST running "sht" before "voc" each cycle (see packet_protocol.cpp).
static float last_temp_c = 25.0;
static float last_rh_pct = 50.0;

inline bool sht40_init() { return sht40.begin(); }
inline bool sht40_read(JsonObject &pkt) {
  sensors_event_t humidity, temp;
  if (!sht40.getEvent(&humidity, &temp)) return false;
  pkt["tmp"] = temp.temperature;
  pkt["rh"]  = humidity.relative_humidity;
  last_temp_c = temp.temperature;
  last_rh_pct = humidity.relative_humidity;
  return true;
}

inline bool sgp40_init() { return sgp40.begin(); }
inline bool sgp40_read(JsonObject &pkt) {
  uint16_t raw = sgp40.measureRaw(last_temp_c, last_rh_pct);
  if (raw == 0) return false;   // the library's failure value
  pkt["voc"] = raw;
  return true;
}

inline bool ina_0_init() { return ina_0.begin(0x40); }
inline bool ina_1_init() { return ina_1.begin(0x41); }
inline bool ina_2_init() { return ina_2.begin(0x44); }
inline bool ina_3_init() { return ina_3.begin(0x45); }

inline bool ina_read(Adafruit_INA238 &ina, JsonObject &pkt) {
  float v = ina.readBusVoltage();
  if (isnan(v)) return false;
  pkt["v"]  = v;
  pkt["ma"] = ina.readCurrent();
  return true;
}
inline bool ina_0_read(JsonObject &pkt) { return ina_read(ina_0, pkt); }
inline bool ina_1_read(JsonObject &pkt) { return ina_read(ina_1, pkt); }
inline bool ina_2_read(JsonObject &pkt) { return ina_read(ina_2, pkt); }
inline bool ina_3_read(JsonObject &pkt) { return ina_read(ina_3, pkt); }

#endif
