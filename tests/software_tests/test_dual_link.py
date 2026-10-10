# tests/software_tests/test_dual_link.py
# Nodes with both Wi-Fi and the radio ("link": ["wifi", "radio"]): the
# nodes.json rules, which link the Pi uses (links.py), switching it with
# node_setup.py prefer, and the Wi-Fi service leaving a radio-first node to
# the radio.
import asyncio
import json
import os
import sqlite3
from datetime import datetime, timedelta

import pytest

import communication_indoor
import links
import node_wizard
import nodes
import wifi_nodes
from links import NodeList, active_link, write_radio_status
from nodes import (NodeConfigError, arduino_build_flags, check_node, links_of, load_nodes,
                   public_nodes, radio_node_ids, storage_of, sync_node_ids, wifi_node_ids)

RADIO_PINS = {"radio_cs": 17, "radio_irq": 21, "radio_rst": 20}


def dual_node(link=("wifi", "radio"), **changes):
    node = {"name": "Porch Pico W", "framework": "arduino", "board": "pico2w", "link": list(link),
            "sense_interval_s": 3, "log_interval_s": 60, "storage": "none", "pins": dict(RADIO_PINS)}
    node.update(changes)
    return node


# ── nodes.json ───────────────────────────────────────────────────────────────

def test_a_node_with_both_links_builds_with_wifi_and_its_radio():
    node = dual_node()
    check_node(20, node)
    flags = arduino_build_flags(20, node)
    assert "-D NODE_LINK_WIFI" in flags and "-D NODE_NO_RADIO" not in flags
    assert "-D RFM69_CS=17" in flags
    assert links_of(node) == ["wifi", "radio"]


def test_wifi_only_builds_without_the_radio_and_radio_only_without_wifi():
    wifi = dual_node(link=["wifi"], pins={})
    assert {"-D NODE_LINK_WIFI", "-D NODE_NO_RADIO"} <= set(arduino_build_flags(20, wifi))
    radio = dual_node(link=["radio"])
    assert not {"-D NODE_LINK_WIFI", "-D NODE_NO_RADIO"} & set(arduino_build_flags(20, radio))


def test_both_services_know_a_node_with_both_links():
    listed = {20: dual_node(storage="sd", pins={**RADIO_PINS, "sd_cs": 9})}
    assert radio_node_ids(listed) == wifi_node_ids(listed) == sync_node_ids(listed) == [20]
    without_storage = dual_node()
    del without_storage["storage"]
    assert storage_of(without_storage) == "sd"     # it has the radio, like any radio node


@pytest.mark.parametrize("node, message", [
    (dual_node(pins={}), 'needs pin "radio_cs"'),
    (dual_node(link=[]), "link must be one of"),
    (dual_node(link=["wifi", "wifi"]), "link must be one of"),
    (dual_node(link=["wifi", "lora"]), "link must be one of"),
    (dual_node(board="pico2"), "can't use Wi-Fi"),
    (dual_node(sleep_window=["19:00", "07:00"]), "doesn't sleep"),
])
def test_dual_link_mistakes_are_reported(node, message):
    with pytest.raises(NodeConfigError, match=message):
        check_node(20, node)


def test_the_dashboard_is_given_every_link_in_order():
    shown = public_nodes({20: dual_node(link=["radio", "wifi"])})[20]
    assert (shown["link"], shown["links"]) == ("radio", ["radio", "wifi"])


# ── Which link is in use ─────────────────────────────────────────────────────

NOW = datetime(2026, 10, 11, 12, 0, 0)


def stamp(seconds_ago):
    return (NOW - timedelta(seconds=seconds_ago)).strftime(links.ISO_FORMAT)


