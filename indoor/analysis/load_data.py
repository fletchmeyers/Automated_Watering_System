'''
Python 3 — run on the Pi (or copy the archive/ folder to your own machine)

Loads archived sensor data (written by communication_indoor.py's BatchReceiver)
into a pandas DataFrame for analysis. Not part of the always-running system —
this is an on-demand tool, imported by explore.py or your own scripts.

Usage:
    from load_data import load_range, by_type

    df = load_range("2026-07-01", "2026-07-06")
    soil = by_type(df, "s2")
'''

import json
from pathlib import Path
import pandas as pd

ARCHIVE_DIR = Path(__file__).parent.parent / "archive"


def load_range(start_date=None, end_date=None):
    '''
    Load archived packets into a DataFrame. start_date/end_date are
    'YYYY-MM-DD' strings; omit either to leave that side unbounded.
    Malformed lines are skipped silently (same tolerance as sensor_health_report).
    '''
    files = sorted(ARCHIVE_DIR.glob("data_*.txt"))
    if start_date:
        files = [f for f in files if f.stem.split("_", 1)[1] >= start_date]
    if end_date:
        files = [f for f in files if f.stem.split("_", 1)[1] <= end_date]

    if not files:
        print(f"[load_data] No archive files found in {ARCHIVE_DIR} "
              f"for range {start_date!r} to {end_date!r}.")
        return pd.DataFrame()

    records = []
    for f in files:
        for line in f.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    df = pd.DataFrame(records)
    if df.empty:
        return df

    # "unknown" ts (packet arrived before its ts packet) becomes NaT, not a crash
    df["ts"] = pd.to_datetime(df["ts"], errors="coerce")
    return df


def by_type(df, packet_type):
    '''
    Convenience: pull one sensor type's rows, indexed by timestamp, sorted.
    Rows with no valid timestamp (NaT) are dropped since they can't be plotted
    or resampled meaningfully — inspect df[df["t"] == packet_type] directly
    if you need those too.
    '''
    if df.empty:
        return df
    sub = df[df["t"] == packet_type].dropna(subset=["ts"]).set_index("ts")
    return sub.sort_index()


if __name__ == "__main__":
    # Quick sanity check when run directly: python3 load_data.py
    df = load_range()
    print(f"Loaded {len(df)} total packets from {ARCHIVE_DIR}")
    if not df.empty:
        print("Packet types seen:", sorted(df["t"].dropna().unique()))
        print("Date range:", df["ts"].min(), "to", df["ts"].max())