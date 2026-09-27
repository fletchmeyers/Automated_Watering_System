# tests/software_tests/test_node_info.py
"""Storage reports ("info"): the Pico's reply, and the Pi recording it."""
import json
import os

import communication_garden as garden
import communication_indoor
import sync_indoor
from communication_garden import PacketSender, send_storage_info
from communication_indoor import CommandManager, FragmentReassembler


class Radio:
    def __init__(self):
        self.sent = []

    def send(self, data):
        assert len(data) <= 60
        self.sent.append(bytes(data))


def test_pico_reports_log_bytes_and_card_space(tmp_path, monkeypatch):
    (tmp_path / "data.txt").write_text("x" * 100)
    (tmp_path / "sending.txt").write_text("y" * 250)
    monkeypatch.setattr(garden, "SD_DATA_FILE", str(tmp_path / "data.txt"))
    monkeypatch.setattr(garden, "SD_SENDING_FILE", str(tmp_path / "sending.txt"))
    monkeypatch.setattr(garden.time, "sleep", lambda s: None)
    # statvfs: block size 4096, 3,900,000 blocks, 2,000,000 free (a 16 GB card)
    monkeypatch.setattr(os, "statvfs", lambda path: (4096, 0, 3_900_000, 2_000_000), raising=False)

    radio = Radio()
    send_storage_info(PacketSender(1, radio))

    # Big card sizes push the packet past 60 bytes — it must arrive whole.
    frags = FragmentReassembler()
    packet = None
    for payload in radio.sent:
        packet = frags.feed(payload) if payload[:1] == b"~" else payload
    info = json.loads(packet)
    assert info["t"] == "info" and info["n"] == 1
    assert info["ub"] == 350
    assert info["fb"] == 4096 * 2_000_000
    assert info["tb"] == 4096 * 3_900_000


def test_pi_saves_latest_info_per_node(tmp_path, monkeypatch):
    monkeypatch.setattr(sync_indoor, "NODE_INFO_FILE", tmp_path / "node_info.json")
    sync_indoor.save_node_info({"t": "info", "n": 2, "ub": 640, "fb": 130176, "tb": 130816})
    sync_indoor.save_node_info({"t": "info", "n": 1, "ub": 350, "fb": 10, "tb": 20})
    sync_indoor.save_node_info({"t": "info", "n": 2, "ub": 128, "fb": 130688, "tb": 130816})

    info = sync_indoor.get_node_info()
    assert set(info) == {"1", "2"}
    assert info["2"]["ub"] == 128
    assert "at" in info["2"]


def test_info_reply_clears_pending_info_command(tmp_path, monkeypatch):
    command_file = tmp_path / "cmd.json"
    monkeypatch.setattr(communication_indoor, "COMMAND_FILE", str(command_file))
    monkeypatch.setattr(communication_indoor.time, "sleep", lambda s: None)
    command_file.write_text(json.dumps({"t": "info", "n": 2}))

    cmd = CommandManager()
    cmd.check_and_forward(Radio())
    assert cmd.pending["t"] == "info"
    assert cmd.handle_ack({"t": "info", "n": 2, "ub": 0, "fb": 1, "tb": 1})
    assert cmd.pending is None
    assert not command_file.exists()


# ── Addressing ───────────────────────────────────────────────────────────────

def test_pico_ignores_packets_not_addressed_to_it():
    from sync_garden import dispatch_command
    sender = PacketSender(1, Radio())
    calls = []
    def send_latest(s, ts):
        calls.append(ts)
    for packet in ({"t": "poll", "ts": "2026-09-27T09:00:00", "n": 2},          # another node's command
                   {"t": "vbat", "q": 5, "n": 2, "v": 4.1},                      # another node's reply
                   {"t": "poll", "ts": "2026-09-27T09:00:00"}):                  # no address at all
        dispatch_command(packet, sender, None, None, lambda: "ts", send_latest, None, 1)
    assert calls == []
    dispatch_command({"t": "poll", "ts": "2026-09-27T09:00:00", "n": 1},
                     sender, None, None, lambda: "ts", send_latest, None, 1)
    assert calls == ["ts"]


def test_poll_is_stamped_when_sent_not_when_queued(tmp_path, monkeypatch):
    command_file = tmp_path / "cmd.json"
    monkeypatch.setattr(communication_indoor, "COMMAND_FILE", str(command_file))
    monkeypatch.setattr(communication_indoor.time, "sleep", lambda s: None)
    command_file.write_text(json.dumps({"t": "poll", "ts": "2000-01-01T00:00:00", "n": 1}))

    radio = Radio()
    CommandManager().check_and_forward(radio)
    sent = json.loads(radio.sent[0])
    assert sent["ts"] != "2000-01-01T00:00:00"
    assert sent["ts"][:4] >= "2026"
