# tests/software_tests/test_node_events.py
"""A node's notes about itself (circuitpython/node_events.py), and the Pi
taking them out of the synced readings into its own log."""
import json
import sqlite3
import sys
import types

import pytest

import communication_garden as garden
import communication_indoor
import node_events
from communication_garden import PacketSender


@pytest.fixture
def sd(tmp_path, monkeypatch):
    monkeypatch.setattr(garden, "SD_DATA_FILE", str(tmp_path / "data.txt"))
    monkeypatch.setattr(garden, "SD_SENDING_FILE", str(tmp_path / "sending.txt"))
    monkeypatch.setattr(garden, "SD_CURSOR_FILE", str(tmp_path / "sync_cursor.txt"))
    monkeypatch.setattr(node_events, "_timestamp", lambda: "2026-10-25T12:03:11")
    return tmp_path


def logged(sd):
    return [json.loads(line) for line in (sd / "data.txt").read_text().splitlines()]


def test_a_note_goes_in_the_sd_log(sd, capsys):
    node_events.record("sensor sht offline: 3 bad reads in a row")
    assert logged(sd) == [{"t": "ev", "m": "sensor sht offline: 3 bad reads in a row",
                           "ts": "2026-10-25T12:03:11"}]
    assert "[EVENT] sensor sht offline" in capsys.readouterr().out


def test_a_long_note_keeps_its_end(sd):
    node_events.record("x" * 500 + " the cause")
    note = logged(sd)[0]["m"]
    assert len(note) == node_events.MAX_CHARS and note.endswith("the cause") and note.startswith("...")


def test_the_start_up_note_says_why_it_restarted(sd, monkeypatch):
    monkeypatch.setitem(sys.modules, "microcontroller", types.SimpleNamespace(
        cpu=types.SimpleNamespace(reset_reason="microcontroller.ResetReason.WATCHDOG")))
    monkeypatch.setitem(sys.modules, "supervisor", types.SimpleNamespace(
        runtime=types.SimpleNamespace(usb_connected=False)))
    monkeypatch.setattr(node_events, "os", None, raising=False)
    node_events.boot()
    note = logged(sd)[0]["m"]
    assert note.startswith("started, reset reason WATCHDOG") and note.endswith("no USB")


def test_a_crash_note_has_the_error(sd):
    try:
        {}["voc"]
    except KeyError as e:
        node_events.crashed(e)
    note = logged(sd)[0]["m"]
    assert note.startswith("crashed: ") and "KeyError" in note and "voc" in note


def test_only_the_first_failed_radio_send_is_noted(sd, monkeypatch):
    monkeypatch.setattr(garden, "send_failed", False)

    class DeadRadio:
        def send(self, data):
            raise RuntimeError("radio didn't answer")

    sender = PacketSender(1, DeadRadio())
    sender.send({"t": "pong", "pq": 1})
    sender.send({"t": "pong", "pq": 2})
    notes = [line["m"] for line in logged(sd)]
    assert len(notes) == 1 and notes[0].startswith("radio send failed") and "didn't answer" in notes[0]


# ── The Pi ───────────────────────────────────────────────────────────────────

def test_the_pi_keeps_events_out_of_the_readings(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(communication_indoor, "ARCHIVE_DIR", tmp_path)
    monkeypatch.setattr(communication_indoor, "NODE_EVENTS_FILE", tmp_path / "node_events.txt")
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE readings (ts TEXT, node_id INTEGER, sensor_type TEXT, key TEXT, value REAL)")
    lines = [{"t": "rt", "tmp": 22.0, "ts": "2026-10-25T12:03:00"},
             {"t": "ev", "m": "started, reset reason POWER_ON, no USB", "ts": "2026-10-25T12:03:11"}]
    communication_indoor.store_sync_lines(1, lines, conn)
    assert conn.execute("SELECT sensor_type FROM readings").fetchall() == [("rt",)]
    assert (tmp_path / "node_events.txt").read_text() == \
        "2026-10-25T12:03:11  node 1  started, reset reason POWER_ON, no USB\n"
    assert "[EVENT] Node 1 at 2026-10-25T12:03:11: started" in capsys.readouterr().out


def test_an_event_comes_across_in_a_usb_pull(sd, tmp_path, monkeypatch):
    '''From the node's SD log to the Pi's events file, through the real sync code.'''
    import usb_sync
    from test_usb_sync import PicoOnUsb
    monkeypatch.setattr(garden.time, "sleep", lambda s: None)
    monkeypatch.setattr(communication_indoor, "ARCHIVE_DIR", tmp_path)
    monkeypatch.setattr(communication_indoor, "NODE_EVENTS_FILE", tmp_path / "node_events.txt")
    garden.append_to_sd([{"t": "rt", "tmp": 22.0}], "2026-10-25T12:03:00")
    node_events.record("crashed: RuntimeError: Timeout during packet send")
    stored = usb_sync.sync_node(usb_sync.UsbNode(PicoOnUsb()), 1, db_conn=None)
    assert stored == 2                                        # both lines came across
    assert "node 1  crashed: RuntimeError: Timeout" in (tmp_path / "node_events.txt").read_text()


def test_the_events_file_is_copied_to_the_node():
    import node_setup
    assert "node_events.py" in node_setup.CIRCUITPY_FILES
