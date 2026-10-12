# tests/software_tests/test_circuitpython_wifi.py
# Wi-Fi on CircuitPython nodes (Pico W / Pico 2 W): nodes.json rules,
# node_config.py, the Wi-Fi settings written into settings.toml, and the
# node's own wifi_link.py and link_power.py run against fakes.
import json
import tomllib
import types

import pytest

import link_power
import node_setup
import nodes
import wifi_link
from nodes import NodeConfigError, check_node, circuitpython_config
from test_node_list import Typing

CP_RADIO = {"spi_sck": "GP18", "spi_mosi": "GP19", "spi_miso": "GP16",
            "radio_cs": "GP22", "radio_rst": "GP27"}


def pico(**changes):
    node = {"name": "Pico (CircuitPython)", "framework": "circuitpython", "board": "pico2w",
            "link": ["radio", "wifi"], "sense_interval_s": 3, "log_interval_s": 60,
            "storage": "sd", "pins": {**CP_RADIO, "sd_cs": "GP17"}}
    node.update(changes)
    return node


# ── nodes.json and node_config.py ────────────────────────────────────────────

def test_a_circuitpython_pico_w_can_have_wifi():
    check_node(1, pico())
    check_node(1, pico(link="wifi", storage="none", pins={}))
    check_node(1, pico(battery_saver=True, wifi_check_hours=24))


def test_wifi_only_with_an_sd_card_still_needs_the_spi_pins():
    with pytest.raises(NodeConfigError, match="shares the radio's SPI bus"):
        check_node(1, pico(link="wifi", pins={"sd_cs": "GP17"}))
    check_node(1, pico(link="wifi", pins={"sd_cs": "GP17", "spi_sck": "GP18",
                                          "spi_mosi": "GP19", "spi_miso": "GP16"}))


def test_node_config_says_the_links_and_battery_saver():
    config = {}
    exec(circuitpython_config(1, pico(battery_saver=True, wifi_check_hours=12)), config)
    assert (config["LINKS"], config["BATTERY_SAVER"], config["WIFI_CHECK_HOURS"]) == (["radio", "wifi"], True, 12)
    config = {}
    exec(circuitpython_config(1, pico(link="wifi", storage="none", pins={})), config)
    assert (config["LINKS"], config["BATTERY_SAVER"], config["PINS"]) == (["wifi"], False, {})


def test_the_new_files_are_copied_to_the_board():
    assert {"wifi_link.py", "link_power.py"} <= set(node_setup.CIRCUITPY_FILES)
    assert node_setup.CIRCUITPY_FILES[-1] == "code.py"


def test_a_wifi_only_circuitpython_node_with_an_sd_card_is_asked_for_the_spi_pins(tmp_path, monkeypatch):
    monkeypatch.setattr(nodes, "NODES_FILE", tmp_path / "nodes.json")
    monkeypatch.setattr(nodes, "LEGACY_FILE", tmp_path / "old.json")
    # board (Pico 2 W), framework (CircuitPython), link: Wi-Fi, ID, name, short,
    # storage: SD, pins (the usual ones), sense, log, RTC, sleep, save
    t = Typing("", "", "wifi", "", "", "", "1", "", "", "", "", "", "")
    node_id = node_setup.node_wizard.add_node(nodes.NODES_FILE, t.wizard())
    added = nodes.load_nodes()[node_id]
    assert added["link"] == "wifi" and added["storage"] == "sd"
    assert {"sd_cs", "spi_sck", "spi_mosi", "spi_miso"} <= set(added["pins"])
    assert "radio_cs" not in added["pins"]


# ── settings.toml ────────────────────────────────────────────────────────────

SETTINGS = {"s": 'My "Garden" Wi-Fi', "p": "pa\\ss", "h": "192.168.1.191", "hn": "pi", "port": 5006}


