#include "packet_protocol.h"
#include "node_sleep.h"
#include "sensor_guard.h"

// ── Sensor list ──────────────────────────────────────────────────────────
// Board-specific entries live here (not in the header) since this is where
// SENSOR_COUNT gets computed. Add new sensors from sensors.h
// as additional rows.

Adafruit_seesaw soil_0, soil_1, soil_2;
Adafruit_MAX17048 max17;
Adafruit_LTR390   ltr;
Adafruit_SHT4x    sht40;
Adafruit_SGP40    sgp40;
Adafruit_INA238 ina_0, ina_1, ina_2, ina_3;
#ifdef BOARD_HAS_RTC
RTCZero rtc;
#endif
#ifdef BOARD_HAS_PCF8523
RTC_PCF8523 ext_rtc;
static bool ext_rtc_ok = false;
#endif

SensorEntry SENSOR_LIST[] = {
#if defined(VBAT_PIN) && RFM69_CS != VBAT_PIN   // a board with a battery divider pin, not also the radio CS
  { "vbat", vbat_init,   vbat_read,   false },
#endif
  { "s0",   soil_0_init, soil_0_read, false },
  { "s1",   soil_1_init, soil_1_read, false },
  { "s2",   soil_2_init, soil_2_read, false },
  { "batt", max17_init,  max17_read,  false },
  { "uv",   ltr_init,    ltr_read,    false },
  { "sht",  sht40_init,  sht40_read,  false },
  { "voc",  sgp40_init,  sgp40_read,  false },
  { "pw0",  ina_0_init,  ina_0_read,  false },
  { "pw1",  ina_1_init,  ina_1_read,  false },
  { "pw2",  ina_2_init,  ina_2_read,  false },
  { "pw3",  ina_3_init,  ina_3_read,  false },
};
const size_t SENSOR_COUNT = sizeof(SENSOR_LIST) / sizeof(SENSOR_LIST[0]);
bool radio_msgpack = false;
JsonDocument latest_readings[MAX_SENSORS];
const char *latest_tags[MAX_SENSORS];
size_t latest_count = 0;

void init_sensors() {
  for (size_t i = 0; i < SENSOR_COUNT; i++) {
    SENSOR_LIST[i].ok = SENSOR_LIST[i].init_fn();
    if (!SENSOR_LIST[i].ok) {
      Serial.print(F("[WARN] Could not init sensor: "));
      Serial.println(SENSOR_LIST[i].tag);
    }
  }
}

// ── Sense cycle, a sensor at a time ──────────────────────────────────────
// loop() reads one sensor per pass and checks the radio (and USB and Wi-Fi)
// in between, so a command arriving mid-cycle is answered after one sensor
// read at most rather than the whole cycle. (RadioHead catches the packet
// on the radio's interrupt either way; it's the answer that has to wait,
// since it can't be sent from inside the interrupt.) The cycle under way
// reads into these, copied to latest_readings when it's done, so polls and
// the log always get a complete set. Each sensor that's present and not
// offline (sensor_guard.h) is read and timed; a failed read leaves nothing,
// a slow one is kept.
static JsonDocument pending_readings[MAX_SENSORS];
static const char *pending_tags[MAX_SENSORS];
static size_t pending_count = 0;
static size_t cycle_next = 0;
static bool cycle_busy = false;

bool sense_cycle_busy() { return cycle_busy; }

void sense_cycle_start() {
  cycle_busy = true;
  cycle_next = 0;
  pending_count = 0;
}

bool sense_cycle_step() {
  if (!cycle_busy) return true;
  while (cycle_next < SENSOR_COUNT) {
    SensorEntry &sensor = SENSOR_LIST[cycle_next++];
    if (!sensor.ok || !sensor_due(sensor)) continue;   // costs nothing: on to the next
    if (pending_count >= MAX_SENSORS) {
      cycle_next = SENSOR_COUNT;
      break;
    }
    pending_readings[pending_count].clear();
    JsonObject obj = pending_readings[pending_count].to<JsonObject>();
    uint32_t started = millis();
    bool ok = sensor.read_fn(obj);
    sensor_result(sensor, ok, millis() - started);
    node_watchdog_feed();
    if (ok) {
      pending_tags[pending_count] = sensor.tag;
      pending_count++;
    }
    break;   // one read per step: back to the radio
  }
  if (cycle_next < SENSOR_COUNT) return false;
  for (size_t i = 0; i < pending_count; i++) {
    latest_readings[i] = pending_readings[i];
    latest_tags[i] = pending_tags[i];
  }
  latest_count = pending_count;
  cycle_busy = false;
  return true;
}

