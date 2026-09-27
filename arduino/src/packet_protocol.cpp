#include "packet_protocol.h"

// ── Sensor list ──────────────────────────────────────────────────────────
// Board-specific entries live here (not in the header) since this is where
// SENSOR_COUNT gets computed. Add new sensors from board_config_feather_m0.h
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

SensorEntry SENSOR_LIST[] = {
  { "vbat", vbat_init,   vbat_read,   false },
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

void run_sense_cycle() {
  latest_count = 0;
  for (size_t i = 0; i < SENSOR_COUNT && latest_count < MAX_SENSORS; i++) {
    if (!SENSOR_LIST[i].ok) continue;

    latest_readings[latest_count].clear();
    JsonObject obj = latest_readings[latest_count].to<JsonObject>();
    SENSOR_LIST[i].read_fn(obj);
    latest_tags[latest_count] = SENSOR_LIST[i].tag;
    latest_count++;
  }
}

void send_latest(PacketSender &sender, const char *timestamp) {
  if (latest_count == 0) {
    Serial.println(F("[POLL] No reading available yet, skipping."));
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

  sender.send_batch_end(latest_count, sent);
}

// ── Clock ────────────────────────────────────────────────────────────────
// Set from the "ts" in every poll. Times are the Pi's local wall-clock time
// counted as seconds since 1970, and only ever turned back into the same
// "YYYY-MM-DDTHH:MM:SS" form — no time zones involved. Boards without an
// RTC fall back to millis(), which is fine while the node never sleeps.

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

static bool parse_iso(const char *ts, uint32_t *epoch) {
  int y, mo, d, h, mi, s;
  if (!ts || sscanf(ts, "%d-%d-%dT%d:%d:%d", &y, &mo, &d, &h, &mi, &s) != 6) return false;
  if (y < 2020 || mo < 1 || mo > 12 || d < 1 || d > 31) return false;
  *epoch = (uint32_t)days_from_civil(y, mo, d) * 86400UL + h * 3600UL + mi * 60UL + s;
  return true;
}

static void format_iso(uint32_t epoch, char out[20]) {
  int y; unsigned mo, d;
  civil_from_days(epoch / 86400, &y, &mo, &d);
  uint32_t t = epoch % 86400;
  // The % bounds only tell the compiler each field fits its width.
  snprintf(out, 20, "%04u-%02u-%02uT%02u:%02u:%02u", (unsigned)y % 10000u, mo % 100u, d % 100u,
           (unsigned)(t / 3600) % 100u, (unsigned)(t / 60 % 60), (unsigned)(t % 60));
}

void clock_begin() {
#ifdef BOARD_HAS_RTC
  rtc.begin();
#endif
}

static void clock_set(uint32_t epoch) {
#ifdef BOARD_HAS_RTC
  rtc.setEpoch(epoch);
#else
  base_epoch = epoch;
  base_ms = millis();
#endif
  have_time = true;
}

bool clock_valid() { return have_time; }

uint32_t clock_now() {
#ifdef BOARD_HAS_RTC
  return rtc.getEpoch();
#else
  return base_epoch + (millis() - base_ms) / 1000;
#endif
}

// ── Reading log ──────────────────────────────────────────────────────────
// One record per sensor per snapshot, stored as the reading's JSON (with
// "t" first) and the snapshot time. The time is added back as "ts" when
// the record is synced, so records look exactly like the Pico's SD lines.

void log_latest_readings() {
  if (!clock_valid() || latest_count == 0 || log_capacity() == 0) return;

  uint32_t now = clock_now();
  size_t logged = 0;
  for (size_t i = 0; i < latest_count; i++) {
    JsonDocument rec;
    rec["t"] = latest_tags[i];
    for (JsonPairConst kv : latest_readings[i].as<JsonObjectConst>()) {
      rec[kv.key()] = kv.value();
    }
    if (measureJson(rec) > LOG_JSON_MAX) {
      Serial.print(F("[LOG] Reading too long to log: "));
      Serial.println(latest_tags[i]);
      continue;
    }
    char buf[LOG_JSON_MAX + 1];
    size_t len = serializeJson(rec, buf, sizeof(buf));
    if (log_append(now, buf, len)) logged++;
  }

  Serial.print(F("[LOG] Logged "));
  Serial.print(logged);
  Serial.print(F(" readings ("));
  Serial.print(log_head() - log_tail());
  Serial.print(F("/"));
  Serial.print(log_capacity());
  Serial.println(F(" records waiting)."));
}

// ── Command receive/dispatch ─────────────────────────────────────────────
bool check_for_command(RH_RF69 &radio, uint16_t timeout_ms, JsonDocument &out) {
  if (!radio.waitAvailableTimeout(timeout_ms)) {
    return false;
  }

  uint8_t buf[RH_RF69_MAX_MESSAGE_LEN];
  uint8_t len = sizeof(buf);
  if (!radio.recv(buf, &len)) {
    return false;
  }

  DeserializationError err = deserializeJson(out, (const char *)buf, len);
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
  uint32_t epoch;
  if (parse_iso(ts, &epoch)) clock_set(epoch);
  send_latest(sender, ts);
  Serial.print(F("[POLL] Latest reading sent (ts="));
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

// Answer one Pi "sync" request — same protocol as send_sync_chunk() on the
// Pico, with record numbers as offsets. Request {"t":"sync","g":..,"o":..,"k":..}:
// g/o confirm the Pi has stored everything before record o. Reply: up to k
// records as JSON lines, then {"t":"se","g":..,"o":<next>,"c":<sent>,"m":<more>}.
// With no log (LOG_NONE) this always answers 0 records.
static void handle_sync(JsonDocument &command, PacketSender &sender) {
  uint16_t g = log_format_id();
  if (command["g"].as<long>() == g && command["o"].is<uint32_t>()) {
    log_confirm(command["o"].as<uint32_t>());
  }
  uint32_t k = command["k"] | 8;

  uint32_t i = log_tail(), sent = 0, epoch;
  char json[LOG_JSON_MAX + 1], ts[20], line[LOG_JSON_MAX + 32];
  while (sent < k && i < log_head()) {
    if (log_read(i, &epoch, json)) {
      format_iso(epoch, ts);
      int len = snprintf(line, sizeof(line), "%.*s,\"ts\":\"%s\"}",
                         (int)strlen(json) - 1, json, ts);
      sender.send_raw((const uint8_t *)line, len);
      delay(SYNC_LINE_GAP_MS);
      sent++;
    }
    i++;
  }

  JsonDocument doc;
  doc["g"] = g;
  doc["o"] = i;
  doc["c"] = sent;
  doc["m"] = i < log_head() ? 1 : 0;
  sender.send(doc, "se");

  Serial.print(F("[SYNC] Sent "));
  Serial.print(sent);
  Serial.print(F(" records ("));
  Serial.print(log_head() - i);
  Serial.println(F(" left)."));
}

// Report log usage so the Pi can show it: bytes used, free and total.
static void handle_info(PacketSender &sender) {
  uint32_t rb = log_record_bytes();
  uint32_t used = (log_head() - log_tail()) * rb;
  uint32_t total = log_capacity() * rb;
  JsonDocument doc;
  doc["ub"] = used;
  doc["fb"] = total - used;
  doc["tb"] = total;
  sender.send(doc, "info");
}

long dispatch_command(JsonDocument &command, PacketSender &sender,
                       RH_RF69 &radio, uint8_t node_id) {
  if (command.isNull()) return -1;

  // Commands are addressed via "n". With more than one radio node sharing
  // this frequency/encryption key, every node hears every command — ignore
  // anything not addressed to us. Missing "n" is accepted for backward
  // compatibility with hand-crafted single-node testing.
  if (!command["n"].isNull() && command["n"].as<int>() != node_id) {
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
    handle_sync(command, sender);
  } else if (strcmp(t, "info") == 0) {
    handle_info(sender);
  } else if (strcmp(t, "set_interval") == 0) {
    return handle_set_interval(command, sender);
  } else if (strcmp(t, "data_ack") == 0) {
    // The Pi's ack for a poll batch — nothing to do.
  } else {
    Serial.print(F("[CMD] Unknown packet type: "));
    Serial.println(t);
  }

  return -1;
}