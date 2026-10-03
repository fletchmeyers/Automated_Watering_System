'''
Python 3 running on Raspberry Pi 3B
Live terminal dashboard for sensor data from data_from_pico.txt
Run with: python3 dashboard.py
'''

import curses
import json
import time
from collections import deque

DATA_FILE = "data_from_pico.txt"
REFRESH_RATE = 2  # seconds
HISTORY = 20      # number of readings to show in sparkline

def load_latest(filepath, n=100):
    """Read the last n lines from the data file."""
    try:
        with open(filepath, "r") as f:
            lines = f.readlines()
        packets = []
        for line in reversed(lines[-n:]):
            try:
                packets.append(json.loads(line.strip()))
            except json.JSONDecodeError:
                continue
        return list(reversed(packets))
    except FileNotFoundError:
        return []

def get_latest_by_type(packets, t):
    for p in reversed(packets):
        if p.get("t") == t:
            return p
    return None

def get_history(packets, t, key, n=HISTORY):
    vals = [p[key] for p in packets if p.get("t") == t and key in p]
    return vals[-n:]

def sparkline(values, width=20):
    """Render a simple ASCII sparkline."""
    if not values:
        return " " * width
    bars = " ▁▂▃▄▅▆▇█"
    lo, hi = min(values), max(values)
    span = hi - lo or 1
    chars = [bars[int((v - lo) / span * (len(bars) - 1))] for v in values]
    # Pad or trim to width
    while len(chars) < width:
        chars.insert(0, " ")
    return "".join(chars[-width:])

def draw(stdscr, packets):
    stdscr.clear()
    h, w = stdscr.getmaxyx()
    
    batt = get_latest_by_type(packets, "batt")
    uv   = get_latest_by_type(packets, "uv")
    soil = get_latest_by_type(packets, "s2")
    ts   = get_latest_by_type(packets, "ts") # won't appear since ts packets aren't written to file
    last_ts = batt.get("ts", "unknown") if batt else "unknown"

    # Colour pairs
    curses.init_pair(1, curses.COLOR_GREEN,  curses.COLOR_BLACK)  # good
    curses.init_pair(2, curses.COLOR_YELLOW, curses.COLOR_BLACK)  # warning
    curses.init_pair(3, curses.COLOR_CYAN,   curses.COLOR_BLACK)  # header
    curses.init_pair(4, curses.COLOR_WHITE,  curses.COLOR_BLACK)  # normal
    curses.init_pair(5, curses.COLOR_RED,    curses.COLOR_BLACK)  # alert

    def addstr(row, col, text, pair=4, bold=False):
        if row >= h - 1 or col >= w:
            return
        attr = curses.color_pair(pair)
        if bold:
            attr |= curses.A_BOLD
        try:
            stdscr.addstr(row, col, text[:w - col], attr)
        except curses.error:
            pass

    row = 0
    addstr(row, 0, "=" * min(w, 60), 3)
    row += 1
    addstr(row, 0, "  GARDEN SENSOR DASHBOARD", 3, bold=True)
    addstr(row, 30, f"Last update: {last_ts}", 4)
    row += 1
    addstr(row, 0, "=" * min(w, 60), 3)
    row += 2

    # --- BATTERY ---
    addstr(row, 0, "BATTERY", 3, bold=True)
    row += 1
    if batt:
        soc = batt.get("soc", 0)
        v   = batt.get("v", 0)
        soc_color = 1 if soc > 50 else (2 if soc > 20 else 5)
        bar_filled = int(soc / 5)  # 0-20 chars
        bar = "█" * bar_filled + "░" * (20 - bar_filled)
        addstr(row, 2, f"Voltage : {v:.2f} V", 4)
        row += 1
        addstr(row, 2, f"Charge  : {soc:.1f}%  [{bar}]", soc_color)
        row += 1
        hist = get_history(packets, "batt", "soc")
        addstr(row, 2, f"Trend   : {sparkline(hist)}", 4)
    else:
        addstr(row, 2, "No data", 2)
    row += 2

    # --- UV / LIGHT ---
    addstr(row, 0, "UV & LIGHT", 3, bold=True)
    row += 1
    if uv:
        lux = uv.get("lux", 0)
        uvi = uv.get("uvi", 0)
        uv_raw = uv.get("uv", 0)
        addstr(row, 2, f"Lux     : {lux:.1f}", 4)
        row += 1
        addstr(row, 2, f"UV Index: {uvi:.2f}   UV raw: {uv_raw}", 4)
        row += 1
        hist = get_history(packets, "uv", "lux")
        addstr(row, 2, f"Lux trend: {sparkline(hist)}", 4)
    else:
        addstr(row, 2, "No data", 2)
    row += 2

    # --- SOIL ---
    addstr(row, 0, "SOIL (sensor 2)", 3, bold=True)
    row += 1
    if soil:
        m   = soil.get("m", 0)
        tmp = soil.get("tmp", 0)
        # Moisture: Seesaw returns ~200 (dry) to ~1023 (wet)
        pct = max(0, min(100, int((m - 200) / 8.23)))
        moist_color = 1 if pct > 40 else (2 if pct > 20 else 5)
        bar_filled = int(pct / 5)
        bar = "█" * bar_filled + "░" * (20 - bar_filled)
        addstr(row, 2, f"Moisture: {m} raw  (~{pct}%)  [{bar}]", moist_color)
        row += 1
        addstr(row, 2, f"Temp    : {tmp:.2f} °C", 4)
        row += 1
        hist = get_history(packets, "s2", "m")
        addstr(row, 2, f"Trend   : {sparkline(hist)}", 4)
    else:
        addstr(row, 2, "No data", 2)
    row += 2

    addstr(row, 0, "─" * min(w, 60), 3)
    row += 1
    addstr(row, 0, "  Press Ctrl+C to exit", 2)

    stdscr.refresh()

def main(stdscr):
    curses.curs_set(0)
    stdscr.nodelay(True)

    while True:
        packets = load_latest(DATA_FILE)
        draw(stdscr, packets)
        time.sleep(REFRESH_RATE)

if __name__ == "__main__":
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass
