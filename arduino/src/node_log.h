/*
 * node_log.h
 *
 * On-node reading log for sync — the Arduino equivalent of the Pico's
 * data.txt on SD. A snapshot of the latest readings is added every
 * LOG_INTERVAL_MS, and the Pi pulls the log back with the same "sync"
 * requests it sends the Pico (see send_sync_chunk() in communication_garden.py).
 *
 * Each board picks where the log lives in its board_config_*.h:
 *   #define LOG_BACKEND LOG_SD           // microSD card, same file layout as the Pico
 *   #define LOG_BACKEND LOG_FLASH_SAMD   // spare internal flash, SAMD21 boards (see flash_log.h)
 *   #define LOG_BACKEND LOG_NONE         // no storage — sync always answers 0 lines
 * Boards that don't define LOG_BACKEND get LOG_NONE.
 */

#ifndef NODE_LOG_H
#define NODE_LOG_H

#include <Arduino.h>
#include <ArduinoJson.h>

#define LOG_NONE       0
#define LOG_FLASH_SAMD 1
#define LOG_SD         2

#ifndef LOG_BACKEND
  #define LOG_BACKEND LOG_NONE
#endif

class PacketSender;

// Set up the log at boot. Returns false if there's no usable log.
bool node_log_init();

// Add the current latest_readings, stamped with epoch (see clock_now()).
void node_log_snapshot(uint32_t epoch);

// Answer one "sync" request: up to k lines, then an "se" packet.
void node_log_sync(JsonDocument &command, PacketSender &sender);

// Answer an "info" request: log bytes used (ub), free (fb) and total (tb).
void node_log_info(PacketSender &sender);

#endif
