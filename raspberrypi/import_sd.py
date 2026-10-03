'''
Python 3 running on Raspberry Pi 3B

Import a node's SD card log straight into sensors.db — for backlogs too big
to sensibly pull over the radio. Reads sending.txt (skipping whatever the
radio sync has already delivered, per the card's sync_cursor.txt) and then
data.txt, the same order the radio sync would have sent them in.

Usage:
    python3 import_sd.py <folder with the card's files> [--node N] [--dry-run]

Point it at a copy of the card's files rather than the card itself, so the
card can go straight back into the node. Nothing is deleted.

Written by Fletcher Meyers
September 2026
'''

import argparse
import json
from pathlib import Path

import db

BATCH_LINES = 2000   # lines per DB transaction — short enough not to stall main.py's writes


def read_cursor(folder):
    '''Return the byte offset the Pi has already confirmed in sending.txt, or 0.'''
    try:
        _gen, offset = (folder / "sync_cursor.txt").read_text().split()
        return int(offset)
    except (OSError, ValueError):
        return 0


def iter_lines(path, start=0):
    '''Yield parsed log lines from path, starting at byte offset start. Bad lines yield None.'''
    with open(path, "rb") as f:
        f.seek(start)
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                line = json.loads(raw)
            except ValueError:
                yield None   # e.g. a half-written last line from when the card filled up
                continue
            yield line if isinstance(line, dict) and "t" in line and "ts" in line else None


def import_folder(folder, node_id, conn=None):
    '''Import sending.txt then data.txt from folder. Returns a summary dict.'''
    sources = []
    if (folder / "sending.txt").exists():
        sources.append((folder / "sending.txt", read_cursor(folder)))
    if (folder / "data.txt").exists():
        sources.append((folder / "data.txt", 0))

    summary = {"imported": 0, "skipped": 0, "first_ts": None, "last_ts": None}
    batch = []

    def flush():
        if conn is not None and batch:
            db.insert_batch(conn, batch)
        batch.clear()

    for path, start in sources:
        print(f"[IMPORT] {path.name}: starting at byte {start:,} of {path.stat().st_size:,}")
        for line in iter_lines(path, start):
            if line is None:
                summary["skipped"] += 1
                continue
            line["n"] = node_id
            batch.append(line)
            summary["imported"] += 1
            if summary["first_ts"] is None or line["ts"] < summary["first_ts"]:
                summary["first_ts"] = line["ts"]
            if summary["last_ts"] is None or line["ts"] > summary["last_ts"]:
                summary["last_ts"] = line["ts"]
            if len(batch) >= BATCH_LINES:
                flush()
                if summary["imported"] % 100_000 == 0:
                    print(f"[IMPORT] {summary['imported']:,} lines so far "
                          f"(at {line['ts']})")
        flush()

    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("folder", type=Path, help="folder holding the card's sending.txt / data.txt")
    parser.add_argument("--node", type=int, default=1, help="node ID the card came from (default 1)")
    parser.add_argument("--dry-run", action="store_true", help="count lines without writing to sensors.db")
    args = parser.parse_args()

    conn = None
    if not args.dry_run:
        conn = db.get_connection()
        # main.py may be writing at the same time — wait for its lock rather than failing.
        conn.execute("PRAGMA busy_timeout = 30000")

    try:
        result = import_folder(args.folder, args.node, conn)
    finally:
        if conn is not None:
            conn.close()

    verb = "Would import" if args.dry_run else "Imported"
    print(f"[IMPORT] {verb} {result['imported']:,} lines "
          f"({result['first_ts']} to {result['last_ts']}), "
          f"skipped {result['skipped']:,} unreadable.")
