# tests/software_tests/test_radio_signal.py
"""How strongly each node's radio packets reach the Pi (RSSI): the radio
loop's SignalWatch and the ping test."""
import json

import communication_indoor
from communication_indoor import SignalWatch, run_ping_test, signal_word


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_a_node_is_reported_when_first_heard_then_every_few_minutes(capsys):
    clock, recorded = Clock(), []
    watch = SignalWatch(report_every=300, clock=clock, record=lambda n, **kw: recorded.append((n, kw)))
    assert "[SIGNAL] Node 1: -88 dBm (fair) over 1 packet, weakest -88 dBm." == watch.heard(1, -88)
    for rssi in (-90, -95, -85):
        clock.t += 60
        assert watch.heard(1, rssi) is None          # not yet: once every 5 minutes
    clock.t += 200
    note = watch.heard(1, -98)
    assert note == "[SIGNAL] Node 1: -92 dBm (weak) over 4 packets, weakest -98 dBm."
    assert recorded[-1][0] == 1
    assert recorded[-1][1]["rssi_dbm"] == -92 and recorded[-1][1]["rssi_weakest_dbm"] == -98


def test_each_node_has_its_own(capsys):
    watch = SignalWatch(clock=Clock())
    assert watch.heard(1, -60) is not None
    assert watch.heard(2, -95) is not None
    assert watch.heard(1, -61) is None
    assert watch.heard(None, -70) is None and watch.heard(3, None) is None


def test_the_words():
    assert [signal_word(d) for d in (-60, -80, -85, -90, -91)] == ["strong", "strong", "fair", "fair", "weak"]


class PingRadio:
    '''Answers pings with a pong at a given signal, or not at all.'''
    def __init__(self, answers):
        self.answers = list(answers)   # per ping: an RSSI, or None for no pong
        self.last_rssi = None
        self._pending = None

    def send(self, data):
        ping = json.loads(data)
        rssi = self.answers.pop(0)
        self._pending = None if rssi is None else (rssi, ping["q"])

    def receive(self, with_header=True, timeout=None):
        if self._pending is None:
            return None
        rssi, q = self._pending
        self._pending = None
        self.last_rssi = rssi
        return b"\xff\xff\x00\x00" + json.dumps({"t": "pong", "q": 1, "n": 1, "pq": q}).encode()


def test_the_ping_test_reports_the_signal(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(communication_indoor, "PING_PROGRESS_FILE", str(tmp_path / "progress.json"))
    result = run_ping_test(PingRadio([-70, None, -90, -80]), node_id=1, count=4, timeout=0.2)
    assert result["hits"] == 3 and result["avg_rssi_dbm"] == -80
    assert [r.get("rssi_dbm") for r in result["results"]] == [-70, None, -90, -80]
    assert "signal -80 dBm" in capsys.readouterr().out


def test_a_ping_test_with_no_pongs_has_no_signal(monkeypatch, tmp_path):
    monkeypatch.setattr(communication_indoor, "PING_PROGRESS_FILE", str(tmp_path / "progress.json"))
    result = run_ping_test(PingRadio([None, None]), node_id=1, count=2, timeout=0.05)
    assert result["hits"] == 0 and result["avg_rssi_dbm"] is None


def test_each_report_is_stored_as_a_reading():
    '''So the signal has a history: the node's row averages it and the
    analysis panel plots it ({"t": "sig"}, every report).'''
    import sqlite3
    import db
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE readings (ts TEXT, node_id INTEGER, sensor_type TEXT, key TEXT, value REAL)")
    clock = Clock()
    watch = SignalWatch(clock=clock, store=lambda packet: db.insert_packet(conn, packet))
    watch.heard(2, -76)
    clock.t += 301
    watch.heard(2, -70)
    rows = conn.execute("SELECT node_id, sensor_type, key, value FROM readings ORDER BY rowid").fetchall()
    assert rows == [(2, "sig", "rssi", -76.0), (2, "sig", "weakest", -76.0),
                    (2, "sig", "rssi", -70.0), (2, "sig", "weakest", -70.0)]

