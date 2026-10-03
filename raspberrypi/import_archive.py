'''
Python 3 running on Raspberry Pi 3B

Import old archive logs (one JSON packet per line, as in archive/,
data_from_pico.txt or a backup copy of them) into sensors.db, adding only
packets the database doesn't already have. Safe to run on overlapping or
repeated files, and safe to run again.

A packet counts as already present when sensors.db has any row with the
same timestamp, node and sensor type: rows are written from the same
packets, so their ts strings match exactly.

Usage:
    python3 import_archive.py <file or folder> [...] [--since DATE] [--until DATE] [--dry-run]

Folders are searched recursively for *.txt. Packets with no real timestamp
("unknown") or no node ID are skipped. main.py can keep running meanwhile.

Written by Fletcher Meyers
October 2026
'''

import argparse
import json
import re
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import db

BATCH_PACKETS = 1000   # packets per DB transaction — short enough not to stall main.py's writes
_TS = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$")


def find_files(paths):
    '''Expand the given files and folders into a sorted list of .txt files.'''
    files = []
    for p in paths:
        if p.is_dir():
            files.extend(sorted(p.rglob("*.txt")))
        elif p.is_file():
            files.append(p)
        else:
            print(f"[IMPORT] Not found, skipping: {p}")
    return files


def iter_packets(path):
    '''Yield each line of path as a dict, or None if it isn't a JSON packet.'''
    with open(path, "rb") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                packet = json.loads(raw)
            except ValueError:
                yield None   # e.g. a line cut short, or zeros left by a power cut
                continue
            yield packet if isinstance(packet, dict) and "t" in packet else None


class Existing:
    '''
    The (ts, node, sensor type) keys already in sensors.db, loaded one day
    at a time as packets from that day turn up, and kept up to date as this
    import adds more.
    '''

    def __init__(self, conn):
        self.conn = conn
        self.days = {}

    def _day(self, day):
        keys = self.days.get(day)
        if keys is None:
            keys = set()
            if self.conn is not None:
                end = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
                rows = self.conn.execute(
                    "SELECT DISTINCT ts, node_id, sensor_type FROM readings "
                    "WHERE ts >= ? AND ts < ?", (day, end))
                keys.update((ts, int(n), t) for ts, n, t in rows)
            self.days[day] = keys
        return keys

    def add_if_new(self, key):
        '''Record key; return True if it wasn't there before.'''
        keys = self._day(key[0][:10])
        if key in keys:
            return False
        keys.add(key)
        return True


def import_files(files, conn, since=None, until=None, write=True):
    '''
    Import files into conn. With write=False, only count what would be
    added (still checking conn for what's already there). Returns a summary.
    '''
    existing = Existing(conn)
    summary = Counter()
    per_month = Counter()
    first = last = None
    batch = []

    def flush():
        if write and batch:
            db.insert_batch(conn, batch)
        batch.clear()

    for path in files:
        added_here = 0
        for packet in iter_packets(path):
            if packet is None:
                summary["unreadable"] += 1
                continue
            ts, node = packet.get("ts"), packet.get("n")
            if not isinstance(ts, str) or not _TS.match(ts):
                summary["no_timestamp"] += 1
                continue
            if not isinstance(node, int):
                summary["no_node"] += 1
                continue
            if (since and ts < since) or (until and ts >= until):
                summary["outside_range"] += 1
                continue
            if not existing.add_if_new((ts, node, packet["t"])):
                summary["already_present"] += 1
                continue

            batch.append(packet)
            summary["added"] += 1
            added_here += 1
            per_month[ts[:7]] += 1
            first = ts if first is None or ts < first else first
            last = ts if last is None or ts > last else last
            if len(batch) >= BATCH_PACKETS:
                flush()
        flush()
        if added_here:
            print(f"[IMPORT] {path.name}: {added_here:,} new packets")

    summary["first_ts"], summary["last_ts"] = first, last
    summary["per_month"] = dict(sorted(per_month.items()))
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", type=Path, nargs="+", help="archive files or folders")
    parser.add_argument("--since", help="only packets on or after this date (YYYY-MM-DD)")
    parser.add_argument("--until", help="only packets before this date (YYYY-MM-DD)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be added without writing to sensors.db")
    args = parser.parse_args()

    files = find_files(args.paths)
    print(f"[IMPORT] {len(files)} files")

    conn = db.get_connection()
    # main.py may be writing at the same time — wait for its lock rather than failing.
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        result = import_files(files, conn, args.since, args.until, write=not args.dry_run)
    finally:
        conn.close()

    skipped = ", ".join(f"{result[k]:,} {k.replace('_', ' ')}"
                        for k in ("already_present", "no_timestamp", "no_node",
                                  "outside_range", "unreadable") if result[k])
    verb = "Would add" if args.dry_run else "Added"
    print(f"[IMPORT] {verb} {result['added']:,} packets "
          f"({result['first_ts']} to {result['last_ts']}). Skipped: {skipped or 'none'}.")
    for month, n in result["per_month"].items():
        print(f"[IMPORT]   {month}: {n:,}")