@pytest.fixture
def status(tmp_path, monkeypatch):
    '''Write the two status files as they'd be seconds_ago old.'''
    wifi_file, radio_file = tmp_path / "wifi.json", tmp_path / "radio.json"

    def write(wifi_seen=None, radio_reachable=None, radio_written=10):
        wifi_file.write_text(json.dumps(
            {} if wifi_seen is None else {"20": {"ip": "x", "since": stamp(3600), "last_seen": stamp(wifi_seen)}}))
        radio_file.write_text(json.dumps(
            {"_updated": stamp(radio_written),
             "nodes": {} if radio_reachable is None else {"20": {"reachable": radio_reachable}}}))

    def link(order):
        return active_link(20, dual_node(link=order), now=NOW, wifi_file=wifi_file, radio_file=radio_file)
    return write, link


def test_the_first_link_thats_up_is_used(status):
    write, link = status
    write(wifi_seen=30, radio_reachable=True)
    assert link(["wifi", "radio"]) == "wifi"
    assert link(["radio", "wifi"]) == "radio"


def test_wifi_that_hasnt_answered_for_three_minutes_is_down(status):
    write, link = status
    write(wifi_seen=200, radio_reachable=True)
    assert link(["wifi", "radio"]) == "radio"
    write(wifi_seen=None, radio_reachable=True)   # not connected at all
    assert link(["wifi", "radio"]) == "radio"


def test_an_unanswering_radio_or_a_stopped_radio_loop_is_down(status):
    write, link = status
    write(wifi_seen=30, radio_reachable=False)
    assert link(["radio", "wifi"]) == "wifi"
    write(wifi_seen=30, radio_reachable=True, radio_written=600)   # main.py stopped writing
    assert link(["radio", "wifi"]) == "wifi"


def test_when_nothing_is_up_the_first_link_keeps_trying(status):
    write, link = status
    write(wifi_seen=None, radio_reachable=False)
    assert link(["wifi", "radio"]) == "wifi"
    assert link(["radio", "wifi"]) == "radio"


def test_a_single_link_needs_no_status_files(tmp_path):
    missing = tmp_path / "nothing.json"
    assert active_link(1, {"link": "radio"}, wifi_file=missing, radio_file=missing) == "radio"
    assert active_link(3, {"link": "wifi"}, wifi_file=missing, radio_file=missing) == "wifi"


def test_the_radio_status_is_written_whole(tmp_path):
    path = tmp_path / "radio.json"
    write_radio_status({1: True, 20: False}, status_file=path)
    written = json.loads(path.read_text())
    assert written["nodes"] == {"1": {"reachable": True}, "20": {"reachable": False}}
    assert list(tmp_path.iterdir()) == [path]   # no temp file left


# ── Switching with node_setup.py prefer ──────────────────────────────────────

@pytest.fixture
def node_file(tmp_path):
    path = tmp_path / "nodes.json"
    data = json.loads(nodes.EXAMPLE_FILE.read_text())
    data["nodes"]["20"] = dual_node()
    path.write_text(json.dumps(data))
    return path


def test_prefer_puts_a_link_first(node_file):
    assert node_wizard.prefer_link(node_file, 20, "radio") == ["radio", "wifi"]
    assert links_of(load_nodes(node_file)[20]) == ["radio", "wifi"]
    assert node_wizard.prefer_link(node_file, 20, "wifi") == ["wifi", "radio"]


@pytest.mark.parametrize("node_id, link, message", [
    (1, "wifi", "has no Wi-Fi link"),          # node 1 is radio only
    (1, "radio", "nothing to switch"),
    (99, "wifi", "isn't in"),
])
def test_prefer_explains_what_it_cant_do(node_file, node_id, link, message):
    with pytest.raises(NodeConfigError, match=message):
        node_wizard.prefer_link(node_file, node_id, link)


def test_the_running_services_see_a_switch_without_a_restart(node_file):
    listed = NodeList(node_file, check_s=0)
    assert links_of(listed.get()[20]) == ["wifi", "radio"]
    node_wizard.prefer_link(node_file, 20, "radio")
    later = os.stat(node_file).st_mtime + 5            # a coarse clock may not tick in between
    os.utime(node_file, (later, later))
    assert links_of(listed.get()[20]) == ["radio", "wifi"]


