/*
 * flash_log.h
 *
 * The LOG_FLASH_SAMD backend for node_log: readings stored as numbered
 * records in the SAMD21's spare internal flash, for boards without an SD
 * card. The Pi pulls them with the same "sync" requests it sends the Pico,
 * using the record number as the sync offset.
 *
 * Note: a firmware upload (bossac --erase) wipes the whole flash, log
 * included. Resets and power loss don't.
 */

#ifndef FLASH_LOG_H
#define FLASH_LOG_H

#include <Arduino.h>

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
