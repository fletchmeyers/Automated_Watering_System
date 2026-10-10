# tests/software_tests/test_wifi_nodes.py
# Nodes on Wi-Fi instead of the radio: nodes.json rules, the garden-wifi
# service (wifi_nodes.py) end to end with a fake node, and node_setup.py's
# Wi-Fi settings.
import asyncio
import json
import sqlite3

import pytest

import communication_indoor
import node_setup
import wifi_nodes
from nodes import (NodeConfigError, check_node, arduino_build_flags, load_nodes,
                   radio_node_ids, wifi_node_ids, sync_node_ids, storage_of)


def wifi_node(**changes):
    node = {"name": "Kitchen ESP32", "framework": "arduino", "board": "feather_esp32_v2",
            "link": "wifi", "sense_interval_s": 3, "log_interval_s": 60}
    node.update(changes)
    return node


# ── nodes.json ───────────────────────────────────────────────────────────────

def test_a_wifi_node_needs_no_pins_or_storage_and_builds_without_a_radio():
    node = wifi_node()
    check_node(20, node)
    assert storage_of(node) == "none"
    flags = arduino_build_flags(20, node)
    assert "-D NODE_LINK_WIFI" in flags and "-D LOG_BACKEND=LOG_NONE" in flags
    assert not any("RFM69" in f for f in flags)


@pytest.mark.parametrize("node, message", [
    (wifi_node(board="pico"), "ESP32"),
    (wifi_node(board="picow", framework="circuitpython"), "ESP32"),
    (wifi_node(sleep_window=["19:00", "07:00"]), "doesn't sleep"),
    (wifi_node(link="lora"), "link must be one of"),
])
def test_wifi_node_mistakes_are_reported(node, message):
    with pytest.raises(NodeConfigError, match=message):
        check_node(20, node)


def test_radio_loop_and_wifi_service_split_the_nodes(tmp_path):
    from nodes import EXAMPLE_FILE
    data = json.loads(EXAMPLE_FILE.read_text())
    data["nodes"]["20"] = wifi_node()
    path = tmp_path / "nodes.json"
    path.write_text(json.dumps(data))
    nodes = load_nodes(path)
    assert radio_node_ids(nodes) == [1, 2]
    assert wifi_node_ids(nodes) == [20]
    assert 20 not in sync_node_ids(nodes)


# ── The garden-wifi service ──────────────────────────────────────────────────

@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setattr(communication_indoor, "ARCHIVE_DIR", tmp_path)
    from nodes import EXAMPLE_FILE
    data = json.loads(EXAMPLE_FILE.read_text())
    data["nodes"]["20"] = wifi_node()
    nodes_file = tmp_path / "nodes.json"
    nodes_file.write_text(json.dumps(data))
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE readings (ts TEXT, node_id INTEGER, sensor_type TEXT, key TEXT, value REAL)")
    svc = wifi_nodes.WifiNodes(conn, nodes_file=nodes_file, poll_interval=0.05, reply_timeout=2,
                               status_file=tmp_path / "status.json", data_file=tmp_path / "data.txt")
    return svc, conn, tmp_path


async def fake_node(port, node_id, polls_to_answer, seen):
    '''Connects like wifi_link.cpp does and answers polls like send_latest().'''
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write((json.dumps({"t": "hello", "q": 0, "n": node_id, "ip": "192.168.1.50"}) + "\n").encode())
    await writer.drain()
    q = 1
    for _ in range(polls_to_answer):
        poll = json.loads(await reader.readline())
        seen.append(poll)
        for packet in ({"t": "ts", "v": poll["ts"]}, {"t": "sht", "tmp": 21.5, "rh": 40.0},
                       {"t": "batch_end", "exp": 1, "snt": 1}):
            packet.update(q=q, n=node_id)
            q += 1
            writer.write((json.dumps(packet) + "\n").encode())
        await writer.drain()
    writer.close()


