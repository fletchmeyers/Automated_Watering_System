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


    
Written by Fletcher Meyers 
May 2025
'''
import time
from communication_garden import read_internal_temp, temp_service
from device_setup import read_internal_temp


while True:
    temp = read_internal_temp()
    print("Temperature:", temp)
    temp_service.set_temperature(temp)
    time.sleep(5)
