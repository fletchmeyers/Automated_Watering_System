'''
CircuitPython 10.0.3 running on Pico 2W RP2350

Package sensor data and prepare it for radio and SD write.
Provides send_latest() for poll responses and send_bulk_sync() for full SD transfers.

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
CHUNK_ACK_WAIT  = 5   # seconds to wait for a per-chunk ack before giving up


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
# expected = number of sensors expected in batch
# sent     = number of sensor packets actually sent
# chunk    = chunk index (1-based) used during bulk sync
# total    = total chunks in a bulk sync transfer
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

    def send(self, packet_dict):
        ordered = {"t": packet_dict["t"], "q": self.sequence, "n": self.node_id}
        for k, v in packet_dict.items():
            if k not in ordered:
                ordered[k] = v
        self.sequence += 1

        packet_string = json.dumps(ordered, separators=(",", ":"))
        try:
            self.radio.send(packet_string.encode("utf-8"))
        except AssertionError:
            print("Packet too large:", len(packet_string), "bytes")

    def send_batch_end(self, expected, sent, chunk=None, total=None):
        '''
        Send a batch_end packet.
        chunk and total are included during bulk sync so the Pi can track progress
        and send a matching per-chunk ack.
        '''
        pkt = {"t": "batch_end", "expected": expected, "sent": sent}
        if chunk is not None:
            pkt["chunk"] = chunk
        if total is not None:
            pkt["total"] = total
        self.send(pkt)


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
                pkt["ts"] = timestamp
                f.write(json.dumps(pkt, separators=(",", ":")) + "\n")
    except Exception as e:
        print(f"[SD] Write failed: {e}")


def _count_lines(filepath):
    '''Count lines in a file without loading it all into memory.'''
    count = 0
    try:
        with open(filepath, "r") as f:
            for _ in f:
                count += 1
    except OSError:
        pass
    return count


def _read_chunk(filepath, start_line, chunk_size):
    '''
    Read up to chunk_size lines from filepath starting at start_line (0-indexed).
    Returns a list of raw line strings (stripped).
    CircuitPython has no seek(), so we iterate from the top each time.
    This is O(n) but acceptable for the expected file sizes.

    # TODO: if bulk sync of very large files becomes slow, consider writing
    # a simple binary index file on the SD to allow faster seeking.
    '''
    lines = []
    try:
        with open(filepath, "r") as f:
            for i, line in enumerate(f):
                if i < start_line:
                    continue
                if i >= start_line + chunk_size:
                    break
                line = line.strip()
                if line:
                    lines.append(line)
    except OSError as e:
        print(f"[SD] Read failed at line {start_line}: {e}")
    return lines


def _wait_for_chunk_ack(radio, expected_chunk):
    '''
    Wait up to CHUNK_ACK_WAIT seconds for a data_ack matching expected_chunk.
    Returns True if ack received, False on timeout.
    '''
    deadline = time.monotonic() + CHUNK_ACK_WAIT
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        packet = radio.receive(timeout=min(0.5, remaining), with_header=True)
        if packet is None:
            continue
        try:
            data = json.loads(packet[4:].decode("utf-8"))
            if data.get("t") == "data_ack" and data.get("chunk") == expected_chunk:
                print(f"[SYNC] Chunk {expected_chunk} acked.")
                return True
        except Exception:
            pass
    print(f"[SYNC] No ack for chunk {expected_chunk} — aborting bulk sync.")
    return False


def send_bulk_sync(sender, radio):
    '''
    Rename data.txt → sending.txt so new sensor data can accumulate uninterrupted,
    then send sending.txt to the Pi in chunk-sized bursts, one batch_end + data_ack
    handshake per chunk. Delete sending.txt only after all chunks are acked.

    Each chunk is one sense-cycle's worth of lines grouped by their ts field,
    or LINES_PER_CHUNK lines if grouping isn't feasible — currently we use a flat
    line count per chunk for simplicity.

    # TODO: group chunks by ts so each chunk is a clean sense-cycle boundary.
    # For now, flat line count is fine and keeps the logic simple.
    '''
    LINES_PER_CHUNK = 20  # tune based on packet size vs radio throughput

    # Rename so new data writes to a fresh data.txt immediately
    try:
        import os
        os.rename(SD_DATA_FILE, SD_SENDING_FILE)
    except OSError as e:
        print(f"[SYNC] Could not rename data file: {e}")
        return

    total_lines = _count_lines(SD_SENDING_FILE)
    if total_lines == 0:
        print("[SYNC] No data to send.")
        try:
            import os
            os.remove(SD_SENDING_FILE)
        except OSError:
            pass
        sender.send({"t": "sync_complete", "chunks": 0})
        return

    # Calculate total number of chunks (ceiling division)
    total_chunks = (total_lines + LINES_PER_CHUNK - 1) // LINES_PER_CHUNK
    print(f"[SYNC] Starting bulk sync: {total_lines} lines, {total_chunks} chunks.")

    for chunk_idx in range(total_chunks):
        chunk_num = chunk_idx + 1
        start_line = chunk_idx * LINES_PER_CHUNK
        lines = _read_chunk(SD_SENDING_FILE, start_line, LINES_PER_CHUNK)

        if not lines:
            print(f"[SYNC] Chunk {chunk_num}: no lines read, skipping.")
            continue

        # Send each line as a raw packet — lines are already JSON from append_to_sd
        sent = 0
        for line in lines:
            try:
                radio.send(line.encode("utf-8"))
                time.sleep(0.1)
                sent += 1
            except Exception as e:
                print(f"[SYNC] Failed to send line: {e}")

        sender.send_batch_end(
            expected=len(lines),
            sent=sent,
            chunk=chunk_num,
            total=total_chunks,
        )

        if not _wait_for_chunk_ack(radio, chunk_num):
            print(f"[SYNC] Aborting at chunk {chunk_num}/{total_chunks}.")
            # Leave sending.txt intact — Pi can request again later
            return

    # All chunks acked — clean up
    try:
        import os
        os.remove(SD_SENDING_FILE)
        print("[SYNC] Bulk sync complete. sending.txt deleted.")
    except OSError as e:
        print(f"[SYNC] Could not delete sending.txt: {e}")

    sender.send({"t": "sync_complete", "chunks": total_chunks})


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

