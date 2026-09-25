'''
CircuitPython 10.0.3 running on Pico 2W RP2350

Package sensor data and prepare it for radio and SD write.
Provides send_latest() for poll responses and send_sync_chunk() for Pi-driven SD sync.

Written by Fletcher Meyers
March 2026
'''

import json
import time

try:
    from hardware_setup_garden import (
        NODE_ID, rfm69, max17, ltr,
        soil_0, soil_1, soil_2,
        sht40, sgp40,
        ina238_0, ina238_1, ina238_2, ina238_3,
    )
except ImportError:
    NODE_ID = None
    rfm69 = None
    max17 = None
    ltr = None
    soil_0 = soil_1 = soil_2 = None
    sht40 = None
    sgp40 = None
    ina238_0 = ina238_1 = ina238_2 = ina238_3 = None


SD_DATA_FILE    = "/sd/data.txt"
SD_SENDING_FILE = "/sd/sending.txt"
SD_CURSOR_FILE  = "/sd/sync_cursor.txt"   # "<generation> <byte offset>" the Pi has confirmed

RADIO_MAX_BYTES     = 60   # RFM69 payload limit with encryption on
FRAGMENT_BODY_BYTES = 45   # leaves 15 bytes for the "~n.msg.i.k|" fragment header
FRAGMENT_GAP        = 0.1  # seconds between fragments, same spacing as normal packets
SYNC_LINE_GAP       = 0.1  # seconds between lines during a sync chunk


# ---------------------------------------------------------------------------
# Packet key reference:
# t        = type/sensor tag
# v        = voltage (or set_interval value)
# soc      = state of charge (%)
# m        = moisture
# tmp      = temperature (°C)
# rh       = relative humidity (%)
# voc      = SGP40 raw gas resistance (higher = cleaner air)
# uv       = raw UV count
# uvi      = UV index
# lux      = lux
# ma       = current (mA)
# mw       = power (mW)
# exp      = number of sensors expected in batch
# snt      = number of sensor packets actually sent
# g        = sync file generation (increments each time data.txt is rotated)
# o        = sync byte offset into sending.txt
# c        = lines sent in a sync chunk
# m        = 1 if more sync data is waiting
# k        = max lines requested per sync chunk
# n        = node ID
# q        = sequence number
# ts       = ISO timestamp
# ---------------------------------------------------------------------------


# latest_reading holds the most recent complete sensor snapshot in memory.
# It is a list of packet dicts, one per sensor, set by store_latest_reading().
# The Pi can request this at any time via a "poll" command without touching the SD.
latest_reading = []


class PacketSender:
    def __init__(self, node_id, radio):
        self.node_id = node_id
        self.radio = radio
        self.sequence = 0
        self.fragment_id = 0

    def send(self, packet_dict):
        ordered = {"t": packet_dict["t"], "q": self.sequence, "n": self.node_id}
        for k, v in packet_dict.items():
            if k not in ordered:
                ordered[k] = v
        self.sequence += 1

        packet_string = json.dumps(ordered, separators=(",", ":"))
        self.send_raw(packet_string.encode("utf-8"))

    def send_raw(self, data):
        '''
        Send already-encoded bytes, splitting into fragments if they don't
        fit in one radio packet. Each fragment is prefixed with a short
        plain-text header "~<node>.<msg>.<i>.<k>|" (fragment i of k of message
        msg) that the Pi's FragmentReassembler stitches back together. Plain
        text rather than a JSON wrapper, since embedding JSON inside a JSON
        string would escape every quote and nearly double the size.
        '''
        try:
            if len(data) <= RADIO_MAX_BYTES:
                self.radio.send(data)
                return

            parts = [data[i:i + FRAGMENT_BODY_BYTES]
                     for i in range(0, len(data), FRAGMENT_BODY_BYTES)]
            msg_id = self.fragment_id
            self.fragment_id = (self.fragment_id + 1) % 1000
            for i, part in enumerate(parts):
                header = f"~{self.node_id}.{msg_id}.{i}.{len(parts)}|".encode("utf-8")
                self.radio.send(header + part)
                if i < len(parts) - 1:
                    time.sleep(FRAGMENT_GAP)
        except Exception as e:
            print(f"[ERROR] Radio send failed ({len(data)} bytes): {e}")

    def send_batch_end(self, expected, sent):
        '''Send a batch_end packet closing out a poll response.'''
        self.send({"t": "batch_end", "exp": expected, "snt": sent})