// A whole cycle now (asleep, with the radio off anyway).
void run_sense_cycle() {
  sense_cycle_start();
  while (!sense_cycle_step()) {}
}

void send_latest(PacketSender &sender, const char *timestamp) {
  // Always answer a poll, even with nothing to report (no sensors attached,
  // or before the first sense cycle). An empty batch_end tells the Pi the
  // node heard it; staying silent makes the Pi time out and, after a few
  // polls, decide the node is unreachable.
  // Sensors taken offline (sensor_guard.h) are listed after the readings.
  char offline[64];
  sensors_offline(offline, sizeof(offline));
  size_t total = latest_count + (offline[0] ? 1 : 0);
  if (total == 0) {
    sender.send_batch_end(0, 0);
    return;
  }

  JsonDocument ts_doc;
  ts_doc["v"] = timestamp;
  sender.send(ts_doc, "ts");
  delay(100);

  uint8_t sent = 0;
  for (size_t i = 0; i < latest_count; i++) {
    sender.send(latest_readings[i], latest_tags[i]);
    delay(100);
    sent++;
  }
  if (offline[0]) {
    JsonDocument off_doc;
    off_doc["s"] = offline;
    sender.send(off_doc, "off");
    delay(100);
    sent++;
  }

  sender.send_batch_end(total, sent);
}

// ── Clock ────────────────────────────────────────────────────────────────
// Set from the "ts" in every poll. Times are the Pi's local wall-clock time
// counted as seconds since 1970, and only ever turned back into the same
// "YYYY-MM-DDTHH:MM:SS" form — no time zones involved.
//
// clock_now() reads the SAMD21's internal RTC (keeps running in sleep), or
// millis() on boards without one. A battery-backed PCF8523, if the board
// has one, carries the time across resets and power loss: it sets the
// internal clock at boot and is corrected whenever a poll shows it drifting.

static bool     have_time = false;
#ifndef BOARD_HAS_RTC
static uint32_t base_epoch = 0, base_ms = 0;
#endif

// Days since 1970-01-01 for a civil date, and back (Howard Hinnant's algorithms).
static int32_t days_from_civil(int y, unsigned m, unsigned d) {
  y -= m <= 2;
  const int era = (y >= 0 ? y : y - 399) / 400;
  const unsigned yoe = (unsigned)(y - era * 400);
  const unsigned mm = m > 2 ? m - 3 : m + 9;
  const unsigned doy = (153 * mm + 2) / 5 + d - 1;
  const unsigned doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
  return era * 146097 + (int32_t)doe - 719468;
}

static void civil_from_days(int32_t z, int *y, unsigned *m, unsigned *d) {
  z += 719468;
  const int era = (z >= 0 ? z : z - 146096) / 146097;
  const unsigned doe = (unsigned)(z - era * 146097);
  const unsigned yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
  const unsigned doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
  const unsigned mp = (5 * doy + 2) / 153;
  *d = doy - (153 * mp + 2) / 5 + 1;
  *m = mp < 10 ? mp + 3 : mp - 9;
  *y = (int)yoe + era * 400 + (*m <= 2);
}

bool parse_iso(const char *ts, uint32_t *epoch) {
  int y, mo, d, h, mi, s;
  if (!ts || sscanf(ts, "%d-%d-%dT%d:%d:%d", &y, &mo, &d, &h, &mi, &s) != 6) return false;
  if (y < 2020 || mo < 1 || mo > 12 || d < 1 || d > 31) return false;
  *epoch = (uint32_t)days_from_civil(y, mo, d) * 86400UL + h * 3600UL + mi * 60UL + s;
  return true;
}

