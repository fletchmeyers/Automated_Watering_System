/*
 * packet_protocol.h
 *
 * Board-agnostic radio protocol: packet building/sending, command dispatch.
 * Mirrors communication_garden.py + sync_garden.py from the CircuitPython
 * node. This file should compile unchanged on any board — everything
 * board-specific lives in board_config_*.h.
 *
 * Packet key reference (must match the Pico node / Pi side exactly):
 *   t   = type/sensor tag        q    = sequence number
 *   n   = node ID                ts   = ISO timestamp
 *   v   = voltage / set_interval value
 *   exp/snt = batch_end fields (expected/sent)
 *   pq  = ping's q, echoed back in pong
 *   g/o/c/m/k = sync fields (log format ID / record offset / count / more / max)
 *   j   = sync line's place in its chunk; in a request, a bitmask of the
 *         lines wanted (bit i = line i), so a retry only resends what's missing
 *   ub/fb/tb  = info fields (log bytes used / free / total)
 *   w/ok/why  = sleep fields (wake time / accepted / reason refused)
 */

#ifndef PACKET_PROTOCOL_H
#define PACKET_PROTOCOL_H

#include <Arduino.h>
#include <ArduinoJson.h>
#include <RH_RF69.h>
#include "board_config.h"
#include "node_log.h"

#ifndef LOG_INTERVAL_MS
  #define LOG_INTERVAL_MS (5UL * 60 * 1000)
#endif

// The sensor list itself is board-specific data, defined in the .cpp file
// alongside SENSOR_COUNT so dispatch/sense code here can stay generic.
extern SensorEntry SENSOR_LIST[];
extern const size_t SENSOR_COUNT;

// ── PacketSender ─────────────────────────────────────────────────────────
// Equivalent of communication_garden.py's PacketSender class.
class PacketSender {
  public:
    PacketSender(uint8_t node_id, RH_RF69 *radio)
      : node_id(node_id), radio(radio), sequence(0), fragment_id(0) {}

    // Sends any JsonDocument that already has its sensor-specific fields
    // set; this stamps t/q/n and increments the sequence counter.
    void send(JsonDocument &doc, const char *type_tag) {
      doc["t"] = type_tag;
      doc["q"] = sequence++;
      doc["n"] = node_id;

      char buf[MAX_PAYLOAD_BYTES];
      size_t len = serializeJson(doc, buf, sizeof(buf));
      if (len >= sizeof(buf)) {
        Serial.print(F("[ERROR] Packet too large, not sent: "));
        Serial.println(type_tag);
        return;
      }
      send_raw((const uint8_t *)buf, len);
    }

    // Sends already-encoded bytes, split into "~<node>.<msg>.<i>.<k>|"
    // fragments if they don't fit in one radio packet — same format as
    // PacketSender.send_raw() on the Pico, reassembled by the Pi's
    // FragmentReassembler.
    void send_raw(const uint8_t *data, size_t len) {
      if (len <= RH_RF69_MAX_MESSAGE_LEN) {
        radio->send(data, len);
        radio->waitPacketSent();
        return;
      }
      uint8_t parts = (len + FRAGMENT_BODY_BYTES - 1) / FRAGMENT_BODY_BYTES;
      uint16_t msg = fragment_id;
      fragment_id = (fragment_id + 1) % 1000;
      for (uint8_t i = 0; i < parts; i++) {
        uint8_t pkt[RH_RF69_MAX_MESSAGE_LEN];
        int header = snprintf((char *)pkt, sizeof(pkt), "~%u.%u.%u.%u|", node_id, msg, i, parts);
        size_t offset = (size_t)i * FRAGMENT_BODY_BYTES;
        size_t body = min((size_t)FRAGMENT_BODY_BYTES, len - offset);
        memcpy(pkt + header, data + offset, body);
        radio->send(pkt, header + body);
        radio->waitPacketSent();
        if (i < parts - 1) delay(FRAGMENT_GAP_MS);
      }
    }

    void send_batch_end(uint8_t expected, uint8_t sent) {
      JsonDocument doc;
      doc["exp"] = expected;
      doc["snt"] = sent;
      send(doc, "batch_end");
    }

    uint8_t node_id;

  private:
    // Fragments carry up to 45 bytes each, leaving 15 for the header.
    static const size_t MAX_PAYLOAD_BYTES   = 128;
    static const size_t FRAGMENT_BODY_BYTES = 45;
    static const uint16_t FRAGMENT_GAP_MS   = 100;

    RH_RF69 *radio;
    uint16_t sequence;
    uint16_t fragment_id;
};

// ── Latest reading buffer ────────────────────────────────────────────────
// Equivalent of latest_reading / store_latest_reading() on the Pico —
// overwritten each sense cycle, sent back in response to a poll.
#define MAX_SENSORS 12

// Pause before replying to any command — see dispatch_command().
#define REPLY_DELAY_MS 50

// Pause between records during a sync chunk — same spacing as the Pico.
#define SYNC_LINE_GAP_MS 100
extern JsonDocument latest_readings[MAX_SENSORS];
extern const char *latest_tags[MAX_SENSORS];
extern size_t latest_count;

void init_sensors();
void run_sense_cycle();
void send_latest(PacketSender &sender, const char *timestamp);

// ── Clock ────────────────────────────────────────────────────────────────
// Kept in the Pi's local wall-clock time. Set from every poll; a board with
// a battery-backed RTC chip (BOARD_HAS_PCF8523) also has it at boot.
// Nothing is logged until the clock is valid.
void clock_begin();
bool clock_valid();
uint32_t clock_now();                        // seconds since 1970
void format_iso(uint32_t epoch, char out[20]);  // "YYYY-MM-DDTHH:MM:SS"
bool parse_iso(const char *ts, uint32_t *epoch);  // false if ts isn't that form

// ── Command handling ─────────────────────────────────────────────────────
// Returns a parsed JsonDocument if a valid command packet was received,
// or an empty/null document otherwise. Non-blocking beyond `timeout_ms`.
bool check_for_command(RH_RF69 &radio, uint16_t timeout_ms, JsonDocument &out);

// Returns a new sense-interval in ms if a set_interval command changed it,
// or -1 otherwise. node_id is this node's own ID — commands addressed to a
// different node (shared radio/key with other nodes, e.g. the Pico) are
// silently ignored here, mirroring the same check added to sync_garden.py.
long dispatch_command(JsonDocument &command, PacketSender &sender,
                       RH_RF69 &radio, uint8_t node_id);

#endif