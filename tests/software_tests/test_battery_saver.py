# tests/software_tests/test_battery_saver.py
# Sleep windows for nodes with Wi-Fi, and battery saver (only the link in
# use powered): nodes.json rules, build flags, the saver command, polls that
# say the preferred link, and the Wi-Fi service putting a node to sleep.
import asyncio
import json
import sqlite3
import sys
from datetime import datetime, timedelta

import pytest

import communication_indoor
import node_setup
import node_wizard
import nodes
import sync_indoor
import wifi_nodes
from nodes import NodeConfigError, arduino_build_flags, check_node, load_nodes
from test_node_list import Typing

RADIO_PINS = {"radio_cs": 32, "radio_irq": 27, "radio_rst": 33}


def esp32(**changes):
    node = {"name": "ESP32-V2 (Arduino)", "framework": "arduino", "board": "feather_esp32_v2",
            "link": ["radio", "wifi"], "sense_interval_s": 3, "log_interval_s": 60,
            "storage": "none", "pins": dict(RADIO_PINS)}
    node.update(changes)
    return node


# ── nodes.json ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("link", ["wifi", ["wifi", "radio"], ["radio", "wifi"]])
def test_nodes_with_wifi_can_sleep(link):
    check_node(3, esp32(link=link, sleep_window=["19:00", "07:00"]))


def test_battery_saver_and_a_wifi_check():
    check_node(3, esp32(battery_saver=True))
    check_node(3, esp32(battery_saver=True, wifi_check_hours=24))


@pytest.mark.parametrize("changes, message", [
    ({"battery_saver": True, "link": "wifi", "pins": {}}, "both Wi-Fi and the radio"),
    ({"battery_saver": "yes"}, "true or false"),
    ({"wifi_check_hours": 24}, "goes with battery_saver"),
    ({"battery_saver": True, "wifi_check_hours": 0}, "whole number of hours"),
    ({"battery_saver": True, "wifi_check_hours": 1.5}, "whole number of hours"),
])
def test_battery_saver_mistakes(changes, message):
    with pytest.raises(NodeConfigError, match=message):
        check_node(3, esp32(**changes))


def test_build_flags():
    radio_first = arduino_build_flags(3, esp32(battery_saver=True, wifi_check_hours=24))
    assert {"-D BATTERY_SAVER", "-D WIFI_CHECK_HOURS=24", "-D NODE_LINK_WIFI", "-D RFM69_CS=32"} <= set(radio_first)
    assert "-D LINK_FIRST_WIFI" not in radio_first
    wifi_first = arduino_build_flags(3, esp32(link=["wifi", "radio"], battery_saver=True))
    assert "-D LINK_FIRST_WIFI" in wifi_first and "-D WIFI_CHECK_HOURS" not in " ".join(wifi_first)
    plain = arduino_build_flags(3, esp32())
    assert "-D BATTERY_SAVER" not in plain


# ── node_setup.py saver ──────────────────────────────────────────────────────

@pytest.fixture
def node_file(tmp_path):
    path = tmp_path / "nodes.json"
    data = json.loads(nodes.EXAMPLE_FILE.read_text())
    data["nodes"]["3"] = esp32()
    path.write_text(json.dumps(data))
    return path


def test_saver_on_and_off(node_file):
    node = node_wizard.set_saver(node_file, 3, True, wifi_check_hours=24)
    assert (node["battery_saver"], node["wifi_check_hours"]) == (True, 24)
    assert load_nodes(node_file)[3]["battery_saver"] is True
    node = node_wizard.set_saver(node_file, 3, False)
    assert "battery_saver" not in node and "wifi_check_hours" not in node


def test_saver_needs_both_links(node_file):
    with pytest.raises(NodeConfigError, match="both Wi-Fi and the radio"):
        node_wizard.set_saver(node_file, 1, True)     # node 1 is radio only


def test_down_to_one_link_drops_the_saver(node_file):
    node_wizard.set_saver(node_file, 3, True, wifi_check_hours=24)
    node = node_wizard.set_links(node_file, 3, ["wifi"], Typing().wizard())
    assert "battery_saver" not in node and "wifi_check_hours" not in node


def test_saver_command_line(node_file, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["node_setup.py", "saver", "3", "on", "--wifi-check-hours", "12",
                                      "--nodes", str(node_file)])
    node_setup.main()
    assert load_nodes(node_file)[3]["wifi_check_hours"] == 12
    assert "re-flash" in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", ["node_setup.py", "saver", "3", "maybe", "--nodes", str(node_file)])
    with pytest.raises(SystemExit):
        node_setup.main()


def test_the_questions_offer_battery_saver(tmp_path, monkeypatch):
    monkeypatch.setattr(nodes, "NODES_FILE", tmp_path / "nodes.json")
    monkeypatch.setattr(nodes, "LEGACY_FILE", tmp_path / "old.json")
    # board ESP32 V2: link both (radio first), ID, name, short, storage, pins, sense, RTC,
    # battery saver: yes, sleep 19:00-07:00, save
    t = Typing("radio,wifi", "", "", "", "", "", "", "", "y", "19:00-07:00", "")
    node_id = node_wizard.add_node(nodes.NODES_FILE, t.wizard(), boards=["feather_esp32_v2"])
    added = load_nodes()[node_id]
    assert added["battery_saver"] is True and added["sleep_window"] == ["19:00", "07:00"]


# ── Polls and who's asleep ───────────────────────────────────────────────────

