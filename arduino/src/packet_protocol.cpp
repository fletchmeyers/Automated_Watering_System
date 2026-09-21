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
  // No RTC on this node — the Pi already stamps every poll with its own
  // clock, and the Pico's RTC round-trip just returns that same value.
  // We can use it directly instead.
  // Sends whatever the last timed sense cycle captured — matches the
  // Pico's send_latest(), which also doesn't force a fresh read on poll.
  const char *ts = command["ts"] | "unknown";
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

static void handle_sync_stub(PacketSender &sender) {
  // No SD card on this node yet — reply immediately so the Pi's
  // CommandManager doesn't sit waiting the full timeout for a node that
  // can't do bulk sync. Swap this out once SD support is added.
  JsonDocument doc;
  doc["chunks"] = 0;
  sender.send(doc, "sync_complete");
  Serial.println(F("[SYNC] sync_request received but no SD configured — replied with 0 chunks."));
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

  if (strcmp(t, "poll") == 0) {
    handle_poll(command, sender);
  } else if (strcmp(t, "ping") == 0) {
    handle_ping(command, sender);
  } else if (strcmp(t, "sync_request") == 0) {
    handle_sync_stub(sender);
  } else if (strcmp(t, "set_interval") == 0) {
    return handle_set_interval(command, sender);
  } else if (strcmp(t, "data_ack") == 0) {
    // No-op here — this node doesn't do chunked bulk sync yet.
  } else {
    Serial.print(F("[CMD] Unknown packet type: "));
    Serial.println(t);
  }

  return -1;
}