#include "packet_protocol.h"
#include "wifi_link.h"

#ifdef NODE_LINK_WIFI

#include <WiFi.h>
#include <Preferences.h>
#include <ESPmDNS.h>

static const char *PREFS_NAMESPACE = "garden";
static const uint16_t DEFAULT_PI_PORT = 5006;
static const unsigned long RETRY_MS = 5000;   // between attempts to reach the Pi

static String ssid, password, pi_host, pi_hostname;
static uint16_t pi_port = DEFAULT_PI_PORT;
static bool wifi_started = false;
static bool mdns_started = false;
static unsigned long last_attempt = 0;

static WiFiClient client;
static PacketSender tcp_sender(NODE_ID, &client);
static LineReader tcp_lines;

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

void wifi_link_begin() {
  load_settings();
  if (ssid.isEmpty()) {
    Serial.println(F("[WIFI] No Wi-Fi settings yet: run node_setup.py on the Pi with this board plugged in."));
    return;
  }
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.begin(ssid.c_str(), password.c_str());
  wifi_started = true;
  client.stop();
  last_attempt = 0;
  Serial.print(F("[WIFI] Joining "));
  Serial.println(ssid);   // the network's name only; never the password
}

// The Pi at its saved address, or — if that has changed — found by name
// over mDNS (e.g. "pi" for pi.local).
static bool connect_to_pi() {
  IPAddress ip;
  if (ip.fromString(pi_host) && client.connect(ip, pi_port, 3000)) return true;
  if (pi_hostname.isEmpty()) return false;
  if (!mdns_started) {
    mdns_started = MDNS.begin((String("garden-node-") + NODE_ID).c_str());
  }
  String name = pi_hostname;
  if (name.endsWith(".local")) name.remove(name.length() - 6);
  IPAddress found = MDNS.queryHost(name.c_str(), 2000);
  return found != IPAddress(0, 0, 0, 0) && client.connect(found, pi_port, 3000);
}

bool wifi_link_poll(JsonDocument &out) {
  if (!wifi_started || WiFi.status() != WL_CONNECTED) return false;
  if (!client.connected()) {
    if (last_attempt && millis() - last_attempt < RETRY_MS) return false;
    last_attempt = millis();
    if (!connect_to_pi()) {
      Serial.println(F("[WIFI] Can't reach the Pi's garden-wifi service yet; retrying."));
      return false;
    }
    client.setNoDelay(true);
    tcp_lines = LineReader();
    JsonDocument hello;
    hello["ip"] = WiFi.localIP().toString();
    tcp_sender.send(hello, "hello");   // t, q and n are added by send()
    Serial.print(F("[WIFI] Connected to the Pi at "));
    Serial.println(client.remoteIP());
  }
  return tcp_lines.read(client, out);
}

PacketSender &wifi_sender() { return tcp_sender; }

void handle_wifi_config(JsonDocument &command, PacketSender &reply) {
  Preferences prefs;
  prefs.begin(PREFS_NAMESPACE, false);
  prefs.putString("ssid", command["s"] | "");
  prefs.putString("pass", command["p"] | "");
  prefs.putString("host", command["h"] | "");
  prefs.putString("hostname", command["hn"] | "");
  prefs.putUShort("port", command["port"] | DEFAULT_PI_PORT);
  prefs.end();

  JsonDocument ack;
  ack["ok"] = 1;
  reply.send(ack, "wifi_config_ack");
  Serial.println(F("[WIFI] Settings saved; reconnecting with them."));

  WiFi.disconnect();
  wifi_link_begin();
}

#endif
