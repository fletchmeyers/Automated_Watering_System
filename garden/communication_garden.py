'''
CircuitPython 10.0.3 running on Pico 2W RP2350

Package sensor data and prepare it for radio and SD write.

Written by Fletcher Meyers 
March 2026

'''

import json
try:
    from hardware_setup_garden import (
        NODE_ID, rfm69, max17, ltr,
        soil_0, soil_1, soil_2,
        sht40, sgp40,
    )
except ImportError:
    NODE_ID = None
    rfm69 = None
    max17 = None
    ltr = None
    soil_0 = soil_1 = soil_2 = None
    sht40 = None
    sgp40 = None


class PacketSender:
    def __init__(self, node_id, radio):
        self.node_id = node_id
        self.radio = radio
        self.sequence = 0

    def send(self, packet_dict):
        packet_dict["n"] = self.node_id
        packet_dict["q"] = self.sequence
        self.sequence += 1

        packet_string = json.dumps(packet_dict, separators=(",", ":"))
        try:
            self.radio.send(packet_string.encode("utf-8"))
        except AssertionError:
            print("Packet too large:", len(packet_string), "bytes")


# Packet key reference:
# t   = type/sensor tag
# v   = voltage
# soc = state of charge (%)
# m   = moisture
# tmp = temperature (°C)
# rh  = relative humidity (%)
# voc = SGP40 raw gas resistance (higher = cleaner air)
# uv  = raw UV count
# uvi = UV index
# lux = lux
# n   = node ID
# q   = sequence number
# ts  = ISO timestamp


def write_batch_to_sd(lines):
    with open("/sd/data.txt", "a") as f:
        for line in lines:
            f.write(line + "\n")

def package_battery_data(sensor=None):
    s = sensor if sensor is not None else max17
    return {
        "t": "batt",
        "v": round(s.cell_voltage, 2),
        "soc": round(s.cell_percent, 1),
    }

def package_radio_temp(sensor=None):
    s = sensor if sensor is not None else rfm69
    return {
        "t": "rt",
        "tmp": s.temperature,
    }

def package_uv_data(sensor=None):
    s = sensor if sensor is not None else ltr
    return {
        "t": "uv",
        "uv": s.uvs,
        "uvi": round(s.uvi, 2),
        "lux": round(s.lux, 1),
    }

def package_sht40_data(sensor=None):
    s = sensor if sensor is not None else sht40
    temperature, relative_humidity = s.measurements
    return {
        "t": "sht",
        "tmp": round(temperature, 2),
        "rh": round(relative_humidity, 1),
    }

def package_sgp40_data(sensor=None, temp=None, humidity=None):
    '''
    Read raw VOC gas resistance from the SGP40.
    If temp (°C) and humidity (%) are provided, applies compensation from SHT40.
    Higher raw values indicate cleaner air.
    '''
    s = sensor if sensor is not None else sgp40
    if temp is not None and humidity is not None:
        raw = s.measure_raw(temperature=temp, relative_humidity=humidity)
    else:
        raw = s.raw
    return {
        "t": "voc",
        "voc": raw,
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
    '''Read SHT40 first, pass temp+humidity to SGP40 for a compensated VOC read.'''
    def read():
        temperature, relative_humidity = sht_sensor.measurements
        return package_sgp40_data(
            sensor=sgp_sensor,
            temp=temperature,
            humidity=relative_humidity,
        )
    return read


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
        # Compensated read — preferred when both sensors are present
        SENSORS.append(("voc", make_sgp40_compensated_fn(sht40, sgp40)))
    else:
        # Uncompensated fallback if SHT40 failed to init
        SENSORS.append(("voc", lambda: package_sgp40_data()))

soil_sensors = [(0, soil_0), (1, soil_1), (2, soil_2)]
for sid, sobj in soil_sensors:
    if sobj:
        SENSORS.append((f"s{sid}", make_soil_fn(sid, sobj)))