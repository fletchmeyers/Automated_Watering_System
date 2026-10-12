#include <Arduino.h>
#include <Adafruit_SleepyDog.h>
#include "node_watchdog.h"

#if defined(BOARD_ESP32)
  #include <esp_system.h>
#endif

#if defined(BOARD_FEATHER_M0)
static const int WATCHDOG_MS = 16000;   // the M0 WiFi's module alone can take 8 s to join
#else
static const int WATCHDOG_MS = 8000;
#endif

static bool running = false;

static bool last_reset_was_watchdog() {
#if defined(BOARD_FEATHER_M0)
  return Watchdog.resetCause() & PM_RCAUSE_WDT;
#elif defined(BOARD_RP2)
  return rp2040.getResetReason() == RP2040::WDT_RESET;
#elif defined(BOARD_ESP32)
  esp_reset_reason_t reason = esp_reset_reason();
  return reason == ESP_RST_TASK_WDT || reason == ESP_RST_INT_WDT || reason == ESP_RST_WDT;
#else
  return false;
#endif
}

void node_watchdog_begin() {
  if (last_reset_was_watchdog()) {
    Serial.println(F("[WATCHDOG] Restarted: the code had stopped (hung) before this reset."));
  }
#ifdef NO_WATCHDOG
  Serial.println(F("[WATCHDOG] Off (built with NO_WATCHDOG)."));
#else
  running = Watchdog.enable(WATCHDOG_MS) > 0;
  if (running) {
    Serial.print(F("[WATCHDOG] On: the board resets if the code stops for "));
    Serial.print(WATCHDOG_MS / 1000);
    Serial.println(F(" s."));
  } else {
    Serial.println(F("[WATCHDOG] Couldn't start it."));
  }
#endif
}

void node_watchdog_feed() {
  if (running) Watchdog.reset();
}

void node_watchdog_pause() {
#if defined(BOARD_FEATHER_M0) && !defined(NO_WATCHDOG)
  if (running) {
    Watchdog.disable();
    running = false;
  }
#endif
}

void node_watchdog_resume() {
#if defined(BOARD_FEATHER_M0) && !defined(NO_WATCHDOG)
  if (!running) running = Watchdog.enable(WATCHDOG_MS) > 0;
#endif
  node_watchdog_feed();
}