void format_iso(uint32_t epoch, char out[20]) {
  int y; unsigned mo, d;
  civil_from_days(epoch / 86400, &y, &mo, &d);
  uint32_t t = epoch % 86400;
  // The % bounds only tell the compiler each field fits its width.
  snprintf(out, 20, "%04u-%02u-%02uT%02u:%02u:%02u", (unsigned)y % 10000u, mo % 100u, d % 100u,
           (unsigned)(t / 3600) % 100u, (unsigned)(t / 60 % 60), (unsigned)(t % 60));
}

static void clock_set_internal(uint32_t epoch) {
#ifdef BOARD_HAS_RTC
  rtc.setEpoch(epoch);
#else
  base_epoch = epoch;
  base_ms = millis();
#endif
  have_time = true;
}

void clock_begin() {
#ifdef BOARD_HAS_RTC
  rtc.begin();
#endif
#ifdef BOARD_HAS_PCF8523
  ext_rtc_ok = ext_rtc.begin();
  if (!ext_rtc_ok) {
    Serial.println(F("[CLOCK] RTC chip not found — waiting for the first poll."));
  } else if (ext_rtc.lostPower() || !ext_rtc.initialized()) {
    Serial.println(F("[CLOCK] RTC chip not set yet — waiting for the first poll."));
  } else {
    clock_set_internal(ext_rtc.now().unixtime());
    char ts[20];
    format_iso(clock_now(), ts);
    Serial.print(F("[CLOCK] Time from RTC chip: "));
    Serial.println(ts);
  }
#endif
}

static void clock_set(uint32_t epoch) {
  clock_set_internal(epoch);
#ifdef BOARD_HAS_PCF8523
  if (ext_rtc_ok) {
    bool unset = ext_rtc.lostPower() || !ext_rtc.initialized();
    int32_t drift = (int32_t)(ext_rtc.now().unixtime() - epoch);
    if (unset || drift > 2 || drift < -2) {
      // adjust() also turns on coin-cell backup (off at the chip's power-on default).
      ext_rtc.adjust(DateTime(epoch));
      ext_rtc.start();
      Serial.println(F("[CLOCK] RTC chip set from poll."));
    }
  }
#endif
}

bool clock_valid() { return have_time; }

uint32_t clock_now() {
#ifdef BOARD_HAS_RTC
  return rtc.getEpoch();
#else
  return base_epoch + (millis() - base_ms) / 1000;
#endif
}

// ── Command receive/dispatch ─────────────────────────────────────────────
bool LineReader::read(Stream &in, JsonDocument &out) {
  while (in.available()) {
    char c = in.read();
    if (c == '\n' || c == '\r') {
      bool complete = len > 0 && !overflow && line[0] == '{';
      size_t n = len;
      if (overflow && line[0] == '{') {
        Serial.println(F("[CMD] Dropped a command line too long for the buffer (LineReader::line)."));
      }
      len = 0;
      overflow = false;
      if (!complete) continue;
      DeserializationError err = deserializeJson(out, line, n);
      if (err) {
        Serial.print(F("[CMD] Could not parse command line: "));
        Serial.println(err.c_str());
        continue;
      }
      return true;
    }
    if (len < sizeof(line)) {
      line[len++] = c;
    } else {
      overflow = true;   // dropping a line too long to be a command
    }
  }
  return false;
}

bool check_for_usb_command(JsonDocument &out) {
  static LineReader usb_lines;
  return usb_lines.read(Serial, out);
}

bool check_for_command(RH_RF69 &radio, uint16_t timeout_ms, JsonDocument &out) {
  if (!radio.waitAvailableTimeout(timeout_ms)) {
    return false;
  }

  uint8_t buf[RH_RF69_MAX_MESSAGE_LEN];
  uint8_t len = sizeof(buf);
  if (!radio.recv(buf, &len)) {
    return false;
  }

  // Fragments of another node's oversized packet — never addressed to us.
  if (len > 0 && buf[0] == '~') return false;

  // JSON starts with "{"; anything else is MessagePack (radio_format).
  DeserializationError err = (len > 0 && buf[0] == '{') ? deserializeJson(out, (const char *)buf, len)
                                                        : deserializeMsgPack(out, (const char *)buf, len);
  if (err) {
    Serial.print(F("[CMD] Could not parse packet: "));
    Serial.println(err.c_str());
    return false;
  }
  return true;
}

