'''
CircuitPython running on a Pico W / Pico 2 W

Wi-Fi to the Pi, for nodes whose nodes.json "link" includes "wifi" (the
CircuitPython twin of arduino/src/wifi_link.cpp). The node joins the Wi-Fi
network, opens a TCP connection to the Pi's garden-wifi service
(raspberrypi/wifi_nodes.py), says hello, and then waits: the Pi stays in
charge, sending the same JSON commands as over the radio, one per line, and
the node answers over the same connection. If the connection or the network
drops it keeps trying, and while it can't join it says why every 30 s.

The Wi-Fi name and password and the Pi's address aren't in the code:
node_setup.py on the Pi writes them into settings.toml on the CIRCUITPY drive
(GARDEN_WIFI_SSID, GARDEN_WIFI_PASSWORD, GARDEN_PI_HOST, GARDEN_PI_HOSTNAME,
GARDEN_PI_PORT), where code updates never touch them. Its own names, rather
than CIRCUITPY_WIFI_SSID, so CircuitPython doesn't also join by itself and
start its web workflow.

WifiLink stands in for the radio with PacketSender, like SerialLink: one
JSON line per packet, no size limit.

Written by Fletcher Meyers
October 2026
'''

import json
import os
import time

try:
    import socketpool
    import wifi
except ImportError:   # a board without Wi-Fi
    socketpool = wifi = None

DEFAULT_PI_PORT = 5006
RETRY_S       = 5    # between attempts to reach the Pi
JOIN_REPORT_S = 30   # "still joining" every this long
REJOIN_S      = 30   # start joining over if it hasn't worked
JOIN_TIMEOUT  = 6    # seconds a join may take (the loop waits for it; the watchdog's limit is 8)
_NO_DATA = (11, 116)   # EAGAIN, ETIMEDOUT: nothing to read yet


def feed():
    '''Feeds the watchdog before anything that waits: code.py points this at
    node_watchdog.feed.'''


class WifiLink:
    max_bytes = None
    line_gap  = 0

    def __init__(self, node_id):
        self.node_id = node_id
        self.ssid = os.getenv("GARDEN_WIFI_SSID")
        self.password = os.getenv("GARDEN_WIFI_PASSWORD") or ""
        self.host = os.getenv("GARDEN_PI_HOST")
        self.hostname = os.getenv("GARDEN_PI_HOSTNAME")
        self.port = int(os.getenv("GARDEN_PI_PORT") or DEFAULT_PI_PORT)
        self.on = False
        self._pool = None
        self._sock = None
        self._buf = b""
        self._chunk = bytearray(256)
        self._joined = False
        self._join_started = self._last_report = self._last_join = self._last_attempt = 0
        self._error = None
        self._sequence = 0

    @property
    def configured(self):
        return wifi is not None and bool(self.ssid)

    def start(self):
        '''Switch Wi-Fi on and start joining.'''
        if wifi is None:
            print("[WIFI] This board has no Wi-Fi.")
            return
        if not self.ssid:
            print("[WIFI] No Wi-Fi settings yet: run node_setup.py on the Pi with this board plugged in.")
            return
        wifi.radio.enabled = True
        self.on = True
        self._joined = False
        self._join_started = self._last_report = time.monotonic()
        print(f"[WIFI] Joining {self.ssid}")   # the network's name only; never the password
        self._join()

    def stop(self):
        '''Leave the network and power Wi-Fi down (battery saver).'''
        if not self.on:
            return
        self._close()
        try:
            wifi.radio.stop_station()
            wifi.radio.enabled = False
        except Exception as e:
            print(f"[WIFI] Couldn't power Wi-Fi down: {e}")
        self.on = False
        self._joined = False
        print("[WIFI] Wi-Fi off.")

    def _join(self):
        self._last_join = time.monotonic()
        feed()
        try:
            wifi.radio.connect(self.ssid, self.password, timeout=JOIN_TIMEOUT)
            self._error = None
        except Exception as e:
            self._error = str(e) or type(e).__name__

    def _ready(self):
        '''True while on the network; says why not every JOIN_REPORT_S.'''
        now = time.monotonic()
        if wifi.radio.connected:
            if not self._joined:
                self._joined = True
                print(f"[WIFI] Joined {self.ssid} as {wifi.radio.ipv4_address}")
            return True
        if self._joined:
            self._joined = False
            self._close()
            self._join_started = self._last_report = now
            print("[WIFI] Lost the Wi-Fi network; rejoining.")
        if now - self._last_report >= JOIN_REPORT_S:
            self._last_report = now
            print(f"[WIFI] Still joining {self.ssid} after {int(now - self._join_started)} s: "
                  f"{self._error or 'no answer yet'}.")
        if now - self._last_join >= REJOIN_S:
            self._join()
        return False

    def _pi_address(self):
        '''The Pi's saved address, or (if that fails) its name through the
        router's DNS; CircuitPython can't look up .local names.'''
        candidates = [self.host] if self.host else []
        if self.hostname:
            name = self.hostname[:-6] if self.hostname.endswith(".local") else self.hostname
            try:
                candidates.append(self._pool.getaddrinfo(name, self.port)[0][4][0])
            except Exception:
                pass
        return candidates

    def _connect(self):
        if self._pool is None:
            self._pool = socketpool.SocketPool(wifi.radio)
        for address in self._pi_address():
            sock = self._pool.socket(self._pool.AF_INET, self._pool.SOCK_STREAM)
            feed()
            try:
                sock.settimeout(3)
                sock.connect((address, self.port))
                sock.settimeout(0)
            except Exception:
                sock.close()
                continue
            self._sock = sock
            self._buf = b""
            hello = {"t": "hello", "q": 0, "n": self.node_id, "ip": str(wifi.radio.ipv4_address)}
            self.send(json.dumps(hello).encode())
            print(f"[WIFI] Connected to the Pi at {address}")
            return True
        return False

    def _close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def receive(self):
        '''Keep Wi-Fi and the connection up; the next command from the Pi as a
        dict, or None. Never waits long (except while joining).'''
        if not self.on or not self._ready():
            return None
        if self._sock is None:
            now = time.monotonic()
            if self._last_attempt and now - self._last_attempt < RETRY_S:
                return None
            self._last_attempt = now
            if not self._connect():
                print("[WIFI] Can't reach the Pi's garden-wifi service yet; retrying.")
                return None
        try:
            n = self._sock.recv_into(self._chunk)
        except OSError as e:
            if e.errno in _NO_DATA:
                n = None
            else:
                print(f"[WIFI] Connection to the Pi dropped: {e}")
                self._close()
                return None
        if n == 0:
            print("[WIFI] The Pi closed the connection.")
            self._close()
            return None
        if n:
            self._buf += bytes(self._chunk[:n])
        end = self._buf.find(b"\n")
        if end < 0:
            if len(self._buf) > 1024:
                self._buf = b""   # no newline in sight: not a command
            return None
        line, self._buf = self._buf[:end].strip(), self._buf[end + 1:]
        try:
            command = json.loads(line)
        except ValueError:
            return None
        return command if isinstance(command, dict) else None

    def send(self, data):
        '''One packet as a JSON line (PacketSender calls this).'''
        if self._sock is None:
            return
        data = data + b"\n"
        try:
            self._sock.settimeout(2)
            while data:
                sent = self._sock.send(data)
                data = data[sent:]
            self._sock.settimeout(0)
        except OSError as e:
            print(f"[WIFI] Send failed: {e}")
            self._close()
