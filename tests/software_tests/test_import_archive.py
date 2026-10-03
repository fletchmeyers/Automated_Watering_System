# tests/software_tests/test_import_archive.py
import json
import sqlite3

import db
from import_archive import find_files, import_files


def make_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE readings (ts TEXT, node_id INTEGER, sensor_type TEXT, key TEXT, value REAL)")
    return conn


def pkt(ts, t="rt", n=1, **values):
    return {"t": t, "n": n, "ts": ts, "q": 1, **(values or {"tmp": 20.0})}


def write(path, packets):
    path.write_text("".join(json.dumps(p) + "\n" for p in packets), newline="\n")
    return path


def test_adds_only_packets_the_database_lacks(tmp_path):
    conn = make_db()
    db.insert_batch(conn, [pkt("2026-08-14T00:01:02"), pkt("2026-08-14T00:01:02", t="sht", tmp=23.6, rh=88.1)])
    f = write(tmp_path / "data_2026-08-14.txt", [
        pkt("2026-08-14T00:01:02"),                          # already there
        pkt("2026-08-14T00:01:02", t="sht", tmp=23.6, rh=88.1),  # already there
        pkt("2026-08-14T00:01:02", t="voc", voc=33037),     # same moment, new sensor
        pkt("2026-08-14T00:02:02"),                          # new moment
    ])
    summary = import_files([f], conn)
    assert summary["added"] == 2
    assert summary["already_present"] == 2
    assert conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 2 + 1 + 1 + 1   # sht has 2 rows


def test_overlapping_files_and_reruns_add_nothing_twice(tmp_path):
    packets = [pkt(f"2026-07-08T18:0{i}:42") for i in range(5)]
    a = write(tmp_path / "data_2026-07-08.txt", packets)
    b = write(tmp_path / "root_archive.txt", packets[2:] + [pkt("2026-07-09T00:00:20")])
    conn = make_db()
    first = import_files([a, b], conn)
    again = import_files([a, b], conn)
    assert first["added"] == 6
    assert again["added"] == 0 and again["already_present"] == 9
    assert conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 6


def test_skips_unknown_timestamps_missing_nodes_and_bad_lines(tmp_path):
    f = tmp_path / "data.txt"
    f.write_text(
        json.dumps(pkt("unknown")) + "\n"
        + json.dumps({"t": "rt", "ts": "2026-07-10T08:07:44", "tmp": 20}) + "\n"
        + json.dumps(pkt("2026-07-10T08:07:44")) + "\n"
        + '{"t": "rt", "tm\n', newline="\n")
    summary = import_files([f], make_db())
    assert summary["added"] == 1
    assert summary["no_timestamp"] == 1
    assert summary["no_node"] == 1
    assert summary["unreadable"] == 1


def test_since_until_and_dry_run(tmp_path):
    f = write(tmp_path / "d.txt", [pkt("2026-03-08T13:05:19", t="batt", v=3.84, soc=54.7),
                                   pkt("2026-07-08T18:00:42"), pkt("2026-08-01T09:49:15")])
    conn = make_db()
    dry = import_files([f], conn, since="2026-07-01", until="2026-08-01", write=False)
    assert dry["added"] == 1 and dry["outside_range"] == 2
    assert dry["per_month"] == {"2026-07": 1}
    assert conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 0


def test_find_files_recurses_into_folders(tmp_path):
    (tmp_path / "root_archive" / "old_seed").mkdir(parents=True)
    write(tmp_path / "root_archive" / "data_2026-07-08.txt", [])
    write(tmp_path / "root_archive" / "old_seed" / "data_2026-07-06.txt", [])
    (tmp_path / "root_archive" / "notes.md").write_text("x")
    names = [p.name for p in find_files([tmp_path / "root_archive", tmp_path / "missing"])]
    assert sorted(names) == ["data_2026-07-06.txt", "data_2026-07-08.txt"]
