#include <Arduino.h>
#include "link_power.h"

#ifdef NODE_HAS_BOTH_LINKS

// What it was built with (nodes.json at the last flash). The Pi's polls
// carry the current settings ("lp", see link_power.h), which replace these
// from the first poll on, so nodes.json can change without a re-flash.
#ifndef WIFI_CHECK_HOURS
  #define WIFI_CHECK_HOURS 0   // radio first: never turn Wi-Fi on to check it
#endif

static const unsigned long SILENCE_MINUTES = 5;   // primary quiet this long: (cheap) radio backup on
static const unsigned long KEEP_MINUTES = 5;      // a backup the Pi is using stays on this long after
static const unsigned long CHECK_MINUTES = 5;     // how long a Wi-Fi check lasts

static NodeLink preferred =
#ifdef LINK_FIRST_WIFI
  LINK_WIFI;
#else
  LINK_RADIO;
#endif
static bool saver =
#ifdef BATTERY_SAVER
  true;
#else
  false;
#endif
static unsigned long check_hours = WIFI_CHECK_HOURS;

static unsigned long heard_at[2] = {0, 0};    // last real command over each link (millis)
static bool heard[2] = {false, false};

// "lp" from a poll: r or w (which link first), capital for battery saver,
// then the Wi-Fi check interval in hours if there is one: "R", "w", "R24".
static void apply_plan(const char *lp) {
  char first = lp[0];
  if (first != 'r' && first != 'R' && first != 'w' && first != 'W') return;
  NodeLink new_preferred = (first == 'w' || first == 'W') ? LINK_WIFI : LINK_RADIO;
  bool new_saver = first == 'R' || first == 'W';
  unsigned long new_hours = strtoul(lp + 1, nullptr, 10);
  if (new_preferred != preferred || new_saver != saver || new_hours != check_hours) {
    preferred = new_preferred;
    saver = new_saver;
    check_hours = new_hours;
    Serial.print(F("[LINKS] From the Pi: "));
    Serial.print(preferred == LINK_WIFI ? F("Wi-Fi") : F("radio"));
    Serial.print(F(" first, battery saver "));
    Serial.println(saver ? F("on.") : F("off."));
  }
}

void link_power_heard(NodeLink link, JsonDocument &command) {
  const char *lp = command["lp"] | "";
  if (*lp) apply_plan(lp);
  if (strcmp(command["t"] | "", "ping") == 0) return;   // pings don't keep a link on
  heard_at[link] = millis();
  heard[link] = true;
}

void link_power_keep(NodeLink link) {
  heard_at[link] = millis();
  heard[link] = true;
}

void link_power_reset() {
  heard_at[LINK_RADIO] = heard_at[LINK_WIFI] = millis();
  heard[LINK_RADIO] = heard[LINK_WIFI] = false;
}

void link_power_plan(bool &radio_on, bool &wifi_on) {
  if (!saver) {
    radio_on = wifi_on = true;
    return;
  }
  unsigned long now = millis();
  NodeLink backup = preferred == LINK_RADIO ? LINK_WIFI : LINK_RADIO;
  bool backup_on = heard[backup] && now - heard_at[backup] < KEEP_MINUTES * 60000UL;
  if (backup == LINK_RADIO) {
    backup_on = backup_on || now - heard_at[LINK_WIFI] >= SILENCE_MINUTES * 60000UL;
  } else if (check_hours > 0) {
    // A few minutes of Wi-Fi every check_hours (the first right after boot).
    backup_on = backup_on || (now / 1000) % (check_hours * 3600UL) < CHECK_MINUTES * 60;
  }
  radio_on = preferred == LINK_RADIO || backup_on;
  wifi_on = preferred == LINK_WIFI || backup_on;
}

#endif