def test_wifi_settings_go_into_settings_toml_without_losing_anything_else():
    before = ('# my own settings\nCIRCUITPY_WEB_API_PASSWORD = "secret"\n'
              'GARDEN_WIFI_SSID = "Old network"\n')
    text = node_setup.settings_toml_with(before, {"GARDEN_WIFI_SSID": SETTINGS["s"],
                                                  "GARDEN_WIFI_PASSWORD": SETTINGS["p"],
                                                  "GARDEN_PI_PORT": 5006})
    parsed = tomllib.loads(text)
    assert parsed["GARDEN_WIFI_SSID"] == 'My "Garden" Wi-Fi'      # quotes survive
    assert parsed["GARDEN_WIFI_PASSWORD"] == "pa\\ss"              # so do backslashes
    assert parsed["GARDEN_PI_PORT"] == "5006"
    assert parsed["CIRCUITPY_WEB_API_PASSWORD"] == "secret" and "# my own settings" in text
    assert text.count("GARDEN_WIFI_SSID") == 1


def test_setup_writes_them_onto_the_drive(tmp_path, monkeypatch):
    from contextlib import contextmanager

    @contextmanager
    def fake_mount(dev, where, read_only=False):
        yield tmp_path
    monkeypatch.setattr(node_setup, "mounted", fake_mount)
    monkeypatch.setattr(node_setup, "sync_disks", lambda: None)
    (tmp_path / "settings.toml").write_text('CIRCUITPY_WEB_API_PASSWORD = "x"\n')
    node_setup.write_circuitpython_wifi(SETTINGS)
    parsed = tomllib.loads((tmp_path / "settings.toml").read_text())
    assert parsed["GARDEN_WIFI_SSID"] == SETTINGS["s"] and parsed["GARDEN_PI_HOST"] == "192.168.1.191"
    assert parsed["CIRCUITPY_WEB_API_PASSWORD"] == "x"


# ── link_power.py ────────────────────────────────────────────────────────────

class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_without_battery_saver_both_links_stay_on():
    assert link_power.LinkPower(["radio", "wifi"]).plan() == (True, True)


def test_radio_first_never_turns_wifi_on_by_default():
    clock = Clock()
    power = link_power.LinkPower(["radio", "wifi"], battery_saver=True, clock=clock)
    for hours in (0, 1, 12, 48):
        clock.t = 1000 + hours * 3600
        assert power.plan() == (True, False)


def test_radio_first_with_a_wifi_check_turns_it_on_for_a_few_minutes():
    clock = Clock()
    power = link_power.LinkPower(["radio", "wifi"], battery_saver=True, wifi_check_hours=24, clock=clock)
    assert power.plan() == (True, True)                 # the first check, right after boot
    clock.t += 6 * 60
    assert power.plan() == (True, False)
    clock.t = 1000 + 24 * 3600 + 60
    assert power.plan() == (True, True)                 # a day later
    power.heard("wifi", {"t": "poll", "n": 1})          # the Pi is using it (the radio's down)
    clock.t += 4 * 60
    assert power.plan() == (True, True)                 # so it stays on after the check's 5 minutes


def test_wifi_first_brings_the_radio_on_once_wifi_goes_quiet():
    clock = Clock()
    power = link_power.LinkPower(["wifi", "radio"], battery_saver=True, clock=clock)
    assert power.plan() == (False, True)
    clock.t += 4 * 60
    power.heard("wifi", {"t": "poll"})
    clock.t += 4 * 60
    assert power.plan() == (False, True)
    clock.t += 2 * 60
    power.heard("wifi", {"t": "ping"})                  # pings don't count
    assert power.plan() == (True, True)


def test_the_pi_changes_the_settings_with_a_poll():
    clock = Clock()
    power = link_power.LinkPower(["radio", "wifi"], clock=clock)   # installed: both on
    assert power.plan() == (True, True)
    clock.t += 600
    power.heard("radio", {"t": "poll", "lp": "R"})                # battery saver, radio first
    assert power.plan() == (True, False)
    power.heard("radio", {"t": "poll", "lp": "W"})                # Wi-Fi first now
    assert power.preferred == "wifi" and power.plan()[1] is True
    power.heard("radio", {"t": "poll", "lp": "r"})                # battery saver off again
    assert power.plan() == (True, True)
    power.heard("radio", {"t": "poll", "lp": "R24"})
    assert (power.saver, power.check_hours) == (True, 24)
    power.heard("radio", {"t": "poll", "lp": "?"})                # nonsense is ignored
    assert (power.preferred, power.saver) == ("radio", True)


