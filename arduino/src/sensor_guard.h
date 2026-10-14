/*
 * sensor_guard.h
 *
 * A safety net for sensors whose connections are going bad (corroded pins
 * on the I2C bus, say): the Arduino twin of circuitpython/sensor_guard.py.
 * A sensor whose read fails, or takes longer than SENSOR_SLOW_MS,
 * SENSOR_STRIKES times in a row is taken offline: run_sense_cycle() stops
 * reading it, so it can't hold up the sensors that still work (or the
 * radio), and tries it again after 10 minutes, 30, then every hour. One
 * good read brings it back; a restart tries everything again too.
 *
 * Retry times are on clock_now() (seconds), which keeps counting through
 * the M0's standby sleep, unlike millis().
 *
 * The offline sensors go to the Pi with every poll ({"t":"off","s":
 * "sht,s1"}; see send_latest()), and the dashboard marks their cards.
 */

#ifndef SENSOR_GUARD_H
#define SENSOR_GUARD_H

#include <Arduino.h>

struct SensorEntry;

#define SENSOR_SLOW_MS 1000   // a read taking longer than this counts as a bad one
#define SENSOR_STRIKES 3      // bad reads in a row before a sensor is taken offline

// True if this sensor should be read this time round.
bool sensor_due(const SensorEntry &sensor);

// How a read went: ok (the sensor answered sensibly) and how long it took.
// Says on Serial when the sensor goes offline, stays offline after a
// retry, or comes back.
void sensor_result(SensorEntry &sensor, bool ok, uint32_t took_ms);

// The offline sensors' tags, comma-separated, into buf ("" if none).
void sensors_offline(char *buf, size_t len);

#endif
