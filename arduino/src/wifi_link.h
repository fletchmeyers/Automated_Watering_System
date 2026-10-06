/*
 * wifi_link.h
 *
 * For nodes on Wi-Fi instead of the radio (nodes.json "link": "wifi";
 * node_setup.py builds them with -D NODE_LINK_WIFI). ESP32 boards only.
 *
 * The node joins the Wi-Fi network, opens a TCP connection to the Pi's
 * garden-wifi service (raspberrypi/wifi_nodes.py), says hello, and then
 * waits: the Pi stays in charge, sending the same JSON commands it sends
 * over the radio, and the node answers with the same packets — one JSON
 * object per line, as over USB. If the connection drops the node keeps
 * trying to reconnect.
 *
 * The Wi-Fi name and password and the Pi's address aren't in the firmware:
 * node_setup.py sends them over USB once ({"t":"wifi_config",...}), and the
 * node keeps them in its own flash (ESP32 NVS, survives re-flashing).
 */

#ifndef WIFI_LINK_H
#define WIFI_LINK_H

#ifdef NODE_LINK_WIFI

#include <ArduinoJson.h>

class PacketSender;

// Start joining Wi-Fi with the saved settings (none yet: says so, waits).
void wifi_link_begin();

// Keep Wi-Fi and the connection to the Pi up; true when a whole command
// line from the Pi has arrived (in out). Never waits long.
bool wifi_link_poll(JsonDocument &out);

// Replies to commands that came over Wi-Fi go back through this.
PacketSender &wifi_sender();

// {"t":"wifi_config","s":ssid,"p":password,"h":pi_ip,"hn":pi_hostname,
//  "port":port} from node_setup.py over USB: save, ack, reconnect with them.
void handle_wifi_config(JsonDocument &command, PacketSender &reply);

#endif
#endif