# ── wifi_link.py against a fake network ──────────────────────────────────────

class FakeSocket:
    def __init__(self, incoming):
        self.incoming = list(incoming)
        self.sent = b""
        self.connected_to = None

    def settimeout(self, t):
        pass

    def connect(self, address):
        self.connected_to = address

    def recv_into(self, buf):
        if not self.incoming:
            raise OSError(11, "EAGAIN")
        data = self.incoming.pop(0)
        buf[:len(data)] = data
        return len(data)

    def send(self, data):
        self.sent += data
        return len(data)

    def close(self):
        pass


@pytest.fixture
def network(monkeypatch):
    sock = FakeSocket([b'{"t":"poll","ts":"2026-10-11T12:00:00",', b'"n":1}\n{"t":"ping","q":3,"n":1}\n'])
    radio = types.SimpleNamespace(enabled=False, connected=False, ipv4_address="192.168.1.60")
    radio.connect = lambda ssid, password, timeout=None: setattr(radio, "connected", True)
    radio.stop_station = lambda: setattr(radio, "connected", False)
    pool = types.SimpleNamespace(AF_INET=2, SOCK_STREAM=1, socket=lambda *a: sock,
                                 getaddrinfo=lambda *a: [(0, 0, 0, "", ("192.168.1.191", 5006))])
    monkeypatch.setattr(wifi_link, "wifi", types.SimpleNamespace(radio=radio))
    monkeypatch.setattr(wifi_link, "socketpool", types.SimpleNamespace(SocketPool=lambda r: pool))
    env = {"GARDEN_WIFI_SSID": "Garden", "GARDEN_WIFI_PASSWORD": "pw", "GARDEN_PI_HOST": "192.168.1.191",
           "GARDEN_PI_HOSTNAME": "pi", "GARDEN_PI_PORT": "5006"}
    monkeypatch.setattr(wifi_link.os, "getenv", lambda key, default=None: env.get(key, default))
    return sock, radio


def test_it_joins_says_hello_and_reads_commands_split_across_packets(network):
    sock, radio = network
    link = wifi_link.WifiLink(1)
    assert link.configured
    link.start()
    assert radio.enabled and radio.connected
    first = link.receive()            # connects, says hello; half a command so far
    assert sock.connected_to == ("192.168.1.191", 5006)
    assert json.loads(sock.sent.decode().splitlines()[0]) == {"t": "hello", "q": 0, "n": 1, "ip": "192.168.1.60"}
    assert first is None
    assert link.receive() == {"t": "poll", "ts": "2026-10-11T12:00:00", "n": 1}
    assert link.receive() == {"t": "ping", "q": 3, "n": 1}
    assert link.receive() is None     # nothing more yet


def test_replies_go_back_as_json_lines(network):
    sock, _ = network
    from communication_garden import PacketSender
    link = wifi_link.WifiLink(1)
    link.start()
    link.receive()
    PacketSender(1, link).send({"t": "pong", "pq": 3})
    assert json.loads(sock.sent.decode().splitlines()[-1]) == {"t": "pong", "q": 0, "n": 1, "pq": 3}


def test_switching_off_powers_wifi_down(network):
    sock, radio = network
    link = wifi_link.WifiLink(1)
    link.start()
    link.stop()
    assert not link.on and not radio.enabled and link.receive() is None


def test_no_settings_yet(monkeypatch, capsys):
    monkeypatch.setattr(wifi_link.os, "getenv", lambda key, default=None: default)
    monkeypatch.setattr(wifi_link, "wifi", types.SimpleNamespace(radio=None))
    link = wifi_link.WifiLink(1)
    assert not link.configured
    link.start()
    assert not link.on and "No Wi-Fi settings yet" in capsys.readouterr().out


