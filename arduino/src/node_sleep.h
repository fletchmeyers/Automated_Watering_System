/*
 * node_sleep.h
 *
 * Deep sleep on the Pi's command, to get a battery node through the night.
 * The Pi sends {"t":"sleep","n":..,"w":"YYYY-MM-DDTHH:MM:SS"}; the node
 * answers with sleep_ack, switches its radio off and sleeps until w, waking
 * every LOG_INTERVAL_MS to take a reading and log it (see node_log.h) so the
 * night's data can be synced the next day. The wake time is absolute rather
 * than a duration, so a retried command can't push it later.
 *
 * With a clock that keeps running in sleep and can raise an alarm — the
 * SAMD21's RTC (BOARD_HAS_RTC) — the chip itself sleeps between readings.
 * Boards without one (RP2040/RP2350) switch the radio off but stay awake.
 */

#ifndef NODE_SLEEP_H
#define NODE_SLEEP_H

#include <Arduino.h>
#include <ArduinoJson.h>
#include <RH_RF69.h>

class PacketSender;

// Longest sleep a node will accept, as a guard against a bad wake time.
#ifndef SLEEP_MAX_S
  #define SLEEP_MAX_S (16UL * 3600)
#endif

// Set by handle_sleep() when a sleep was accepted — the main loop sleeps
// until this time (clock_now() seconds), then clears it.
extern uint32_t requested_wake;

// Answer a "sleep" command with sleep_ack (ok 1 or 0, plus the reason if
// refused), and set requested_wake if accepted.
void handle_sleep(JsonDocument &command, PacketSender &sender);

// Radio off, then sleep until wake, logging a reading every LOG_INTERVAL_MS.
// Returns once wake is reached, with the radio ready to be used again.
void sleep_until(uint32_t wake, RH_RF69 &radio);

#endif
