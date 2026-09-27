/*
 * flash_log.h
 *
 * On-node reading log for sync — the Arduino equivalent of the Pico's
 * data.txt on SD. Readings are stored as records numbered 0, 1, 2, ...;
 * the Pi pulls them with the same "sync" requests it sends the Pico, using
 * the record number as the sync offset.
 *
 * Which backend (if any) a board uses is chosen in its board_config_*.h:
 *   #define LOG_BACKEND LOG_FLASH_SAMD   // internal flash, SAMD21 boards
 *   #define LOG_BACKEND LOG_NONE         // no storage — sync always reports 0 records
 * Boards that don't define LOG_BACKEND get LOG_NONE.
 */

#ifndef FLASH_LOG_H
#define FLASH_LOG_H

#include <Arduino.h>

#define LOG_NONE       0
#define LOG_FLASH_SAMD 1

#ifndef LOG_BACKEND
  #define LOG_BACKEND LOG_NONE
#endif

// Longest reading JSON a record can hold (the record's timestamp is kept
// separately and added back when the record is sent).
#define LOG_JSON_MAX 55

// Scan the log area and find the oldest/newest records. Formats the area on
// first use. Returns false if there's no usable log (LOG_NONE, or the
// firmware has grown into the log area).
bool log_init();

// Append one reading. Once the log is full, the oldest records are
// overwritten.
bool log_append(uint32_t epoch, const char *json, uint8_t len);

// Read record `index` into json (LOG_JSON_MAX + 1 bytes, null-terminated).
// Returns false if that record has been overwritten or never existed.
bool log_read(uint32_t index, uint32_t *epoch, char *json);

// The Pi has stored everything before `index` — it may be discarded.
void log_confirm(uint32_t index);

uint32_t log_tail();        // oldest record not yet confirmed by the Pi
uint32_t log_head();        // number the next record will get
uint32_t log_capacity();    // records the log can hold
uint32_t log_record_bytes();
uint16_t log_format_id();   // changes whenever the log area is reformatted

#endif
