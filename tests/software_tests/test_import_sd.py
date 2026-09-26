# tests/software_tests/test_import_sd.py
import json
import sqlite3

import communication_indoor
from communication_indoor import CommandManager
from import_sd import import_folder


def make_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE readings (ts TEXT, node_id INTEGER, sensor_type TEXT, key TEXT, value REAL)")
    return conn


def log_line(i):
    return json.dumps({"t": "rt", "tmp": 20 + i, "ts": f"2026-09-12T15:{i:02}:00"}) + "\n"


def test_skips_already_synced_part_of_sending_txt(tmp_path):
    lines = [log_line(i) for i in range(10)]
    (tmp_path / "sending.txt").write_text("".join(lines), newline="\n")
    synced = sum(len(l) for l in lines[:4])
    (tmp_path / "sync_cursor.txt").write_text(f"1 {synced}")
    (tmp_path / "data.txt").write_text(log_line(10) + log_line(11), newline="\n")

    conn = make_db()
    summary = import_folder(tmp_path, 1, conn)

    rows = conn.execute("SELECT ts, node_id, value FROM readings ORDER BY ts").fetchall()
    assert summary["imported"] == 8
    assert [r[2] for r in rows] == [20.0 + i for i in range(4, 12)]
    assert all(r[1] == 1 for r in rows)


def test_half_written_last_line_is_skipped(tmp_path):
    (tmp_path / "data.txt").write_text(log_line(0) + log_line(1) + '{"t":"rt","tm', newline="\n")
    conn = make_db()
    summary = import_folder(tmp_path, 1, conn)
    assert summary["imported"] == 2
    assert summary["skipped"] == 1
    assert summary["first_ts"] == "2026-09-12T15:00:00"
    assert summary["last_ts"] == "2026-09-12T15:01:00"


def test_dry_run_writes_nothing(tmp_path):
    (tmp_path / "data.txt").write_text(log_line(0), newline="\n")
    assert import_folder(tmp_path, 1, conn=None)["imported"] == 1


def test_sync_chunks_retry_faster_than_other_commands(tmp_path, monkeypatch):
    command_file = tmp_path / "cmd.json"
    monkeypatch.setattr(communication_indoor, "COMMAND_FILE", str(command_file))
    monkeypatch.setattr(communication_indoor.time, "sleep", lambda s: None)
    clock = [1000.0]
    monkeypatch.setattr(communication_indoor.time, "monotonic", lambda: clock[0])

    class Radio:
        sent = 0
        def send(self, data):
            Radio.sent += 1

    for command, expect_resend in (({"t": "sync", "n": 1}, True), ({"t": "poll", "n": 1}, False)):
        Radio.sent = 0
        command_file.write_text(json.dumps(command))
        cmd = CommandManager()
        cmd.check_and_forward(Radio())
        clock[0] += communication_indoor.SYNC_RETRY_INTERVAL
        cmd.check_and_forward(Radio())
        assert Radio.sent == (2 if expect_resend else 1), command["t"]
