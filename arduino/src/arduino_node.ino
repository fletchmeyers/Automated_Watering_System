/*
 * arduino_node.ino
 *
 * Feather M0 + RFM69HCW FeatherWing sensor node.
 * Mirrors code.py's structure: read sensors on a timer, keep the latest
 * reading in memory, log a snapshot every LOG_INTERVAL_MS for the Pi to
 * pull with "sync" (see node_log.h), and listen for Pi commands
 * (poll/ping/sync/info/set_interval) at all times.
 *
 * Requires libraries: RadioHead (RH_RF69), ArduinoJson (v6.x).
 */

#include <SPI.h>
#include <RH_RF69.h>
#include <ArduinoJson.h>
#include "board_config.h"
#include "packet_protocol.h"

RH_RF69 rf69(RFM69_CS, RFM69_INT);
PacketSender sender(NODE_ID, &rf69);

unsigned long sense_interval_ms = DEFAULT_SENSE_INTERVAL_MS;
unsigned long last_sense_at = 0;
unsigned long last_log_at = 0;

void setup() {
  Serial.begin(115200);
  unsigned long serial_wait_start = millis();
  while (!Serial && millis() - serial_wait_start < 3000) {
    delay(10);
    }

#ifdef SD_CS
  // Deselect the SD card before the radio touches the shared SPI bus.
  pinMode(SD_CS, OUTPUT);
  digitalWrite(SD_CS, HIGH);
#endif

  pinMode(RFM69_RST, OUTPUT);
  digitalWrite(RFM69_RST, LOW);
  digitalWrite(RFM69_RST, HIGH);
  delay(10);
  digitalWrite(RFM69_RST, LOW);
  delay(10);

  if (!rf69.init()) {
    Serial.println(F("[ERROR] RFM69 init failed — check wiring/IRQ jumper."));
    while (1) delay(1000);
  }
  if (!rf69.setFrequency(RADIO_FREQ_MHZ)) {
    Serial.println(F("[ERROR] setFrequency failed."));
  }
  rf69.setEncryptionKey((uint8_t *)RADIO_ENCRYPT_KEY);
  rf69.setTxPower(20, true); // RFM69HCW maximum, same as the Pi and Pico

  init_sensors();
  clock_begin();
  node_log_init();

  last_sense_at = millis() - sense_interval_ms; // sense immediately on first loop
  last_log_at = millis() - LOG_INTERVAL_MS;      // log as soon as the clock is valid
  Serial.println(F("[BOOT] Node ready."));
}

void loop() {
  unsigned long now = millis();

  // ── Sense cycle ──────────────────────────────────────────────────────
  if (now - last_sense_at >= sense_interval_ms) {
    last_sense_at = now;
    run_sense_cycle();
  }

  // ── Log cycle (waits until the clock is valid) ───────────────────────
  if (clock_valid() && now - last_log_at >= LOG_INTERVAL_MS) {
    last_log_at = now;
    node_log_snapshot(clock_now());
  }

  // ── Radio listen (short timeout so the sense loop stays on schedule) ──
  JsonDocument command;
  if (check_for_command(rf69, 100, command)) {
    long new_interval_ms = dispatch_command(command, sender, rf69, NODE_ID);
    if (new_interval_ms > 0) {
      sense_interval_ms = new_interval_ms;
    }
  }
}