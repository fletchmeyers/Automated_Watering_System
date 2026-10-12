/*
 * arduino_node.ino
 *
 * Feather M0 + RFM69HCW FeatherWing sensor node.
 * Mirrors code.py's structure: read sensors on a timer, keep the latest
 * reading in memory, log a snapshot every LOG_INTERVAL_MS for the Pi to
 * pull with "sync" (see node_log.h), and listen for Pi commands
 * (poll/ping/sync/info/set_interval/sleep) at all times. A "sleep" command
 * switches the radio off until a wake time (see node_sleep.h).
 *
 * Requires libraries: RadioHead (RH_RF69), ArduinoJson (v6.x).
 */

#include <SPI.h>
#include <RH_RF69.h>
#include <ArduinoJson.h>
#include "board_config.h"
#include "packet_protocol.h"
#include "node_sleep.h"
#include "wifi_link.h"   // empty unless built with -D NODE_LINK_WIFI
#include "link_power.h"  // empty unless it has both Wi-Fi and the radio
#include "node_watchdog.h"

RH_RF69 rf69(RFM69_CS, RFM69_INT);
PacketSender sender(NODE_ID, &rf69);
PacketSender usb_sender(NODE_ID, &Serial);   // replies to commands that came over USB

bool radio_ok = false;
bool radio_on = true;   // false while battery saver has it switched off (link_power.h)
unsigned long sense_interval_ms = DEFAULT_SENSE_INTERVAL_MS;
unsigned long last_sense_at = 0;
unsigned long last_log_at = 0;

void setup() {
  Serial.begin(115200);
#ifdef BOARD_ESP32
  board_early_init();   // power the STEMMA QT port before the sensors are probed
#endif
  unsigned long serial_wait_start = millis();
  while (!Serial && millis() - serial_wait_start < 3000) {
    delay(10);
    }
  // Before the radio, sensors and SD card, so one that hangs while being
  // set up resets the board too (see node_watchdog.h); after the wait for
  // USB, so its messages can be seen.
  node_watchdog_begin();

#ifdef SD_CS
  // Deselect the SD card before the radio touches the shared SPI bus.
  pinMode(SD_CS, OUTPUT);
  digitalWrite(SD_CS, HIGH);
#endif
#ifdef BOARD_HAS_WINC1500
  // The Feather M0 WiFi's Wi-Fi module shares that bus too (CS on D8): keep
  // it deselected, whether or not this node uses Wi-Fi.
  pinMode(8, OUTPUT);
  digitalWrite(8, HIGH);
#endif

#if defined(NODE_HAS_BOTH_LINKS)
  // Wi-Fi and the radio: it answers each command over the link it came in
  // on, and the Pi decides which one it uses (links.py). Wi-Fi starts in
  // loop() if it's wanted (with battery saver it may not be: link_power.h).
  wifi_link_off();
  link_power_reset();
#elif defined(NODE_LINK_WIFI)
  wifi_link_begin();   // Wi-Fi only (wifi_link.h)
#endif
#ifdef NODE_NO_RADIO
  Serial.println(F("[BOOT] Wi-Fi only: no radio."));
#else
  pinMode(RFM69_RST, OUTPUT);
  digitalWrite(RFM69_RST, LOW);
  digitalWrite(RFM69_RST, HIGH);
  delay(10);
  digitalWrite(RFM69_RST, LOW);
  delay(10);

#if defined(RADIO_SPI_SCK) && defined(BOARD_RP2)
  // Radio on other pins than the board's default SPI ones (nodes.json).
  SPI.setSCK(RADIO_SPI_SCK);
  SPI.setTX(RADIO_SPI_MOSI);
  SPI.setRX(RADIO_SPI_MISO);
#elif defined(RADIO_SPI_SCK) && defined(BOARD_ESP32)
  // The radio library's own SPI.begin() then leaves these pins in place.
  SPI.begin(RADIO_SPI_SCK, RADIO_SPI_MISO, RADIO_SPI_MOSI);
#endif

  // Without a radio (a board on the bench) the node still runs and answers
  // over USB, so it can be set up and tested from the Pi.
  radio_ok = rf69.init();
  if (!radio_ok) {
    Serial.println(F("[ERROR] RFM69 init failed — check wiring/IRQ jumper. USB only."));
  } else {
    if (!rf69.setFrequency(RADIO_FREQ_MHZ)) {
      Serial.println(F("[ERROR] setFrequency failed."));
    }
    rf69.setEncryptionKey((uint8_t *)RADIO_ENCRYPT_KEY);
    rf69.setTxPower(20, true); // RFM69HCW maximum, same as the Pi and Pico
  }
#endif

  node_watchdog_feed();
  init_sensors();
  node_watchdog_feed();
  clock_begin();
  node_log_init();
  node_watchdog_feed();

  last_sense_at = millis() - sense_interval_ms; // sense immediately on first loop
  last_log_at = millis() - LOG_INTERVAL_MS;      // log as soon as the clock is valid
  Serial.println(F("[BOOT] Node ready."));
}

