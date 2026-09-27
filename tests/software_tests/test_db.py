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