def test_the_questions_offer_both_links_on_a_wifi_board(tmp_path, monkeypatch):
    from test_node_list import Typing
    monkeypatch.setattr(nodes, "NODES_FILE", tmp_path / "nodes.json")
    monkeypatch.setattr(nodes, "LEGACY_FILE", tmp_path / "old.json")
    # framework, link: both (radio first), ID, name, short, storage, pins (accepted), sense, RTC, save
    t = Typing("arduino", "radio,wifi", *[""] * 8)
    node_id = node_wizard.add_node(nodes.NODES_FILE, t.wizard(), boards=["pico2w"])
    added = load_nodes()[node_id]
    assert added["link"] == ["radio", "wifi"]
    assert set(RADIO_PINS) <= set(added["pins"])


# ── The Wi-Fi service with a node that has the radio too ─────────────────────

@pytest.fixture
def service(tmp_path, monkeypatch, node_file):
    monkeypatch.setattr(communication_indoor, "ARCHIVE_DIR", tmp_path)
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE readings (ts TEXT, node_id INTEGER, sensor_type TEXT, key TEXT, value REAL)")
    svc = wifi_nodes.WifiNodes(conn, nodes_file=node_file, poll_interval=0.05, reply_timeout=2,
                               status_file=tmp_path / "wifi.json", data_file=tmp_path / "data.txt",
                               radio_status_file=tmp_path / "radio.json")
    return svc, conn, tmp_path


async def node_answering(port, commands_to_answer, seen):
    '''Answers polls and pings like the firmware, then hangs up.'''
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b'{"t":"hello","q":0,"n":20}\n')
    await writer.drain()
    q = 1
    for _ in range(commands_to_answer):
        command = json.loads(await reader.readline())
        seen.append(command["t"])
        if command["t"] == "poll":
            replies = [{"t": "ts", "v": command["ts"]}, {"t": "sht", "tmp": 21.5, "rh": 40.0},
                       {"t": "batch_end", "exp": 1, "snt": 1}]
        else:
            replies = [{"t": "pong", "pq": command["q"]}]
        for packet in replies:
            packet.update(q=q, n=20)
            q += 1
            writer.write((json.dumps(packet) + "\n").encode())
        await writer.drain()
    writer.close()


def run_node(svc, commands):
    async def scenario():
        server = await asyncio.start_server(svc.handle, "127.0.0.1", 0)
        seen = []
        await asyncio.wait_for(node_answering(server.sockets[0].getsockname()[1], commands, seen), 10)
        await asyncio.sleep(0.2)
        server.close()
        await server.wait_closed()
        return seen
    return asyncio.run(scenario())


def test_a_radio_first_node_is_only_pinged_while_the_radio_answers(service, node_file):
    svc, conn, tmp_path = service
    node_wizard.prefer_link(node_file, 20, "radio")
    write_radio_status({20: True}, status_file=tmp_path / "radio.json")
    assert run_node(svc, 3) == ["ping", "ping", "ping"]
    assert conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 0   # the radio stores them


def test_wifi_takes_over_when_the_radio_stops_answering(service, node_file):
    svc, conn, tmp_path = service
    node_wizard.prefer_link(node_file, 20, "radio")
    write_radio_status({20: False}, status_file=tmp_path / "radio.json")
    assert run_node(svc, 2) == ["poll", "poll"]
    assert conn.execute("SELECT COUNT(*) FROM readings WHERE key = 'tmp'").fetchone()[0] == 2


def test_a_wifi_first_node_is_polled_over_wifi(service):
    svc, conn, tmp_path = service
    write_radio_status({20: True}, status_file=tmp_path / "radio.json")
    assert run_node(svc, 2) == ["poll", "poll"]
