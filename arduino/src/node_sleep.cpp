#include "packet_protocol.h"
#include "node_sleep.h"

uint32_t requested_wake = 0;

void handle_sleep(JsonDocument &command, PacketSender &sender) {
  const char *w = command["w"] | "";
  uint32_t wake = 0;
  const char *refused = nullptr;

#ifndef BOARD_HAS_RTC
  refused = "no RTC to wake from";
#else
  if (!clock_valid())                          refused = "clock not set";
  else if (!parse_iso(w, &wake))               refused = "bad wake time";
  else if (wake <= clock_now())                refused = "wake time already passed";
  else if (wake - clock_now() > SLEEP_MAX_S)   refused = "longer than SLEEP_MAX_S";
#endif

  JsonDocument doc;
  doc["ok"] = refused ? 0 : 1;
  doc["w"] = w;
  if (refused) doc["why"] = refused;
  sender.send(doc, "sleep_ack");

  if (refused) {
    Serial.print(F("[SLEEP] Refused: "));
    Serial.println(refused);
    return;
  }
  requested_wake = wake;
}

#ifdef BOARD_HAS_RTC

static void on_alarm() {}   // the alarm only needs to wake the chip

void sleep_until(uint32_t wake, RH_RF69 &radio) {
  char ts[20];
  format_iso(wake, ts);
  Serial.print(F("[SLEEP] Radio off, sleeping until "));
  Serial.println(ts);
  Serial.flush();

  radio.sleep();
#if SLEEP_USE_STANDBY
  // SAMD21 errata: letting the flash controller power down in standby can
  // hard-fault the chip on wake. Keep it powered (costs a few µA).
  NVMCTRL->CTRLB.bit.SLEEPPRM = NVMCTRL_CTRLB_SLEEPPRM_DISABLED_Val;
#endif
  rtc.attachInterrupt(on_alarm);

  while (clock_now() < wake) {
    uint32_t next = min(wake, clock_now() + (uint32_t)(LOG_INTERVAL_MS / 1000));
    rtc.setAlarmEpoch(next);
    rtc.enableAlarm(rtc.MATCH_YYMMDDHHMMSS);
#if SLEEP_USE_STANDBY
    // USB activity would wake the chip straight back up — disconnect it
    // for the duration. The computer sees the port vanish and return.
    USBDevice.detach();
    rtc.standbyMode();
    USBDevice.attach();
#else
    // Bench mode: stay awake on a timer so the serial console keeps working.
    while (clock_now() < next) delay(200);
#endif
    rtc.disableAlarm();

    // Anything other than the alarm (there shouldn't be anything) just
    // goes back to sleep; only a real alarm takes a reading.
    if (clock_now() >= next) {
      run_sense_cycle();
      node_log_snapshot(clock_now());
    }
  }

  rtc.detachInterrupt();
  // RadioHead puts the radio back in receive mode on its next available().
  Serial.println(F("[SLEEP] Awake, radio back on."));
}

#else

void sleep_until(uint32_t, RH_RF69 &) {}

#endif
