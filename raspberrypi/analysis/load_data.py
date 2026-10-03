'''
Python 3 — run on the Pi (or copy sensors.db to your own machine and run there)

Loads sensor readings from sensors.db (long-format: ts, node_id, sensor_type,
key, value) into pandas for analysis. Not part of the always-running system —
this is an on-demand tool, imported by explore.py or your own scripts.

Usage:
    from load_data import load_query, by_type

    # Raw long-format rows, any combination of filters:
    df = load_query(sensor_type="s2", start="2026-08-01", end="2026-08-07")

    # Wide-format, one sensor type at a time (same shape the old flat-file
    # by_type() returned — one row per reading, one column per field):
    soil = by_type("s2")
'''

import sqlite3
from pathlib import Path
import pandas as pd

DB_FILE = Path(__file__).parent.parent / "sensors.db"


def _connect():
    return sqlite3.connect(DB_FILE)


def load_query(sensor_type=None, node_id=None, start=None, end=None):
    '''
    Query sensors.db for raw long-format rows, filtered by any combination
    of sensor_type, node_id, and a ts range (start/end are ISO-8601 strings;
    omit either to leave that side unbounded). Omit everything to load the
    whole table.

    Returns a DataFrame with columns: ts, node_id, sensor_type, key, value.
    ts is parsed to a proper datetime; unparseable values (e.g. "unknown",
    from a batch that lost its ts packet in radio transit) become NaT rather
    than crashing — same tolerance the old flat-file loader had.
    '''
    clauses = []
    params  = []

    if sensor_type is not None:
        clauses.append("sensor_type = ?")
        params.append(sensor_type)
    if node_id is not None:
        clauses.append("node_id = ?")
        params.append(node_id)
    if start is not None:
        clauses.append("ts >= ?")
        params.append(start)
    if end is not None:
        clauses.append("ts <= ?")
        params.append(end)

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    query = (f"SELECT ts, node_id, sensor_type, key, value FROM readings "
             f"{where_sql} ORDER BY ts ASC")

    with _connect() as conn:
        df = pd.read_sql_query(query, conn, params=params)

    if df.empty:
        return df

    df["ts"] = pd.to_datetime(df["ts"], errors="coerce")
    return df


def by_type(sensor_type, node_id=None, start=None, end=None):
    '''
    Pull one sensor type's readings and pivot long-format rows back to wide
    format — one row per (ts, node_id), one column per field (e.g. "m",
    "tmp" for s2) — matching the shape the old flat-file by_type() returned.
    Indexed by ts, sorted, rows with unparseable ts (NaT) dropped since they
    can't be plotted or resampled meaningfully.
    '''
    df = load_query(sensor_type=sensor_type, node_id=node_id, start=start, end=end)
    if df.empty:
        return df

    df = df.dropna(subset=["ts"])
    if df.empty:
        return df

    wide = df.pivot_table(index=["ts", "node_id"], columns="key",
                           values="value", aggfunc="first")
    wide = wide.reset_index(level="node_id").sort_index()
    return wide


if __name__ == "__main__":
    # Quick sanity check when run directly: python3 load_data.py
    df = load_query()
    print(f"Loaded {len(df)} total readings from {DB_FILE}")
    if not df.empty:
        print("Sensor types seen:", sorted(df["sensor_type"].dropna().unique()))
        print("Date range:", df["ts"].min(), "to", df["ts"].max())