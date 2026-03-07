'''
CircuitPython 10.0.3 running on Pico 2W RP2350

Package sensor data and prepare it for radio and SD write.

Written by Fletcher Meyers 
March 2026

'''

import json
from device_setup import NODE_ID, rfm69, sequence, max17, ltr, soil_0, soil_1, soil_2


def send_packet(packet_dict):
    global sequence

    packet_dict["n"] = NODE_ID
    packet_dict["q"] = sequence

    sequence += 1
    packet_string = json.dumps(packet_dict, separators=(",", ":"))

    try: 
        print("Sending:", packet_string)
        rfm69.send(packet_string.encode("utf-8"))
    except AssertionError as e: 
        print("Packet too large for radio: ", len(packet_string), " bytes")
        #TODO: break packet up into smaller parts and try radio again


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

def package_battery_data():
    return {
        "t": "batt",
        "v": round(max17.cell_voltage, 2),
        "soc": round(max17.cell_percent, 1),
    }

def package_uv_data():
    return {
        "t": "uv",
        "uv": ltr.uvs,
        "uvi": round(ltr.uvi, 2),
        "lux": round(ltr.lux, 1),
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
if max17:
    SENSORS.append(("bt", package_battery_data))
if ltr:
    SENSORS.append(("uv", package_uv_data))

soil_sensors = [(0, soil_0), (1, soil_1), (2, soil_2)]
for sid, sobj in soil_sensors:
    if sobj:
        SENSORS.append((f"s{sid}", make_soil_fn(sid, sobj)))

