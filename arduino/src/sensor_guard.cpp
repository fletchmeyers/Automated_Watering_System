#include "packet_protocol.h"
#include "sensor_guard.h"

// Then tried again after these, the last repeating.
static const uint16_t RETRY_MINUTES[] = {10, 30, 60};
static const uint8_t RETRY_STEPS = sizeof(RETRY_MINUTES) / sizeof(RETRY_MINUTES[0]);

bool sensor_due(const SensorEntry &sensor) {
  return !sensor.off || (int32_t)(clock_now() - sensor.retry_at) >= 0;
}

static void say(const SensorEntry &sensor, const __FlashStringHelper *what) {
  Serial.print(F("[SENSOR] "));
  Serial.print(sensor.tag);
  Serial.print(what);
}

static void say_why(bool ok, uint32_t took_ms) {
  if (!ok) {
    Serial.print(F("failed"));
  } else {
    Serial.print(F("took "));
    Serial.print(took_ms / 1000.0, 1);
    Serial.print(F(" s"));
  }
}

void sensor_result(SensorEntry &sensor, bool ok, uint32_t took_ms) {
  bool bad = !ok || took_ms > SENSOR_SLOW_MS;
  if (sensor.off) {
    if (!bad) {
      sensor.off = false;
      sensor.strikes = 0;
      say(sensor, F(" is answering again; reading it as normal.\n"));
      return;
    }
    if (sensor.tries < 255) sensor.tries++;
    uint16_t wait = RETRY_MINUTES[min((uint8_t)sensor.tries, (uint8_t)(RETRY_STEPS - 1))];
    sensor.retry_at = clock_now() + wait * 60UL;
    say(sensor, F(" still isn't answering properly ("));
    say_why(ok, took_ms);
    Serial.print(F("); trying again in "));
    Serial.print(wait);
    Serial.println(F(" min."));
    return;
  }
  if (!bad) {
    sensor.strikes = 0;
    return;
  }
  if (++sensor.strikes < SENSOR_STRIKES) return;
  sensor.off = true;
  sensor.tries = 0;
  sensor.retry_at = clock_now() + RETRY_MINUTES[0] * 60UL;
  say(sensor, F(" offline: "));
  Serial.print(SENSOR_STRIKES);
  Serial.print(F(" bad reads in a row (the last "));
  say_why(ok, took_ms);
  Serial.print(F("). Not reading it for "));
  Serial.print(RETRY_MINUTES[0]);
  Serial.println(F(" min, so the other sensors keep going; check its connector."));
}

void sensors_offline(char *buf, size_t len) {
  size_t used = 0;
  buf[0] = '\0';
  for (size_t i = 0; i < SENSOR_COUNT; i++) {
    if (!SENSOR_LIST[i].ok || !SENSOR_LIST[i].off) continue;
    int n = snprintf(buf + used, len - used, used ? ",%s" : "%s", SENSOR_LIST[i].tag);
    if (n < 0 || used + n >= len) break;   // out of room: the rest wait for the next poll
    used += n;
  }
}
