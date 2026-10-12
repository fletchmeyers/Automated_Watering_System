#include "packet_protocol.h"
#include "wifi_link.h"
#include "node_watchdog.h"

#ifdef NODE_LINK_WIFI

#if defined(BOARD_ESP32)
  #include <WiFi.h>
  #include <Preferences.h>
  #include <ESPmDNS.h>
#elif defined(BOARD_RP2)
  #include <WiFi.h>
  #include <EEPROM.h>
#elif defined(BOARD_HAS_WINC1500)
  #include <WiFi101.h>
  #include <FlashStorage.h>
#else
  #error "Wi-Fi is only built for ESP32 boards, the Pico W / Pico 2 W and the Feather M0 WiFi"
#endif

static const uint16_t DEFAULT_PI_PORT = 5006;
static const unsigned long RETRY_MS = 5000;          // between attempts to reach the Pi
static const unsigned long JOIN_REPORT_MS = 30000;   // "still joining" every this long
#if defined(BOARD_RP2) || defined(BOARD_HAS_WINC1500)
// Neither core rejoins by itself: start joining over this long after the
// last try. The M0's WiFi101 waits (up to WINC_JOIN_MS) while it joins, so
// each failed try doubles the wait, up to REJOIN_MAX_MS, to keep the radio
// listening most of the time while Wi-Fi is out of reach.
static const unsigned long REJOIN_MS = 30000;
static const unsigned long REJOIN_MAX_MS = 5UL * 60 * 1000;
static unsigned long rejoin_wait = REJOIN_MS, last_begin = 0;
#endif
#ifdef BOARD_HAS_WINC1500
// The Feather M0 WiFi's ATWINC1500 module (Adafruit 3044).
static const int WINC_CS = 8, WINC_IRQ = 7, WINC_RST = 4, WINC_EN = 2;
static const unsigned long WINC_JOIN_MS = 8000;
#endif

static String ssid, password, pi_host, pi_hostname;
static uint16_t pi_port = DEFAULT_PI_PORT;
static bool wifi_started = false;
static bool wifi_up = false;
static unsigned long join_started = 0, last_join_report = 0;
static unsigned long last_attempt = 0;
#ifdef BOARD_ESP32
static bool mdns_started = false;
#endif

static WiFiClient client;
static PacketSender tcp_sender(NODE_ID, &client);
static LineReader tcp_lines;

// "192.168.1.50": not every core's IPAddress has toString().
static String ip_text(IPAddress ip) {
  return String(ip[0]) + "." + ip[1] + "." + ip[2] + "." + ip[3];
}

// ── Saved settings ───────────────────────────────────────────────────────
// ESP32: its NVS (Preferences). Pico W / Pico 2 W: the Arduino-Pico core's
// EEPROM, the last sector of flash. Both survive re-flashing. Feather M0
// WiFi: a page of its own flash (FlashStorage), which an upload erases;
// node_setup.py sends the settings again after every upload anyway.

#if defined(BOARD_ESP32)

static const char *PREFS_NAMESPACE = "garden";

static void load_settings() {
  Preferences prefs;
  prefs.begin(PREFS_NAMESPACE, true);
  ssid        = prefs.getString("ssid", "");
  password    = prefs.getString("pass", "");
  pi_host     = prefs.getString("host", "");
  pi_hostname = prefs.getString("hostname", "");
  pi_port     = prefs.getUShort("port", DEFAULT_PI_PORT);
  prefs.end();
}

static bool save_settings(const char *s, const char *p, const char *h, const char *hn, uint16_t port) {
  Preferences prefs;
  prefs.begin(PREFS_NAMESPACE, false);
  prefs.putString("ssid", s);
  prefs.putString("pass", p);
  prefs.putString("host", h);
  prefs.putString("hostname", hn);
  prefs.putUShort("port", port);
  prefs.end();
  return true;
}

#else

struct StoredSettings {
  uint32_t magic;
  char ssid[33];       // Wi-Fi names are at most 32 bytes
  char pass[65];       // passwords at most 63 (or a 64-digit hex key)
  char host[40];
  char hostname[64];
  uint16_t port;
};
static const uint32_t SETTINGS_MAGIC = 0x31494657;   // "WFI1"; anything else is empty flash

#if defined(BOARD_RP2)
static const size_t EEPROM_BYTES = 512;
static_assert(sizeof(StoredSettings) <= EEPROM_BYTES, "settings must fit in EEPROM_BYTES");

static void read_stored(StoredSettings &stored) {
  EEPROM.begin(EEPROM_BYTES);
  EEPROM.get(0, stored);
  EEPROM.end();
}

static bool write_stored(const StoredSettings &stored) {
  EEPROM.begin(EEPROM_BYTES);
  EEPROM.put(0, stored);
  bool ok = EEPROM.commit();
  EEPROM.end();
  return ok;
}
#else
FlashStorage(settings_flash, StoredSettings);