def store_latest_reading(packets):
    '''
    Overwrite latest_reading with the freshly-read sensor packets.
    Called at the end of each sense cycle in code.py.
    '''
    global latest_reading
    latest_reading = list(packets)


def send_latest(sender, timestamp):
    '''
    Transmit the most recent in-memory sensor snapshot in response to a poll.
    Uses the same burst format as before (ts header → sensor packets → batch_end)
    so the Pi's existing BatchReceiver can handle it unchanged.
    '''
    if not latest_reading:
        print("[POLL] No reading available yet, skipping.")
        return

    sender.send({"t": "ts", "v": timestamp})
    time.sleep(0.1)

    sent = 0
    for pkt in latest_reading:
        try:
            sender.send(pkt)
            time.sleep(0.1)
            sent += 1
        except Exception as e:
            print(f"[POLL] Failed to send packet {pkt.get('t')}: {e}")

    sender.send_batch_end(expected=len(latest_reading), sent=sent)


def append_to_sd(packets, timestamp):
    '''
    Append a sensor snapshot to the SD data file.
    Each packet is written as a JSON line tagged with the batch timestamp.
    '''
    try:
        with open(SD_DATA_FILE, "a") as f:
            for pkt in packets:
                stamped = dict(pkt)      # copy so latest_reading dicts are not mutated
                stamped["ts"] = timestamp
                f.write(json.dumps(stamped, separators=(",", ":")) + "\n")
    except Exception as e:
        print(f"[SD] Write failed: {e}")


def _file_size(path):
    '''Size in bytes, or None if the file doesn't exist.'''
    try:
        import os
        return os.stat(path)[6]
    except OSError:
        return None


def _read_cursor():
    '''Return (generation, offset) the Pi last confirmed, or (0, 0) if none saved.'''
    try:
        with open(SD_CURSOR_FILE, "r") as f:
            gen, offset = f.read().split()
            return int(gen), int(offset)
    except (OSError, ValueError):
        return 0, 0


def _write_cursor(gen, offset):
    try:
        with open(SD_CURSOR_FILE, "w") as f:
            f.write(f"{gen} {offset}")
    except OSError as e:
        print(f"[SYNC] Could not save cursor: {e}")


def send_sync_chunk(sender, command, max_lines=20):
    '''
    Answer one Pi "sync" request with up to max_lines lines from sending.txt,
    followed by an "se" (sync end) packet. The Pi drives the whole transfer
    one chunk at a time, so nothing here blocks waiting on the Pi, and the Pi
    is free to poll other nodes in between chunks.

    Request: {"t":"sync","g":<gen>,"o":<offset>,"k":<max lines>}
      g/o are the generation and byte offset from the previous "se" — sending
      them confirms the Pi has stored everything before o, so the cursor
      advances. Omitting them (e.g. the Pi restarted) resumes from the saved
      cursor instead.
    Reply: lines..., then {"t":"se","g":<gen>,"o":<next offset>,"c":<lines>,"m":<0/1>}
      m = 1 if more data is waiting after this chunk.

    data.txt is renamed to sending.txt when a new transfer starts, so logging
    carries on into a fresh data.txt. The generation number goes up with each
    rename, so a late retry that still carries the previous file's offset is
    recognised and ignored rather than applied to the new file. Reads seek
    straight to the offset, so the chunk cost doesn't grow with file size.
    '''
    import os

    gen, offset = _read_cursor()
    if command.get("g") == gen and isinstance(command.get("o"), int):
        offset = command["o"]
        _write_cursor(gen, offset)
    max_lines = command.get("k", max_lines)

    size = _file_size(SD_SENDING_FILE)

    # Previous file fully confirmed by the Pi — delete it and move on.
    if size is not None and offset >= size:
        try:
            os.remove(SD_SENDING_FILE)
        except OSError as e:
            print(f"[SYNC] Could not delete sending.txt: {e}")
        print(f"[SYNC] Generation {gen} fully synced.")
        size = None

    # Start a new generation from whatever has been logged since.
    if size is None:
        data_size = _file_size(SD_DATA_FILE)
        if not data_size:
            sender.send({"t": "se", "g": gen, "o": 0, "c": 0, "m": 0})
            print("[SYNC] Nothing to send.")
            return
        try:
            os.rename(SD_DATA_FILE, SD_SENDING_FILE)
        except OSError as e:
            print(f"[SYNC] Could not rename data file: {e}")
            return
        gen, offset, size = gen + 1, 0, data_size
        _write_cursor(gen, offset)

    sent = 0
    try:
        with open(SD_SENDING_FILE, "rb") as f:
            f.seek(offset)
            while sent < max_lines:
                line = f.readline()
                if not line:
                    break
                offset += len(line)
                line = line.strip()
                if not line:
                    continue
                sender.send_raw(line)
                time.sleep(SYNC_LINE_GAP)
                sent += 1
    except OSError as e:
        print(f"[SYNC] Read failed at offset {offset}: {e}")
        return

    more = 1 if offset < size or _file_size(SD_DATA_FILE) else 0
    sender.send({"t": "se", "g": gen, "o": offset, "c": sent, "m": more})
    print(f"[SYNC] Sent {sent} lines (gen {gen}, offset {offset}/{size}).")


