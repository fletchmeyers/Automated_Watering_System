# tests/software_tests/test_radio_msgpack.py
"""MessagePack over the radio (nodes.json radio_format): the Pi's setting and
poll code, the CircuitPython node's packets, and the Pi reading either."""
import json

import pytest

import communication_garden as garden
from communication_garden import PacketSender, SerialLink
from communication_indoor import BatchReceiver, FragmentReassembler, decode_packet
from link_power import LinkPower
from nodes import NodeConfigError, check_node, link_plan, public_nodes

msgpack = pytest.importorskip("msgpack")   # the radio venv has it; skipped where it isn't installed


def radio_node(**changes):
    node = {"name": "M0", "framework": "arduino", "board": "feather_m0", "sense_interval_s": 3,
            "log_interval_s": 300, "pins": {"radio_cs": 9, "radio_irq": 6, "radio_rst": 11, "sd_cs": 10}}
    node.update(changes)
    return node


# ── The setting and the poll code ────────────────────────────────────────────

def test_the_poll_code_carries_the_format():
    assert link_plan(radio_node()) is None                                  # JSON: nothing to say
    assert link_plan(radio_node(radio_format="msgpack")) == "m"
    assert link_plan(radio_node(radio_format="json")) is None
    both = dict(radio_node(), link=["radio", "wifi"], board="feather_m0_wifi")
    assert link_plan({**both, "radio_format": "msgpack"}) == "rm"
    assert link_plan({**both, "battery_saver": True, "wifi_check_hours": 24,
                      "radio_format": "msgpack"}) == "R24m"


@pytest.mark.parametrize("changes, message", [
    ({"radio_format": "csv"}, "radio_format must be one of"),
    ({"radio_format": "msgpack", "link": "wifi", "board": "feather_esp32_v2"}, "for nodes with the radio"),
])
def test_radio_format_mistakes(changes, message):
    with pytest.raises(NodeConfigError, match=message):
        check_node(2, radio_node(**changes))


def test_the_dashboard_hears_the_format():
    assert public_nodes({2: radio_node(radio_format="msgpack")})[2]["radio_format"] == "msgpack"


def test_the_link_plan_still_reads_with_an_m_on_the_end():
    power = LinkPower(["radio", "wifi"])
    power.apply("R24m")
    assert (power.preferred, power.saver, power.check_hours) == ("radio", True, 24)


def test_node_setup_format_sets_and_clears_it(tmp_path):
    import node_wizard
    path = tmp_path / "nodes.json"
    path.write_text(json.dumps({"nodes": {"2": radio_node()}}))
    assert node_wizard.set_radio_format(path, 2, "msgpack")["radio_format"] == "msgpack"
    assert "radio_format" not in node_wizard.set_radio_format(path, 2, "json")   # JSON is the default


# ── The CircuitPython node's packets ─────────────────────────────────────────

class Radio:
    def __init__(self):
        self.sent = []

    def send(self, data):
        assert len(data) <= 60
        self.sent.append(bytes(data))


class Wire:
    max_bytes = None   # USB and Wi-Fi: no packet size limit

    def __init__(self):
        self.sent = []

    def write(self, data):
        self.sent.append(data)


class CircuitPythonMsgpack:
    '''CircuitPython's built-in msgpack: the same pack/unpack, but its floats
    are 32-bit, so it writes 5-byte floats where desktop Python writes 9.'''
    @staticmethod
    def pack(obj, stream):
        stream.write(msgpack.packb(obj, use_single_float=True))

    unpack = staticmethod(msgpack.unpack)


@pytest.fixture
def node_msgpack(monkeypatch):
    monkeypatch.setattr(garden, "msgpack", CircuitPythonMsgpack)
    monkeypatch.setattr(garden, "radio_msgpack", False)
    monkeypatch.setattr(garden.time, "sleep", lambda s: None)


def test_the_poll_switches_the_radio_and_only_the_radio(node_msgpack, capsys):
    reading = {"t": "sht", "tmp": 21.53, "rh": 40.1}
    radio, wire = Radio(), Wire()
    radio_sender, usb_sender = PacketSender(1, radio), PacketSender(1, SerialLink(wire))

    radio_sender.send(dict(reading))
    as_json = radio.sent[-1]
    garden.set_radio_format(True)
    assert "sending MessagePack" in capsys.readouterr().out
    radio_sender.send(dict(reading))
    usb_sender.send(dict(reading))

    as_msgpack = radio.sent[-1]
    assert as_json[:1] == b"{" and as_msgpack[:1] != b"{"
    assert len(as_msgpack) <= len(as_json) * 0.7             # 30 bytes instead of 45
    assert wire.sent[-1][:1] == b"{"                          # USB stays JSON
    packet = decode_packet(as_msgpack)
    assert packet == {"t": "sht", "q": 1, "n": 1, "tmp": 21.53, "rh": 40.1}

    garden.set_radio_format(False)                            # and back
    radio_sender.send(dict(reading))
    assert radio.sent[-1][:1] == b"{"