void loop() {
  node_watchdog_feed();
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

#ifdef NODE_HAS_BOTH_LINKS
  // ── Which links are powered (battery saver switches one off) ─────────
  bool want_radio, want_wifi;
  link_power_plan(want_radio, want_wifi);
  if (radio_ok && want_radio != radio_on) {
    if (!want_radio) rf69.sleep();   // the next available() switches it back on
    radio_on = want_radio;
    Serial.println(radio_on ? F("[LINKS] Radio on.") : F("[LINKS] Radio off (battery saver)."));
  }
  if (want_wifi != wifi_link_running()) {
    if (want_wifi) wifi_link_begin(); else wifi_link_off();
  }
#endif

  // ── Radio listen (short timeout so the sense loop stays on schedule) ──
  JsonDocument command;
  if (!radio_ok || !radio_on) {
    delay(100);   // USB only, or the radio's off: keep the loop's pace without its wait
  } else if (check_for_command(rf69, 100, command)) {
#ifdef NODE_HAS_BOTH_LINKS
    link_power_heard(LINK_RADIO, command);
#endif
    long new_interval_ms = dispatch_command(command, sender, rf69, NODE_ID);
    if (new_interval_ms > 0) {
      sense_interval_ms = new_interval_ms;
    }
  }

#ifdef NODE_LINK_WIFI
  // ── Wi-Fi commands from the Pi (replies go back over the same link) ───
  JsonDocument wifi_command;
  if (wifi_link_poll(wifi_command)) {
#ifdef NODE_HAS_BOTH_LINKS
    link_power_heard(LINK_WIFI, wifi_command);
#endif
    long new_interval_ms = dispatch_command(wifi_command, wifi_sender(), rf69, NODE_ID);
    if (new_interval_ms > 0) {
      sense_interval_ms = new_interval_ms;
    }
  }
#endif

  // ── USB commands (replies go back over USB, not the radio) ────────────
  JsonDocument usb_command;
  if (check_for_usb_command(usb_command)) {
    if (usb_command["n"].isNull()) usb_command["n"] = NODE_ID;  // over a cable, it can only be for us
#ifdef NODE_LINK_WIFI
    // Wi-Fi settings are only ever taken over the cable.
    if (strcmp(usb_command["t"] | "", "wifi_config") == 0) {
      handle_wifi_config(usb_command, usb_sender);
#ifdef NODE_HAS_BOTH_LINKS
      link_power_keep(LINK_WIFI);   // long enough for node_setup.py to see it connect
#endif
    } else
#endif
    {
      long new_interval_ms = dispatch_command(usb_command, usb_sender, rf69, NODE_ID);
      if (new_interval_ms > 0) {
        sense_interval_ms = new_interval_ms;
      }
    }
  }

  // ── Sleep, if the Pi asked for it (the ack has already gone out) ──────
  if (requested_wake) {
    uint32_t wake = requested_wake;
    requested_wake = 0;
#ifdef NODE_LINK_WIFI
    wifi_link_off();   // Wi-Fi sleeps too
#endif
    sleep_until(wake, rf69);
#if defined(NODE_HAS_BOTH_LINKS)
    link_power_reset();   // loop() switches the wanted links back on
    radio_on = true;
#elif defined(NODE_LINK_WIFI)
    wifi_link_begin();
#endif
  }
}