# ---------------------------------------------------------------------------
# Sensor read functions
# ---------------------------------------------------------------------------

def package_battery_data(sensor=None):
    s = sensor if sensor is not None else max17
    return {"t": "batt", "v": round(s.cell_voltage, 2), "soc": round(s.cell_percent, 1)}

def package_radio_temp(sensor=None):
    s = sensor if sensor is not None else rfm69
    return {"t": "rt", "tmp": s.temperature}

def package_uv_data(sensor=None):
    s = sensor if sensor is not None else ltr
    return {"t": "uv", "uv": s.uvs, "uvi": round(s.uvi, 2), "lux": round(s.lux, 1)}

def package_sht40_data(sensor=None):
    s = sensor if sensor is not None else sht40
    temperature, relative_humidity = s.measurements
    return {"t": "sht", "tmp": round(temperature, 2), "rh": round(relative_humidity, 1)}

def package_sgp40_data(sensor=None, temp=None, humidity=None):
    s = sensor if sensor is not None else sgp40
    if temp is not None and humidity is not None:
        raw = s.measure_raw(temperature=temp, relative_humidity=humidity)
    else:
        raw = s.raw
    return {"t": "voc", "voc": raw}

def package_ina238_data(sensor_id, sensor=None):
    s = sensor
    return {
        "t": f"pw{sensor_id}",
        "v": round(s.bus_voltage, 3),
        "ma": round(s.current * 1000, 1),
        "mw": round(s.power * 1000, 1),
    }

def make_soil_fn(sensor_id, sensor_obj):
    def read():
        return {
            "t": f"s{sensor_id}",
            "m": sensor_obj.moisture_read(),
            "tmp": round(sensor_obj.get_temp(), 2),
        }
    return read

def make_sgp40_compensated_fn(sht_sensor, sgp_sensor):
    def read():
        temperature, relative_humidity = sht_sensor.measurements
        return package_sgp40_data(sensor=sgp_sensor, temp=temperature, humidity=relative_humidity)
    return read


# ---------------------------------------------------------------------------
# SENSORS list — ordered list of (name, read_fn) for each available sensor
# ---------------------------------------------------------------------------

SENSORS = []

if rfm69:
    SENSORS.append(("rt", package_radio_temp))
if max17:
    SENSORS.append(("batt", package_battery_data))
if ltr:
    SENSORS.append(("uv", package_uv_data))
if sht40:
    SENSORS.append(("sht", package_sht40_data))
if sgp40:
    if sht40:
        SENSORS.append(("voc", make_sgp40_compensated_fn(sht40, sgp40)))
    else:
        SENSORS.append(("voc", lambda: package_sgp40_data()))

for sid, sobj in [(0, soil_0), (1, soil_1), (2, soil_2)]:
    if sobj:
        SENSORS.append((f"s{sid}", make_soil_fn(sid, sobj)))

for sid, sobj in [(0, ina238_0), (1, ina238_1), (2, ina238_2), (3, ina238_3)]:
    if sobj:
        SENSORS.append((f"pw{sid}", lambda s=sobj, i=sid: package_ina238_data(i, s)))