def test_without_a_msgpack_module_it_stays_json(node_msgpack, monkeypatch, capsys):
    monkeypatch.setattr(garden, "msgpack", None)
    garden.set_radio_format(True)
    assert garden.radio_msgpack is False
    assert "no msgpack module" in capsys.readouterr().out


def test_a_long_packet_is_split_and_comes_back_whole(node_msgpack):
    garden.set_radio_format(True)
    radio = Radio()
    big = {"t": "off", "s": ",".join(["sht", "voc", "uv", "s0", "s1", "s2", "pw0", "pw1", "pw2", "pw3"] * 2)}
    PacketSender(1, radio).send(big)
    assert len(radio.sent) > 1 and all(p[:1] == b"~" for p in radio.sent)
    frags = FragmentReassembler()
    whole = None
    for payload in radio.sent:
        whole = frags.feed(payload)
    assert decode_packet(whole)["s"] == big["s"]


def test_sync_lines_go_as_msgpack_too_and_stay_sync_lines(node_msgpack):
    garden.set_radio_format(True)
    radio = Radio()
    line = b'{"t":"sht","tmp":21.5,"rh":40.0,"ts":"2026-10-14T12:00:00"}'
    PacketSender(1, radio).send_line(line, 3)
    record = decode_packet(radio.sent[-1])
    assert record == {"t": "sht", "tmp": 21.5, "rh": 40.0, "ts": "2026-10-14T12:00:00", "j": 3}
    assert "q" not in record                                  # how the Pi tells a sync line from a reading


def test_a_whole_poll_answer_in_msgpack_is_stored(node_msgpack, tmp_path, monkeypatch):
    import communication_indoor
    monkeypatch.setattr(communication_indoor, "ARCHIVE_DIR", tmp_path)
    monkeypatch.setattr(garden, "latest_reading", [{"t": "sht", "tmp": 21.53, "rh": 40.1},
                                                   {"t": "uv", "uv": 12, "uvi": 0.5, "lux": 812.4}])
    garden.set_radio_format(True)
    radio = Radio()
    garden.send_latest(PacketSender(1, radio), "2026-10-14T12:00:00")

    batch = BatchReceiver(data_file=tmp_path / "data.txt")
    written = None
    for payload in radio.sent:
        data = decode_packet(payload)
        if data["t"] == "ts":
            batch.open_batch(data["v"])
        elif data["t"] == "batch_end":
            batch.close_batch(data)
            written = batch.flush(radio=None)
        else:
            batch.collect(data)
    assert [(p["t"], p.get("tmp"), p.get("lux")) for p in written] == [("sht", 21.53, None), ("uv", None, 812.4)]


# ── The Pi reads either ──────────────────────────────────────────────────────

def test_the_pi_reads_json_and_msgpack():
    assert decode_packet(b'{"t":"pong","q":3,"n":2,"pq":7}') == {"t": "pong", "q": 3, "n": 2, "pq": 7}
    # An Arduino node's floats are 32-bit; they come back as written.
    payload = msgpack.packb({"t": "batt", "q": 4, "n": 2, "v": 3.71, "soc": 5.3}, use_single_float=True)
    assert decode_packet(payload) == {"t": "batt", "q": 4, "n": 2, "v": 3.71, "soc": 5.3}


def test_garbage_is_an_error_not_a_packet():
    with pytest.raises(Exception):
        decode_packet(msgpack.packb([1, 2, 3]))


def test_the_pi_says_which_format_each_node_sends(capsys):
    from communication_indoor import FormatWatch
    watch = FormatWatch()
    as_json = b'{"t":"sht","q":1,"n":3,"tmp":21.5,"rh":40.1}'
    assert "Node 3 is sending JSON (44 bytes for its sht packet)" in watch.seen(as_json, decode_packet(as_json))
    assert watch.seen(as_json, decode_packet(as_json)) is None            # said once
    as_msgpack = msgpack.packb({"t": "sht", "q": 2, "n": 3, "tmp": 21.5, "rh": 40.1}, use_single_float=True)
    assert "Node 3 is sending MessagePack (30 bytes" in watch.seen(as_msgpack, decode_packet(as_msgpack))
    sync_line = msgpack.packb({"t": "sht", "tmp": 21.5, "ts": "2026-10-22T18:00:00", "j": 0})
    assert watch.seen(sync_line, decode_packet(sync_line)) is None        # sync lines don't count
