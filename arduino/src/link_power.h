/*
 * link_power.h
 *
 * Which links a node with both the radio and Wi-Fi keeps powered.
 *
 * Normally both are on, and the Pi uses whichever it prefers (links.py).
 * With battery saver (nodes.json "battery_saver": true; -D BATTERY_SAVER)
 * only the preferred link stays on, since Wi-Fi in particular draws far more
 * than a listening radio:
 *   - Radio first: Wi-Fi stays off. With "wifi_check_hours" (-D
 *     WIFI_CHECK_HOURS=N) it comes on for CHECK_MINUTES every N hours, and
 *     stays on while the Pi uses it (that means the radio is down).
 *   - Wi-Fi first: the radio comes on once nothing has come over Wi-Fi for
 *     SILENCE_MINUTES (listening is cheap), and goes off again once the Pi
 *     is back on Wi-Fi.
 * Only real commands count as hearing the Pi on a link; the Wi-Fi service's
 * "still there?" pings don't keep one on. Polls carry the Pi's preferred
 * link ("pf"), so node_setup.py prefer switches it without a re-flash; until
 * the first poll the preference is the one it was built with
 * (-D LINK_FIRST_WIFI, or the radio).
 */

#ifndef LINK_POWER_H
#define LINK_POWER_H

#if defined(NODE_LINK_WIFI) && !defined(NODE_NO_RADIO)
#define NODE_HAS_BOTH_LINKS

#include <ArduinoJson.h>

enum NodeLink { LINK_RADIO = 0, LINK_WIFI = 1 };

// A command came in over link (call before acting on it).
void link_power_heard(NodeLink link, JsonDocument &command);

// After a sleep, or at boot: start counting quiet time from now.
void link_power_reset();

// Keep link on for a few minutes as if the Pi were using it (after new
// Wi-Fi settings, so node_setup.py can see it connect).
void link_power_keep(NodeLink link);

// Which links should be powered right now.
void link_power_plan(bool &radio_on, bool &wifi_on);

#endif
#endif
