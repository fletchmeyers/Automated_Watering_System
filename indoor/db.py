'''
db.py — SQLite storage for sensor readings, on the Pi.

Long-format schema (ts, node_id, sensor_type, key, value), matching the
`readings` table created in the SQLite migration's first step.

Not wired into the live system yet — this module is standalone so it can
be tested on its own with fake packets before touching
communication_indoor.py's BatchReceiver.
'''

import sqlite3
from pathlib import Path

DB_FILE = Path(__file__).parent / "sensors.db"

# Packet keys that are metadata, not sensor readings — never turned into rows.
_META_KEYS = {"t", "q", "n", "ts"}


def get_connection():
    '''
    Open a connection to sensors.db. Call this once per process (e.g. once
    at startup in main.py) and reuse the connection — don't open a fresh
    one per packet.
    '''
    return sqlite3.connect(DB_FILE)


def _rows_for_packet(packet):
    '''
    Turn one packet dict into a list of (ts, node_id, sensor_type, key, value)
    tuples, ready for an INSERT. Returns [] if the packet is missing required
    fields or has no usable numeric values.
    '''
    ts          = packet.get("ts")
    node_id     = packet.get("n")
    sensor_type = packet.get("t")

    if ts is None or node_id is None or sensor_type is None:
        print(f"[DB] Skipping packet missing ts/n/t: {packet}")
        return []

    rows = []
    for key, value in packet.items():
        if key in _META_KEYS:
            continue
        if not isinstance(value, (int, float)):
            print(f"[DB] Skipping non-numeric field {key!r}={value!r} "
                  f"in {sensor_type} packet")
            continue
        rows.append((ts, node_id, sensor_type, key, float(value)))

    return rows


def insert_packet(conn, packet):
    '''Insert one packet's readings and commit immediately.'''
    rows = _rows_for_packet(packet)
    if not rows:
        return
    conn.executemany(
        "INSERT INTO readings (ts, node_id, sensor_type, key, value) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()


def insert_batch(conn, packets):
    '''
    Insert a whole batch of packets (e.g. everything BatchReceiver just
    flushed) in a single transaction — one commit for the group rather
    than one per packet, since they arrived together.
    '''
    rows = []
    for pkt in packets:
        rows.extend(_rows_for_packet(pkt))
    if not rows:
        return
    conn.executemany(
        "INSERT INTO readings (ts, node_id, sensor_type, key, value) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()


def query_readings(conn, minutes=None, start=None, end=None,
                    sensor_type=None, node_id=None):
    '''
    Query the readings table with optional filters, returning raw long-format
    rows as (ts, node_id, sensor_type, key, value) tuples, oldest first.

    minutes       — if given, only rows from the last N minutes (uses SQLite's
                     own clock via `datetime('now', '-N minutes')`, not Python's,
                     so results are consistent even if this call takes a moment).
    start, end    — ISO-8601 strings; alternative to `minutes` for an explicit
                     time range. Ignored if `minutes` is given.
    sensor_type   — e.g. "uv", "s2". None = all types.
    node_id       — None = all nodes.

    minutes and start/end are mutually exclusive on purpose — pass one or the
    other, not both, to avoid ambiguous overlapping ranges.
    '''
    clauses = []
    params  = []

    if minutes is not None:
        clauses.append("ts >= datetime('now', 'localtime', ?)")
        params.append(f"-{int(minutes)} minutes")
    else:
        if start is not None:
            clauses.append("ts >= ?")
            params.append(start)
        if end is not None:
            clauses.append("ts <= ?")
            params.append(end)

    if sensor_type is not None:
        clauses.append("sensor_type = ?")
        params.append(sensor_type)

    if node_id is not None:
        clauses.append("node_id = ?")
        params.append(node_id)

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    query = f"SELECT ts, node_id, sensor_type, key, value FROM readings {where_sql} ORDER BY ts ASC"

    cursor = conn.execute(query, params)
    return cursor.fetchall()


def pivot_to_packets(rows):
    '''
    Turn long-format rows (ts, node_id, sensor_type, key, value) back into
    packet-shaped dicts, e.g. {"t": "uv", "n": 1, "ts": "...", "lux": 967.2,
    "uvi": 0.13, "uv": 3.0} — same shape as lines in data_from_pico.txt, so
    existing dashboard rendering code doesn't need to change.

    Rows are grouped by (ts, node_id, sensor_type); order of packets in the
    output follows first-appearance order of each group in `rows`.
    '''
    packets = {}
    order = []

    for ts, node_id, sensor_type, key, value in rows:
        group_key = (ts, node_id, sensor_type)
        if group_key not in packets:
            packets[group_key] = {"t": sensor_type, "n": node_id, "ts": ts}
            order.append(group_key)
        packets[group_key][key] = value

    return [packets[k] for k in order]