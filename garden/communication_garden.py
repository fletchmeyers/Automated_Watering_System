'''
CircuitPython 10.0.3 running on Pico 2W RP2350

Package sensor data and prepare it for radio and SD write.

Written by Fletcher Meyers 
March 2026

'''

import json
try: 
    from hardware_setup_garden import NODE_ID, rfm69, max17, ltr, soil_0, soil_1, soil_2
except (ImportError, ModuleNotFoundError):
    NODE_ID = None
    rfm69 = None
    max17 = None
    ltr = None
    soil_0 = soil_1 = soil_2 = None


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
# temp= temperature (°C)
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


def make_soil_fn(sensor_id, sensor_obj):
    def read():
        return {
            "t": f"s{sensor_id}",
            "m": sensor_obj.moisture_read(),
            "tmp": round(sensor_obj.get_temp(), 2),
        }
    return read


SENSORS = []

if rfm69:
    SENSORS.append(("rt", package_radio_temp))
if max17:
    SENSORS.append(("bt", package_battery_data))
if ltr:
    SENSORS.append(("uv", package_uv_data))

soil_sensors = [(0, soil_0), (1, soil_1), (2, soil_2)]
for sid, sobj in soil_sensors:
    if sobj:
        SENSORS.append((f"s{sid}", make_soil_fn(sid, sobj)))