# ── node_watchdog.py ─────────────────────────────────────────────────────────

@pytest.fixture
def chip(monkeypatch):
    '''Fake microcontroller and watchdog modules: the dog records feeds.'''
    import sys
    import node_watchdog
    dog = types.SimpleNamespace(timeout=None, mode=None, feeds=0, stopped=False)
    dog.feed = lambda: setattr(dog, "feeds", dog.feeds + 1)
    dog.deinit = lambda: setattr(dog, "stopped", True)
    reasons = types.SimpleNamespace(WATCHDOG="watchdog", POWER_ON="power_on")
    micro = types.SimpleNamespace(watchdog=dog, ResetReason=reasons,
                                  cpu=types.SimpleNamespace(reset_reason="power_on"))
    monkeypatch.setitem(sys.modules, "microcontroller", micro)
    monkeypatch.setitem(sys.modules, "watchdog", types.SimpleNamespace(WatchDogMode=types.SimpleNamespace(RESET="reset")))
    monkeypatch.setattr(node_watchdog.os, "listdir", lambda path: ["code.py", "lib"])
    monkeypatch.setattr(node_watchdog, "_dog", None)
    return node_watchdog, dog, micro


def test_the_watchdog_resets_the_board_after_8_seconds(chip):
    node_watchdog, dog, _ = chip
    assert node_watchdog.start() and node_watchdog.running()
    assert (dog.timeout, dog.mode) == (8, "reset")
    node_watchdog.feed()
    assert dog.feeds == 2                       # once when started, once here


def test_ctrl_c_stops_it_for_the_console(chip):
    node_watchdog, dog, _ = chip
    node_watchdog.start()
    node_watchdog.stop()
    assert dog.stopped and not node_watchdog.running()
    node_watchdog.feed()                        # harmless once stopped


def test_a_no_watchdog_file_turns_it_off(chip, monkeypatch, capsys):
    node_watchdog, dog, _ = chip
    monkeypatch.setattr(node_watchdog.os, "listdir", lambda path: ["code.py", "no_watchdog"])
    assert not node_watchdog.start() and dog.timeout is None
    assert "Off" in capsys.readouterr().out


def test_it_says_when_it_reset_the_board(chip, capsys):
    node_watchdog, _, micro = chip
    micro.cpu.reset_reason = "watchdog"
    node_watchdog.start()
    assert "hung" in capsys.readouterr().out


def test_sending_packets_and_joining_wifi_feed_it(network, monkeypatch):
    import communication_garden
    fed = []
    monkeypatch.setattr(communication_garden, "feed", lambda: fed.append("packet"))
    monkeypatch.setattr(wifi_link, "feed", lambda: fed.append("wifi"))
    link = wifi_link.WifiLink(1)
    link.start()                                # joining feeds first
    link.receive()                              # connecting feeds; the hello is a raw send
    communication_garden.PacketSender(1, link).send({"t": "pong", "pq": 1})
    assert fed[:2] == ["wifi", "wifi"] and fed[-1] == "packet"


def test_the_watchdog_file_is_copied_to_the_board():
    assert "node_watchdog.py" in node_setup.CIRCUITPY_FILES


def test_failed_joins_back_off_so_the_radio_keeps_listening(network, monkeypatch):
    sock, radio = network
    joins = []
    radio.connect = lambda ssid, password, timeout=None: joins.append(clock[0])   # never joins
    clock = [0.0]
    monkeypatch.setattr(wifi_link.time, "monotonic", lambda: clock[0])
    link = wifi_link.WifiLink(1)
    link.start()
    for second in range(0, 1200, 5):
        clock[0] = float(second)
        link.receive()
    gaps = [b - a for a, b in zip(joins, joins[1:])]
    assert gaps[:4] == [30, 60, 120, 240] and max(gaps) == 300
