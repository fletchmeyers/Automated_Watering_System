'''
Designed for a Pico W RP2040 running CircuitPython 10.0.3

Read sendor data and save it to the SD card. 
Send sensor data to Pi via radio.

Written by Fletcher Meyers 
May 2025



import time
from device_setup import write_date_to_sd, write_sensor_data_to_sd


write_date_to_sd() #data header

while True:
    
    write_sensor_data_to_sd()
    time.sleep(3)
'''

import time
from device_setup import SENSORS, SEND_INTERVAL






while True:

    for sensor_name, sensor_function in SENSORS:

        try:
            sensor_function()

        except Exception as e:
            print(f"[ERROR] Sensor '{sensor_name}' failed:", e)

        time.sleep(0.5)

    time.sleep(SEND_INTERVAL)


