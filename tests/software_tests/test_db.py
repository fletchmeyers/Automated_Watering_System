# tests/software_tests/test_db.py
import sqlite3

import db


def make_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE readings (ts TEXT, node_id INTEGER, sensor_type TEXT, key TEXT, value REAL)")
    return conn


def test_available_fields_are_per_node():
    conn = make_db()
    db.insert_batch(conn, [
        {"t": "batt", "n": 1, "ts": "2026-09-27T10:00:00", "v": 3.9, "soc": 80.0},
        {"t": "batt", "n": 2, "ts": "2026-09-27T10:00:00", "v": 4.1, "soc": 95.0},
        {"t": "s0",   "n": 1, "ts": "2026-09-27T10:00:00", "m": 900, "tmp": 21.0},
    ])
    fields = {(f["node_id"], f["sensor_type"], f["key"]) for f in db.get_available_fields(conn)}
    assert fields == {
        (1, "batt", "v"), (1, "batt", "soc"), (2, "batt", "v"), (2, "batt", "soc"),
        (1, "s0", "m"), (1, "s0", "tmp"),
    }


def stamp(minutes_ago):
    from datetime import datetime, timedelta
    return (datetime.now() - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S")


def test_a_minutes_window_only_covers_those_minutes():
    # Stored times have a "T"; a window that compared them against
    # datetime()'s "YYYY-MM-DD HH:MM:SS" took in everything since midnight.
    conn = make_db()
    db.insert_batch(conn, [{"t": "s0", "n": 1, "ts": stamp(m), "m": 800 + m} for m in (1, 3, 30, 300)])
    assert [r[4] for r in db.query_readings(conn, minutes=5, sensor_type="s0")] == [803, 801]
    assert len(db.query_readings(conn, minutes=360, sensor_type="s0")) == 4


def test_a_cursor_fetches_only_rows_added_since():
    conn = make_db()
    db.insert_batch(conn, [{"t": "s0", "n": 1, "ts": stamp(10), "m": 800}])
    cursor = db.latest_rowid(conn)
    db.insert_batch(conn, [{"t": "s0", "n": 1, "ts": stamp(1), "m": 810},
                           {"t": "s0", "n": 1, "ts": stamp(600), "m": 700}])   # an old one, synced late
    upto = db.latest_rowid(conn)
    db.insert_batch(conn, [{"t": "s0", "n": 1, "ts": stamp(0), "m": 820}])     # after the reply's cursor
    rows = db.query_readings(conn, after=cursor, upto=upto, key_only=None) if False else \
        db.query_readings(conn, after=cursor, upto=upto)
    assert sorted(r[4] for r in rows if r[3] == "m") == [700, 810]
    assert db.latest_rowid(make_db()) == 0


def test_available_fields_are_kept_and_only_new_rows_looked_at():
    conn = make_db()
    db.insert_batch(conn, [{"t": "s0", "n": 1, "ts": stamp(5), "m": 800, "tmp": 21.0}])
    fields = db.AvailableFields()
    assert {(f["node_id"], f["sensor_type"], f["key"]) for f in fields.get(conn)} == {(1, "s0", "m"), (1, "s0", "tmp")}

    scanned = []
    conn.set_trace_callback(scanned.append)
    assert len(fields.get(conn)) == 2                      # nothing new: no scan at all
    assert not any("DISTINCT" in q for q in scanned)

    db.insert_batch(conn, [{"t": "uv", "n": 3, "ts": stamp(1), "lux": 300.0}])
    assert (3, "uv", "lux") in {(f["node_id"], f["sensor_type"], f["key"]) for f in fields.get(conn)}
    assert any("DISTINCT" in q and "rowid > 2" in q for q in scanned)   # only past the first two rows