static void handle_poll(JsonDocument &command, PacketSender &sender) {
  // The Pi stamps every poll with its own clock — use it both as this
  // batch's timestamp and to set the node's clock for the reading log.
  // Sends whatever the last timed sense cycle captured — matches the
  // Pico's send_latest(), which also doesn't force a fresh read on poll.
  const char *ts = command["ts"] | "unknown";
  // Every poll says how to send over the radio: "m" in its "lp" for
  // MessagePack, none for JSON. This answer already goes out that way.
  bool msgpack = strchr(command["lp"] | "", 'm') != nullptr;
  if (msgpack != radio_msgpack) {
    radio_msgpack = msgpack;
    Serial.println(msgpack ? F("[RADIO] From the Pi: sending MessagePack over the radio.")
                           : F("[RADIO] From the Pi: sending JSON over the radio."));
  }
  uint32_t epoch;
  if (parse_iso(ts, &epoch)) clock_set(epoch);
  send_latest(sender, ts);
  Serial.print(latest_count ? F("[POLL] Latest reading sent (ts=") : F("[POLL] Nothing to report, sent an empty batch (ts="));
  Serial.print(ts);
  Serial.println(F(")."));
}

static void handle_ping(JsonDocument &command, PacketSender &sender) {
  JsonDocument doc;
  doc["pq"] = command["q"];
  sender.send(doc, "pong");
}

static long handle_set_interval(JsonDocument &command, PacketSender &sender) {
  if (!command["v"].is<long>() || command["v"].as<long>() <= 0) {
    Serial.println(F("[INTERVAL] Invalid interval value — must be a positive integer."));
    return -1;
  }
  long seconds = command["v"].as<long>();
  delay(1000);

  JsonDocument doc;
  doc["v"] = seconds;
  sender.send(doc, "set_interval_ack");

  Serial.print(F("[INTERVAL] Sense interval updated to "));
  Serial.print(seconds);
  Serial.println(F("s."));
  return seconds * 1000L;
}

long dispatch_command(JsonDocument &command, PacketSender &sender,
                       RH_RF69 &radio, uint8_t node_id) {
  if (command.isNull()) return -1;

  // Commands are addressed via "n". Every node on this frequency/key hears
  // every packet — the Pi's commands to other nodes, other nodes' replies,
  // and their sync lines (which carry no "n" at all) — so anything not
  // addressed to us is ignored.
  if (command["n"].isNull() || command["n"].as<int>() != node_id) {
    return -1;
  }

  const char *t = command["t"] | "";

  // Give the Pi time to switch its radio from TX back to RX before we
  // answer. Compiled C++ replies within ~1ms — faster than the Pi's Python
  // loop gets back into receive() — so without this the first reply packet
  // (the poll's "ts" header, or a pong) is always lost. The CircuitPython
  // Pico never hit this because it's naturally slow enough to reply.
  delay(REPLY_DELAY_MS);

  if (strcmp(t, "poll") == 0) {
    handle_poll(command, sender);
  } else if (strcmp(t, "ping") == 0) {
    handle_ping(command, sender);
  } else if (strcmp(t, "sync") == 0) {
    node_log_sync(command, sender);
  } else if (strcmp(t, "info") == 0) {
    node_log_info(sender);
  } else if (strcmp(t, "set_interval") == 0) {
    return handle_set_interval(command, sender);
  } else if (strcmp(t, "sleep") == 0) {
    handle_sleep(command, sender);
  } else if (strcmp(t, "data_ack") == 0) {
    // The Pi's ack for a poll batch — nothing to do.
  } else {
    Serial.print(F("[CMD] Unknown packet type: "));
    Serial.println(t);
  }

  return -1;
}