static void read_stored(StoredSettings &stored) { stored = settings_flash.read(); }

static bool write_stored(const StoredSettings &stored) {
  settings_flash.write(stored);
  return true;
}
#endif

static void load_settings() {
  StoredSettings stored;
  read_stored(stored);
  if (stored.magic != SETTINGS_MAGIC) {
    ssid = password = pi_host = pi_hostname = "";
    pi_port = DEFAULT_PI_PORT;
    return;
  }
  // Never trust the terminators of what was read back.
  stored.ssid[sizeof(stored.ssid) - 1] = stored.pass[sizeof(stored.pass) - 1] = '\0';
  stored.host[sizeof(stored.host) - 1] = stored.hostname[sizeof(stored.hostname) - 1] = '\0';
  ssid = stored.ssid;
  password = stored.pass;
  pi_host = stored.host;
  pi_hostname = stored.hostname;
  pi_port = stored.port;
}

static bool copy_field(char *to, size_t size, const char *from) {
  if (strlen(from) >= size) return false;
  strcpy(to, from);
  return true;
}

static bool save_settings(const char *s, const char *p, const char *h, const char *hn, uint16_t port) {
  StoredSettings stored = {};
  stored.magic = SETTINGS_MAGIC;
  stored.port = port;
  if (!copy_field(stored.ssid, sizeof(stored.ssid), s) || !copy_field(stored.pass, sizeof(stored.pass), p)
      || !copy_field(stored.host, sizeof(stored.host), h)
      || !copy_field(stored.hostname, sizeof(stored.hostname), hn)) {
    return false;
  }
  return write_stored(stored);
}

#endif

// ── Joining the network ──────────────────────────────────────────────────

static void start_joining() {
  node_watchdog_feed();   // the M0's join waits up to WINC_JOIN_MS
#if defined(BOARD_ESP32)
  WiFi.setHostname((String("garden-node-") + NODE_ID).c_str());
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.begin(ssid.c_str(), password.c_str());
#elif defined(BOARD_RP2)
  WiFi.setHostname((String("garden-node-") + NODE_ID).c_str());
  WiFi.beginNoBlock(ssid.c_str(), password.c_str());   // begin() would wait up to 30 s
  last_begin = millis();
#else
  // WiFi101 can only join by waiting for it: at most WINC_JOIN_MS.
  WiFi.setTimeout(WINC_JOIN_MS);
  WiFi.begin(ssid.c_str(), password.c_str());
  last_begin = millis();
#endif
  join_started = last_join_report = millis();
}

void wifi_link_begin() {
#ifdef BOARD_HAS_WINC1500
  WiFi.setPins(WINC_CS, WINC_IRQ, WINC_RST, WINC_EN);
#endif
  load_settings();
  if (ssid.length() == 0) {
    Serial.println(F("[WIFI] No Wi-Fi settings yet: run node_setup.py on the Pi with this board plugged in."));
    return;
  }
#ifdef BOARD_HAS_WINC1500
  if (WiFi.status() == WL_NO_SHIELD) {
    Serial.println(F("[WIFI] No ATWINC1500 Wi-Fi module answered: is this really a Feather M0 WiFi?"));
    return;
  }
#endif
  client.stop();
  wifi_up = false;
  last_attempt = 0;
  Serial.print(F("[WIFI] Joining "));
  Serial.println(ssid);   // the network's name only; never the password
  start_joining();
  wifi_started = true;
}

static const char *join_problem(uint8_t status) {
  switch (status) {
    case WL_NO_SSID_AVAIL:   return "network not found";
    case WL_CONNECT_FAILED:  return "it turned the node away (wrong password?)";
    case WL_CONNECTION_LOST: return "connection lost";
    case WL_NO_SHIELD:       return "no Wi-Fi module answered";
    default:                 return "no answer yet";
  }
}

// True while the node is on the network. Says so every JOIN_REPORT_MS while
// it isn't, so a node that can't join isn't silent.
static bool wifi_ready() {
  uint8_t status = WiFi.status();
  if (status == WL_CONNECTED) {
    if (!wifi_up) {
      wifi_up = true;
#if defined(BOARD_RP2) || defined(BOARD_HAS_WINC1500)
      rejoin_wait = REJOIN_MS;
#endif
      Serial.print(F("[WIFI] Joined "));
      Serial.print(ssid);
      Serial.print(F(" as "));
      Serial.println(ip_text(WiFi.localIP()));
    }
    return true;
  }
  unsigned long now = millis();
  if (wifi_up) {
    wifi_up = false;
    join_started = last_join_report = now;
    Serial.println(F("[WIFI] Lost the Wi-Fi network; rejoining."));
  }
  if (now - last_join_report >= JOIN_REPORT_MS) {
    last_join_report = now;
    Serial.print(F("[WIFI] Still joining "));
    Serial.print(ssid);
    Serial.print(F(" after "));
    Serial.print((now - join_started) / 1000);
    Serial.print(F(" s: "));
    Serial.print(join_problem(status));
    Serial.print(F(" (status "));
    Serial.print(status);
    Serial.println(F(")."));
  }
#if defined(BOARD_RP2) || defined(BOARD_HAS_WINC1500)
  if (now - last_begin >= rejoin_wait) {
    WiFi.disconnect();
    node_watchdog_feed();
  #if defined(BOARD_RP2)
    WiFi.beginNoBlock(ssid.c_str(), password.c_str());
  #else
    WiFi.begin(ssid.c_str(), password.c_str());
    rejoin_wait = min(rejoin_wait * 2, REJOIN_MAX_MS);   // each failed try waits WINC_JOIN_MS
  #endif
    last_begin = millis();
  }
#endif
  return false;
}

// ── Reaching the Pi ──────────────────────────────────────────────────────

#if defined(BOARD_ESP32)
static bool connect_ip(IPAddress ip) { node_watchdog_feed(); return client.connect(ip, pi_port, 3000); }
#else
static bool connect_ip(IPAddress ip) { node_watchdog_feed(); return client.connect(ip, pi_port); }
#endif

// The Pi at its saved address, or — if that has changed — found by name
// (e.g. "pi" for pi.local): over mDNS on an ESP32; through the router's DNS
// on a Pico W or M0 WiFi, whose libraries can't look up .local names.
static bool connect_to_pi() {
  IPAddress ip;
  if (ip.fromString(pi_host) && connect_ip(ip)) return true;
  if (pi_hostname.length() == 0) return false;
  String name = pi_hostname;
  if (name.endsWith(".local")) name.remove(name.length() - 6);
  IPAddress found;
  node_watchdog_feed();   // looking the Pi up by name waits up to 3 s
#if defined(BOARD_ESP32)
  if (!mdns_started) {
    mdns_started = MDNS.begin((String("garden-node-") + NODE_ID).c_str());
  }
  found = MDNS.queryHost(name.c_str(), 2000);
#elif defined(BOARD_RP2)
  if (!WiFi.hostByName(name.c_str(), found, 3000)) return false;
#else
  if (!WiFi.hostByName(name.c_str(), found)) return false;
#endif
  return found != IPAddress(0, 0, 0, 0) && connect_ip(found);
}

bool wifi_link_poll(JsonDocument &out) {
  if (!wifi_started || !wifi_ready()) return false;
  if (!client.connected()) {
    if (last_attempt && millis() - last_attempt < RETRY_MS) return false;
    last_attempt = millis();
    if (!connect_to_pi()) {
      Serial.println(F("[WIFI] Can't reach the Pi's garden-wifi service yet; retrying."));
      return false;
    }
#if defined(BOARD_ESP32) || defined(BOARD_RP2)
    client.setNoDelay(true);
#endif
    tcp_lines = LineReader();
    JsonDocument hello;
    hello["ip"] = ip_text(WiFi.localIP());
    tcp_sender.send(hello, "hello");   // t, q and n are added by send()
    Serial.print(F("[WIFI] Connected to the Pi at "));
    Serial.println(ip_text(client.remoteIP()));
  }
  return tcp_lines.read(client, out);
}

PacketSender &wifi_sender() { return tcp_sender; }

bool wifi_link_running() { return wifi_started; }

void wifi_link_off() {
  if (wifi_started) {
    client.flush();   // e.g. a sleep_ack just sent
    delay(20);
    client.stop();
#if defined(BOARD_ESP32)
    WiFi.disconnect(true);
    WiFi.mode(WIFI_OFF);
#elif defined(BOARD_RP2)
    WiFi.end();
#else
    WiFi.end();
#endif
    wifi_started = false;
    wifi_up = false;
    Serial.println(F("[WIFI] Wi-Fi off."));
  }
#ifdef BOARD_HAS_WINC1500
  // Hold the module disabled (it may never have been started).
  pinMode(WINC_EN, OUTPUT);
  digitalWrite(WINC_EN, LOW);
#endif
}

void handle_wifi_config(JsonDocument &command, PacketSender &reply) {
  bool saved = save_settings(command["s"] | "", command["p"] | "", command["h"] | "",
                             command["hn"] | "", command["port"] | DEFAULT_PI_PORT);
  JsonDocument ack;
  ack["ok"] = saved ? 1 : 0;
  if (!saved) ack["e"] = "a setting is too long to save";
  reply.send(ack, "wifi_config_ack");
  if (!saved) {
    Serial.println(F("[WIFI] Settings NOT saved: one is too long."));
    return;
  }
  Serial.println(F("[WIFI] Settings saved; reconnecting with them."));

  WiFi.disconnect();
  wifi_link_begin();
}

#endif
