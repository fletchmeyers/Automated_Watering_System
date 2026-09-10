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
 *   exp/snt/chk/tot = batch_end fields (expected/sent/chunk/total)
 *   pq  = ping's q, echoed back in pong
 */

#ifndef PACKET_PROTOCOL_H
#define PACKET_PROTOCOL_H

#include <Arduino.h>
#include <ArduinoJson.h>
#include <RH_RF69.h>
#include "board_config.h" 

// The sensor list itself is board-specific data, defined in the .cpp file
// alongside SENSOR_COUNT so dispatch/sense code here can stay generic.
extern SensorEntry SENSOR_LIST[];
extern const size_t SENSOR_COUNT;

// ── PacketSender ─────────────────────────────────────────────────────────
// Equivalent of communication_garden.py's PacketSender class.
class PacketSender {
  public:
    PacketSender(uint8_t node_id, RH_RF69 *radio)
      : node_id(node_id), radio(radio), sequence(0) {}

    // Sends any JsonDocument that already has its sensor-specific fields
    // set; this stamps t/q/n and increments the sequence counter.
    void send(JsonDocument &doc, const char *type_tag) {
      doc["t"] = type_tag;
      doc["q"] = sequence++;
      doc["n"] = node_id;

      char buf[RH_RF69_MAX_MESSAGE_LEN];
      size_t len = serializeJson(doc, buf, sizeof(buf));

      if (len >= sizeof(buf)) {
        Serial.print(F("[ERROR] Packet too large, not sent: "));
        Serial.println(type_tag);
        return;
      }
      radio->send((uint8_t *)buf, len);
      radio->waitPacketSent();
    }

    void send_batch_end(uint8_t expected, uint8_t sent,
                         int chunk = -1, int total = -1) {
      JsonDocument doc;
      doc["exp"] = expected;
      doc["snt"] = sent;
      if (chunk >= 0) doc["chk"] = chunk;
      if (total >= 0) doc["tot"] = total;
      send(doc, "batch_end");
    }

    uint8_t node_id;

  private:
    RH_RF69 *radio;
    uint16_t sequence;
};

// ── Latest reading buffer ────────────────────────────────────────────────
// Equivalent of latest_reading / store_latest_reading() on the Pico —
// overwritten each sense cycle, sent back in response to a poll.
#define MAX_SENSORS 12
extern JsonDocument latest_readings[MAX_SENSORS];
extern const char *latest_tags[MAX_SENSORS];
extern size_t latest_count;

void init_sensors();
void run_sense_cycle();
void send_latest(PacketSender &sender, const char *timestamp);

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