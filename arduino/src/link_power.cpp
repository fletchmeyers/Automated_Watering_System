#include <Arduino.h>
#include "link_power.h"

#ifdef NODE_HAS_BOTH_LINKS

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

static unsigned long heard_at[2] = {0, 0};    // last real command over each link (millis)
static bool heard[2] = {false, false};

void link_power_heard(NodeLink link, JsonDocument &command) {
  const char *pf = command["pf"] | "";
  if (strcmp(pf, "wifi") == 0) preferred = LINK_WIFI;
  else if (strcmp(pf, "radio") == 0) preferred = LINK_RADIO;
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
#ifndef BATTERY_SAVER
  radio_on = wifi_on = true;
#else
  unsigned long now = millis();
  NodeLink backup = preferred == LINK_RADIO ? LINK_WIFI : LINK_RADIO;
  bool backup_on = heard[backup] && now - heard_at[backup] < KEEP_MINUTES * 60000UL;
  if (backup == LINK_RADIO) {
    backup_on = backup_on || now - heard_at[LINK_WIFI] >= SILENCE_MINUTES * 60000UL;
  }
#if WIFI_CHECK_HOURS > 0
  else {
    // A few minutes of Wi-Fi every WIFI_CHECK_HOURS (the first right after boot).
    unsigned long period_s = WIFI_CHECK_HOURS * 3600UL;
    backup_on = backup_on || (now / 1000) % period_s < CHECK_MINUTES * 60;
  }
#endif
  radio_on = preferred == LINK_RADIO || backup_on;
  wifi_on = preferred == LINK_WIFI || backup_on;
#endif
}

#endif
