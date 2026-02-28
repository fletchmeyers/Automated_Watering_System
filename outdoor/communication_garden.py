
#!Replace with circuit python!

import bluetooth
import time
from micropython import const
#from bluetooth import UUID


# BLE events
_IRQ_CENTRAL_CONNECT = const(1)
_IRQ_CENTRAL_DISCONNECT = const(2)

# UUIDs for custom service and characteristic
TEMP_SERVICE_UUID = bluetooth.UUID("6E400001-B5A3-F393-E0A9-E50E24DCCA9E")
TEMP_CHAR_UUID = bluetooth.UUID("6E400003-B5A3-F393-E0A9-E50E24DCCA9E")

class Telemetry:
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
        name = "Outdoor MCU"
        payload = bytearray('\x02\x01\x06', 'utf-8') + bytearray((len(name) + 1, 0x09)) + name.encode()
        self._ble.gap_advertise(100_000, adv_data=payload)
        print("Advertising as", name)


    def log_data(self, sensor, value, filename):
        #save data to SD card
        try:
            with open (filename, "a") as file:
                timestamp = time.ticks_ms()
                file.write("Time: {} ms, {}: {}\n".format(timestamp, sensor, value))
        except Exception as e:
            print("SD write failed:", e)

        #send data over BLE
        if sensor == "temperature":
            self.set_temperature(value)
        

    def set_temperature(self, temp_c):
        # Convert float to string and encode
        temp_str = "{:.2f}".format(temp_c)
        temp_bytes = temp_str.encode('utf-8')
        self._ble.gatts_write(self._handle, temp_bytes)
        for conn_handle in self._connections:
            self._ble.gatts_notify(conn_handle, self._handle, temp_bytes)



# Main
ble = bluetooth.BLE()
temp_service = Telemetry(ble)
