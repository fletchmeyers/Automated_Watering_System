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
#include <Adafruit_seesaw.h>
#include <Adafruit_MAX1704X.h>
#include <Adafruit_LTR390.h>
#include <Adafruit_SHT4x.h>
#include <Adafruit_SGP40.h>
#include <Adafruit_INA238.h>

#define SOIL_ADDR 0x38

extern Adafruit_seesaw   soil_seesaw;
extern Adafruit_MAX17048 max17;
extern Adafruit_LTR390   ltr;
extern Adafruit_SHT4x    sht40;
extern Adafruit_SGP40    sgp40;
extern Adafruit_INA238 ina_0, ina_1, ina_2, ina_3;

inline bool soil_init() { return soil_seesaw.begin(SOIL_ADDR); }
inline void soil_read(JsonObject &pkt) {
  pkt["m"]   = soil_seesaw.touchRead(0);
  pkt["tmp"] = soil_seesaw.getTemp();
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
// the Python closure — plain statics do the same job here.
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

inline void ina_0_read(JsonObject &pkt) {
  pkt["v"]  = ina_0.readBusVoltage();
  pkt["ma"] = ina_0.readCurrent();
}
inline void ina_1_read(JsonObject &pkt) { pkt["v"] = ina_1.readBusVoltage(); pkt["ma"] = ina_1.readCurrent(); }
inline void ina_2_read(JsonObject &pkt) { pkt["v"] = ina_2.readBusVoltage(); pkt["ma"] = ina_2.readCurrent(); }
inline void ina_3_read(JsonObject &pkt) { pkt["v"] = ina_3.readBusVoltage(); pkt["ma"] = ina_3.readCurrent(); }


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
// failing sketch startup.
//
// Battery voltage is included as a working example since it needs no
// extra hardware (Feather boards have a built-in resistor divider on the
// battery-sense pin). Add real sensors the same way: write an init_fn
// that returns true/false, a read_fn that fills a JsonObject, and add a
// row to SENSOR_LIST in packet_protocol.cpp.

#define VBAT_PIN A7

struct SensorEntry {
  const char *tag;                  // packet "t" value, e.g. "vbat"
  bool (*init_fn)();                // returns true if the sensor is present
  void (*read_fn)(JsonObject &pkt); // fills in the rest of the packet fields
  bool ok;                          // set by init_fn at startup
};

// Example sensor: battery voltage via the Feather's onboard divider.
inline bool vbat_init() {
  pinMode(VBAT_PIN, INPUT);
  return true; // always "present" — it's a wired-in divider, not a chip
}

inline void vbat_read(JsonObject &pkt) {
  float measured = analogRead(VBAT_PIN);
  measured *= 2.0;          // divider halves the voltage
  measured *= 3.3;          // reference voltage
  measured /= 1024.0;       // 10-bit ADC
  pkt["v"] = measured;
}

/*
 * To add a real I2C sensor later (e.g. an SHT40), the pattern is:
 *
 *   Adafruit_SHT4x sht40;
 *   bool sht40_init() { return sht40.begin(); }
 *   void sht40_read(JsonObject &pkt) {
 *     sensors_event_t humidity, temp;
 *     sht40.getEvent(&humidity, &temp);
 *     pkt["tmp"] = temp.temperature;
 *     pkt["rh"]  = humidity.relative_humidity;
 *   }
 *
 * ...then add {"sht", sht40_init, sht40_read, false} to SENSOR_LIST.
 */

#endif