def test_wifi_node_is_polled_and_its_readings_stored(service):
    svc, conn, tmp_path = service

    async def scenario():
        server = await asyncio.start_server(svc.handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        seen = []
        await asyncio.wait_for(fake_node(port, 20, 2, seen), 10)
        await asyncio.sleep(0.2)          # let the service notice the node left
        server.close()
        await server.wait_closed()
        return seen

    seen = asyncio.run(scenario())
    assert [p["t"] for p in seen] == ["poll", "poll"] and all(p["n"] == 20 for p in seen)
    rows = conn.execute("SELECT node_id, sensor_type, key, value FROM readings ORDER BY key").fetchall()
    assert rows == [(20, "sht", "rh", 40.0), (20, "sht", "rh", 40.0),
                    (20, "sht", "tmp", 21.5), (20, "sht", "tmp", 21.5)]
    assert json.loads((tmp_path / "status.json").read_text()) == {}   # gone again


def test_unknown_node_is_turned_away(service):
    svc, conn, tmp_path = service

    async def scenario():
        server = await asyncio.start_server(svc.handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b'{"t":"hello","q":0,"n":2}\n')   # node 2 is a radio node
        await writer.drain()
        closed = await asyncio.wait_for(reader.read(), 5)
        server.close()
        await server.wait_closed()
        return closed

    assert asyncio.run(scenario()) == b""                # closed without a poll
    assert svc.links == {}


# ── node_setup.py's Wi-Fi settings ───────────────────────────────────────────

def test_wifi_settings_come_from_networkmanager(monkeypatch):
    calls = []

    class Result:
        def __init__(self, out):
            self.returncode, self.stdout, self.stderr = 0, out, ""

    def fake_run(cmd, capture_output, text):
        calls.append(cmd)
        if "GENERAL.CONNECTION" in cmd:
            return Result("preconfigured\n")
        if "802-11-wireless.ssid" in cmd:
            return Result("Garden\\:Net\n")              # nmcli escapes colons
        return Result("s3cret\\\\pass\n")

    monkeypatch.setattr(node_setup.subprocess, "run", fake_run)
    monkeypatch.setattr(node_setup, "pi_address", lambda: "192.168.1.191")
    settings = node_setup.wifi_settings()
    assert settings["s"] == "Garden:Net" and settings["p"] == "s3cret\\pass"
    assert settings["h"] == "192.168.1.191" and settings["port"] == wifi_nodes.WIFI_PORT
    assert calls[-1][0] == "sudo"                         # only the password needs root


def test_bench_wifi_file_is_valid():
    from pathlib import Path
    nodes = load_nodes(Path(__file__).parent.parent / "hardware_tests" / "bench_nodes_wifi.json")
    assert wifi_node_ids(nodes) == [13, 14] and radio_node_ids(nodes) == []


def test_no_store_mode_polls_without_saving(service, capsys):
    svc, conn, tmp_path = service
    svc.store = False

    async def scenario():
        server = await asyncio.start_server(svc.handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        await asyncio.wait_for(fake_node(port, 20, 1, []), 10)
        await asyncio.sleep(0.2)
        server.close()
        await server.wait_closed()

    asyncio.run(scenario())
    assert conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 0
    assert not (tmp_path / "data.txt").exists()
    assert '"tmp": 21.5' in capsys.readouterr().out          # printed instead


def test_wifi_settings_reach_a_board_that_reset_when_its_port_opened(monkeypatch):
    '''The ESP32 V2 can reset as its port opens: the first question goes
    unanswered while it boots. Keep asking on the same open port, then send
    the settings there too, rather than reopening (and resetting it again).'''
    import usb_sync
    opened, received = [], []

    class BootingV2:
        def __init__(self):
            self.replies, self.ignored = [], 0
        def write(self, data):
            command = json.loads(data)
            received.append(command)
            if command["t"] == "info" and self.ignored == 0:
                self.ignored += 1                         # still booting: lost
                return
            reply = ({"t": "info", "q": 1, "n": 14, "ub": 0, "fb": 0, "tb": 0}
                     if command["t"] == "info" else {"t": "wifi_config_ack", "q": 2, "n": 14, "ok": 1})
            self.replies.append((json.dumps(reply) + "\n").encode())
        def readline(self):
            return self.replies.pop(0) if self.replies else b""
        def close(self):
            pass

    def fake_open(path):
        opened.append(path)
        return BootingV2()

    monkeypatch.setattr(usb_sync, "open_port", fake_open)
    monkeypatch.setattr(node_setup, "serial_ports", lambda: ["/dev/ttyACM0"])
    real_ask = usb_sync.UsbNode.ask
    monkeypatch.setattr(usb_sync.UsbNode, "ask",
                        lambda self, cmd, kind, timeout=1: real_ask(self, cmd, kind, timeout=0.2))

    settings = {"s": "Garden", "p": "pw", "h": "192.168.1.191", "hn": "pi", "port": 5006}
    assert node_setup.send_wifi_config(14, settings, answer_within=3)
    assert opened == ["/dev/ttyACM0"]                       # one port session only
    assert [c["t"] for c in received] == ["info", "info", "wifi_config"]
    assert received[-1]["s"] == "Garden" and received[-1]["p"] == "pw"


def test_wifi_settings_are_not_sent_to_a_different_node(monkeypatch):
    import usb_sync

    class OtherNode:
        def __init__(self):
            self.replies = []
        def write(self, data):
            self.replies.append(b'{"t":"info","q":1,"n":9}\n')
        def readline(self):
            return self.replies.pop(0) if self.replies else b""
        def close(self):
            pass

    monkeypatch.setattr(usb_sync, "open_port", lambda path: OtherNode())
    monkeypatch.setattr(node_setup, "serial_ports", lambda: ["/dev/ttyACM0"])
    assert not node_setup.send_wifi_config(14, {"s": "x", "p": "y"}, answer_within=1)


def test_the_longest_wifi_settings_line_fits_the_firmwares_command_buffer():
    '''The firmware drops a command line longer than LineReader's buffer.
    The longest Wi-Fi settings line: a 32-byte network name of control
    characters (each escaped to \\u00XX) and a 63-character password of
    quote marks (each escaped to \\").'''
    import re
    from pathlib import Path
    header = (Path(__file__).parents[2] / "arduino" / "src" / "packet_protocol.h").read_text()
    buffer = int(re.search(r"char line\[(\d+)\]", header).group(1))
    worst = {"t": "wifi_config", "s": "\x01" * 32, "p": '"' * 63,
             "h": "255.255.255.255", "hn": "a-long-hostname-for-the-pi", "port": 65535}
    line = json.dumps(worst, separators=(",", ":"))   # exactly what UsbNode.send() writes
    assert len(line) < buffer, (len(line), buffer)
