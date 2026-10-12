/*
 * node_watchdog.h
 *
 * The chip's watchdog, so a node out in the garden heals itself: if the
 * code stops feeding it for WATCHDOG_MS (a sensor or bus that hangs, say
 * from corroded pins, or a crash), the board resets and starts again. The
 * Arduino twin of circuitpython/node_watchdog.py.
 *
 * It starts first thing in setup(), before the sensors and SD card, and is
 * fed on every pass of loop(), with every packet sent (PacketSender), and
 * before anything that waits (joining Wi-Fi, reaching the Pi). The M0 gets
 * 16 s, since its Wi-Fi module can take 8 s to join; the others 8 s. The
 * M0's standby sleep lasts minutes between readings, so it pauses the
 * watchdog for standby and resumes it on waking; the other boards stay
 * awake while "asleep" and keep feeding it.
 *
 * Build with -D NO_WATCHDOG to leave it off (bench debugging). Uses the
 * Adafruit SleepyDog library.
 */

#ifndef NODE_WATCHDOG_H
#define NODE_WATCHDOG_H

// Start it; says on Serial if the last reset was the watchdog's.
void node_watchdog_begin();

// Tell it the code is still running.
void node_watchdog_feed();

// Around a long standby sleep (the M0's); the other boards keep feeding.
void node_watchdog_pause();
void node_watchdog_resume();

#endif
