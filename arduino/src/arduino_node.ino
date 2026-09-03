/*
 * arduino_node.ino
 *
 * Feather M0 + RFM69HCW FeatherWing sensor node.
 * Mirrors code.py's structure: read sensors on a timer, keep the latest
 * reading in memory, and listen for Pi commands (poll/ping/set_interval)
 * at all times. No SD card yet, so no bulk sync — see packet_protocol.cpp's
 * handle_sync_stub() for how that's handled gracefully in the meantime.
 *
 * Requires libraries: RadioHead (RH_RF69), ArduinoJson (v6.x).
 */

#include <SPI.h>
#include <RH_RF69.h>
#include <ArduinoJson.h>
#include "board_config_feather_m0.h"
#include "packet_protocol.h"

RH_RF69 rf69(RFM69_CS, RFM69_INT);
PacketSender sender(NODE_ID, &rf69);

unsigned long sense_interval_ms = DEFAULT_SENSE_INTERVAL_MS;
unsigned long last_sense_at = 0;

void setup() {
  Serial.begin(115200);
  unsigned long serial_wait_start = millis();
  while (!Serial && millis() - serial_wait_start < 3000) {
    delay(10);
    }

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
  rf69.setTxPower(13, true); // matches rfm69.tx_power = 13 on the Pico node

  init_sensors();

  last_sense_at = millis() - sense_interval_ms; // sense immediately on first loop
  Serial.println(F("[BOOT] Node ready."));
}

void loop() {
  unsigned long now = millis();

  // ── Sense cycle ──────────────────────────────────────────────────────
  if (now - last_sense_at >= sense_interval_ms) {
    last_sense_at = now;
    run_sense_cycle();
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