def test_a_poll_can_say_the_preferred_link(tmp_path, monkeypatch):
    monkeypatch.setattr(sync_indoor, "COMMAND_FILE", str(tmp_path / "command.json"))
    monkeypatch.setattr(sync_indoor, "POLL_RESULT_FILE", str(tmp_path / "result.json"))
    sync_indoor.request_poll(3, prefer="radio")
    assert json.loads((tmp_path / "command.json").read_text())["pf"] == "radio"
    sync_indoor.request_poll(1)
    assert "pf" not in json.loads((tmp_path / "command.json").read_text())


def test_a_sleep_either_service_sent_is_seen_by_both(tmp_path, monkeypatch):
    monkeypatch.setattr(sync_indoor, "NODE_INFO_FILE", tmp_path / "node_info.json")
    now = datetime(2026, 10, 11, 23, 0)
    assert sync_indoor.recorded_wake(3, now=now) is None
    sync_indoor.update_node_info(3, sleep_until="2026-10-12T07:00:00")
    assert sync_indoor.recorded_wake(3, now=now) == datetime(2026, 10, 12, 7, 0)
    assert sync_indoor.recorded_wake(3, now=datetime(2026, 10, 12, 7, 1)) is not None   # grace
    assert sync_indoor.recorded_wake(3, now=datetime(2026, 10, 12, 7, 5)) is None
    assert list(tmp_path.iterdir()) == [tmp_path / "node_info.json"]                   # no temp file left


# ── The Wi-Fi service putting a node to sleep ────────────────────────────────

def window_around_now():
    '''A sleep window that's open right now (crossing midnight is fine).'''
    now = datetime.now()
    return [(now - timedelta(hours=1)).strftime("%H:%M"), (now + timedelta(hours=1)).strftime("%H:%M")]


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setattr(communication_indoor, "ARCHIVE_DIR", tmp_path)
    monkeypatch.setattr(sync_indoor, "NODE_INFO_FILE", tmp_path / "node_info.json")
    path = tmp_path / "nodes.json"
    data = json.loads(nodes.EXAMPLE_FILE.read_text())
    data["nodes"]["3"] = esp32(link=["wifi", "radio"], battery_saver=True)
    path.write_text(json.dumps(data))
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE readings (ts TEXT, node_id INTEGER, sensor_type TEXT, key TEXT, value REAL)")
    svc = wifi_nodes.WifiNodes(conn, nodes_file=path, poll_interval=0.05, reply_timeout=2,
                               status_file=tmp_path / "wifi.json", data_file=tmp_path / "data.txt",
                               radio_status_file=tmp_path / "radio.json")
    return svc, path, tmp_path


async def firmware_like_node(port, seen):
    '''Answers polls; acknowledges a sleep, then leaves Wi-Fi like the firmware.'''
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b'{"t":"hello","q":0,"n":3}\n')
    await writer.drain()
    q = 1
    while line := await reader.readline():
        command = json.loads(line)
        seen.append(command)
        if command["t"] == "poll":
            replies = [{"t": "ts", "v": command["ts"]}, {"t": "sht", "tmp": 20.0, "rh": 40.0},
                       {"t": "batch_end", "exp": 1, "snt": 1}]
        elif command["t"] == "sleep":
            replies = [{"t": "sleep_ack", "ok": 1, "w": command["w"]}]
        else:
            replies = [{"t": "pong", "pq": command.get("q")}]
        for packet in replies:
            packet.update(q=q, n=3)
            q += 1
            writer.write((json.dumps(packet) + "\n").encode())
        await writer.drain()
        if command["t"] == "sleep":
            break
    writer.close()


def run_node(svc):
    async def scenario():
        server = await asyncio.start_server(svc.handle, "127.0.0.1", 0)
        seen = []
        await asyncio.wait_for(firmware_like_node(server.sockets[0].getsockname()[1], seen), 10)
        await asyncio.sleep(0.2)
        server.close()
        await server.wait_closed()
        return seen
    return asyncio.run(scenario())


def test_polls_say_the_preferred_link(service):
    svc, path, tmp_path = service

    async def scenario():
        server = await asyncio.start_server(svc.handle, "127.0.0.1", 0)
        reader, writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
        writer.write(b'{"t":"hello","q":0,"n":3}\n')
        await writer.drain()
        command = json.loads(await asyncio.wait_for(reader.readline(), 5))
        writer.close()
        server.close()
        return command
    command = asyncio.run(scenario())
    assert command["t"] == "poll" and command["pf"] == "wifi"


def test_the_wifi_service_puts_a_node_to_sleep_and_records_it(service, capsys):
    svc, path, tmp_path = service
    data = json.loads(path.read_text())
    data["nodes"]["3"]["sleep_window"] = window_around_now()
    path.write_text(json.dumps(data))
    seen = run_node(svc)
    assert [c["t"] for c in seen] == ["sleep"]
    wake = sync_indoor.get_node_info()["3"]["sleep_until"]
    assert wake == seen[0]["w"] and sync_indoor.recorded_wake(3) is not None
    out = capsys.readouterr().out
    assert "asleep until" in out and "connection dropped" not in out


def test_no_sleep_while_the_radio_is_in_use(service):
    svc, path, tmp_path = service
    from links import write_radio_status
    data = json.loads(path.read_text())
    data["nodes"]["3"].update(link=["radio", "wifi"], sleep_window=window_around_now())
    path.write_text(json.dumps(data))
    write_radio_status({3: True}, status_file=tmp_path / "radio.json")

    async def scenario():
        server = await asyncio.start_server(svc.handle, "127.0.0.1", 0)
        reader, writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
        writer.write(b'{"t":"hello","q":0,"n":3}\n')
        await writer.drain()
        command = json.loads(await asyncio.wait_for(reader.readline(), 5))
        writer.close()
        server.close()
        return command
    assert asyncio.run(scenario())["t"] == "ping"   # main.py sends the sleep over the radio
