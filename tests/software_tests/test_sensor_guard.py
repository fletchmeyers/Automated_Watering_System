# tests/software_tests/test_sensor_guard.py
"""Sensors with bad connections: the node takes them offline (sensor_guard.py),
tells the Pi with each poll, and the Pi records it for the dashboard."""
import json
import sqlite3

import pytest

import communication_garden as garden
import communication_indoor
import sync_indoor
from communication_garden import PacketSender
from communication_indoor import BatchReceiver
from sensor_guard import RETRY_MINUTES, SLOW_S, STRIKES, SensorGuard


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_three_bad_reads_in_a_row_take_a_sensor_offline():
    clock = Clock()
    g = SensorGuard(clock)
    assert g.result("sht", False, 0.1) is None              # one failure...
    assert g.result("sht", True, 0.05) is None              # ...forgiven by a good read
    assert g.result("sht", False, 0.1) is None
    assert g.result("sht", True, SLOW_S + 2) is None        # slow counts too
    note = g.result("sht", False, 0.1)
    assert "sht offline" in note and f"{STRIKES} bad reads" in note
    assert g.offline() == ["sht"] and not g.due("sht")
    assert g.due("uv")                                      # the others carry on


def test_an_offline_sensor_is_retried_less_and_less_often_until_it_answers():
    clock = Clock()
    g = SensorGuard(clock)
    for _ in range(STRIKES):
        g.result("s1", False, 0.1)
    waits = []
    for _ in range(4):
        start = clock.t
        while not g.due("s1"):
            clock.t += 60
        waits.append(round((clock.t - start) / 60))
        assert "still isn't answering" in g.result("s1", True, SLOW_S + 1)
    assert waits == [RETRY_MINUTES[0], RETRY_MINUTES[1], RETRY_MINUTES[2], RETRY_MINUTES[2]]

    while not g.due("s1"):
        clock.t += 60
    assert "answering again" in g.result("s1", True, 0.1)
    assert g.offline() == [] and g.due("s1")
    assert g.result("s1", False, 0.1) is None               # needs three in a row again


# ── The poll answer ──────────────────────────────────────────────────────────

class Radio:
    def __init__(self):
        self.sent = []

    def send(self, data):
        self.sent.append(json.loads(bytes(data)))


@pytest.fixture
def node_state(monkeypatch):
    monkeypatch.setattr(garden.time, "sleep", lambda s: None)
    monkeypatch.setattr(garden, "latest_reading", [{"t": "uv", "lux": 300.0}])
    monkeypatch.setattr(garden, "sensors_offline", [])


def test_the_poll_answer_lists_offline_sensors(node_state, monkeypatch):
    monkeypatch.setattr(garden, "sensors_offline", ["sht", "voc", "s0", "s1", "s2", "pw0", "pw1"])
    radio = Radio()
    garden.send_latest(PacketSender(1, radio), "2026-10-14T12:00:00")
    kinds = [p["t"] for p in radio.sent]
    assert kinds == ["ts", "uv", "off", "batch_end"]
    assert radio.sent[2]["s"] == "sht,voc,s0,s1,s2,pw0,pw1"
    assert radio.sent[3]["exp"] == 2


def test_no_offline_sensors_no_extra_packet(node_state):
    radio = Radio()
    garden.send_latest(PacketSender(1, radio), "2026-10-14T12:00:00")
    assert [p["t"] for p in radio.sent] == ["ts", "uv", "batch_end"]


# ── The Pi records it ────────────────────────────────────────────────────────

@pytest.fixture
def pi(tmp_path, monkeypatch):
    monkeypatch.setattr(sync_indoor, "NODE_INFO_FILE", tmp_path / "node_info.json")
    monkeypatch.setattr(communication_indoor, "ARCHIVE_DIR", tmp_path)
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE readings (ts TEXT, node_id INTEGER, sensor_type TEXT, key TEXT, value REAL)")
    return BatchReceiver(data_file=tmp_path / "data.txt", db_conn=conn), conn


def answer(batch, *packets):
    batch.open_batch("2026-10-14T12:00:00")
    for q, p in enumerate(packets):
        batch.collect({**p, "q": q, "n": 1})
    batch.close_batch({"t": "batch_end", "exp": len(packets), "snt": len(packets)})
    return batch.flush(radio=None)


def test_the_pi_records_offline_sensors_and_keeps_them_out_of_the_readings(pi, capsys):
    batch, conn = pi
    written = answer(batch, {"t": "uv", "lux": 300.0}, {"t": "off", "s": "sht,s1"})
    assert [p["t"] for p in written] == ["uv"]
    assert conn.execute("SELECT DISTINCT sensor_type FROM readings").fetchall() == [("uv",)]
    off = sync_indoor.get_node_info()["1"]["sensors_off"]
    assert set(off) == {"sht", "s1"}
    assert "took sensor sht offline" in capsys.readouterr().out

    answer(batch, {"t": "uv", "lux": 310.0}, {"t": "off", "s": "s1"})   # sht came back
    later = sync_indoor.get_node_info()["1"]["sensors_off"]
    assert later == {"s1": off["s1"]}                                    # s1 keeps its first time
    assert "sensor sht is answering again" in capsys.readouterr().out

    answer(batch, {"t": "uv", "lux": 320.0})                             # no "off": all fine
    assert sync_indoor.get_node_info()["1"]["sensors_off"] == {}


def test_a_node_that_never_sends_the_list_records_nothing(pi):
    batch, conn = pi
    answer(batch, {"t": "uv", "lux": 300.0})
    assert "sensors_off" not in sync_indoor.get_node_info().get("1", {})


# ── VOC compensation doesn't read the SHT40 again ────────────────────────────

class Sgp:
    def __init__(self):
        self.calls = []

    def measure_raw(self, temperature, relative_humidity):
        self.calls.append((temperature, relative_humidity))
        return 30000

    @property
    def raw(self):
        self.calls.append(None)
        return 29000


class BrokenSht:
    @property
    def measurements(self):
        raise OSError("I2C timeout")


def test_voc_uses_the_sht40s_reading_from_this_cycle(monkeypatch):
    monkeypatch.setattr(garden, "last_sht", None)
    sgp = Sgp()
    read_voc = garden.make_sgp40_compensated_fn(BrokenSht(), sgp)
    assert read_voc()["voc"] == 29000 and sgp.calls == [None]     # no SHT40 reading: uncompensated

    class Sht:
        measurements = (21.5, 40.0)
    garden.package_sht40_data(Sht())
    assert read_voc()["voc"] == 30000 and sgp.calls[-1] == (21.5, 40.0)
