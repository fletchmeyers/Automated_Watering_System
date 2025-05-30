'''
Designed for a Pico W RP2040 running Micropython v1.25.0

Basic telemetry - 
    Collect data from onboard temperature sensor
    Print this data to the serial terminal
    Package data to be sent over Bluetooth Low Energy
    Send telemetry data to a Raspberry Pi

Planned expansion - 
    Connect other sensors over I2C and UART for telemetry data
    Add power system - solar pane and LiPo battery
    Control fluid solenoid to actively water plants
    Save telemetry data locally to a microSD card


    
Written by Fletcher Meyers - May 2025
'''

import time
import bluetooth
from machine import ADC
from micropython import const
from bluetooth import UUID

# BLE events
_IRQ_CENTRAL_CONNECT = const(1)
_IRQ_CENTRAL_DISCONNECT = const(2)

# UUIDs for custom service and characteristic
TEMP_SERVICE_UUID = UUID("6E400001-B5A3-F393-E0A9-E50E24DCCA9E")
TEMP_CHAR_UUID = UUID("6E400003-B5A3-F393-E0A9-E50E24DCCA9E")

class BLETemperature:
    def __init__(self, ble):
        self._ble = ble
        self._ble.active(True)
        self._ble.irq(self._irq)
        self._connections = set()

        # Define characteristic and service
        self._temp_char = (TEMP_CHAR_UUID, bluetooth.FLAG_READ | bluetooth.FLAG_NOTIFY)
        temp_service = (TEMP_SERVICE_UUID, (self._temp_char,))
        ((self._handle,),) = self._ble.gatts_register_services((temp_service,))

        self._advertise()

    def _irq(self, event, data):
        if event == _IRQ_CENTRAL_CONNECT:
            conn_handle, _, _ = data
            self._connections.add(conn_handle)
            print("Central connected")
        elif event == _IRQ_CENTRAL_DISCONNECT:
            conn_handle, _, _ = data
            self._connections.remove(conn_handle)
            print("Central disconnected")
            self._advertise()

    def _advertise(self):
        name = "PicoTemp"
        payload = bytearray('\x02\x01\x06', 'utf-8') + bytearray((len(name) + 1, 0x09)) + name.encode()
        self._ble.gap_advertise(100_000, adv_data=payload)
        print("Advertising as", name)

    def set_temperature(self, temp_c):
        # Convert float to string and encode
        temp_str = "{:.2f}".format(temp_c)
        temp_bytes = temp_str.encode('utf-8')
        self._ble.gatts_write(self._handle, temp_bytes)
        for conn_handle in self._connections:
            self._ble.gatts_notify(conn_handle, self._handle, temp_bytes)

def read_internal_temp():
    sensor = ADC(4)
    voltage = sensor.read_u16() * 3.3 / 65535
    temp_c = 27 - (voltage - 0.706) / 0.001721
    return temp_c

# Main
ble = bluetooth.BLE()
temp_service = BLETemperature(ble)

while True:
    temp = read_internal_temp()
    print("Temperature:", temp)
    temp_service.set_temperature(temp)
    time.sleep(5)
