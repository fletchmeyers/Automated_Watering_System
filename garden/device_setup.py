'''
Set up I2C bus and other sensors (flow meter, battery monitors)

'''
from machine import ADC, Pin
import time

# Variables for counting
pulse_count = 0

# Interrupt callback
def pulse_handler(pin):
    global pulse_count
    pulse_count += 1

# Setup pin
flow_pin = Pin(15, Pin.IN, Pin.PULL_UP)  # GP15 with pull-up
flow_pin.irq(trigger=Pin.IRQ_FALLING, handler=pulse_handler)

# Calibration constant
calibration = 7.407  # Pulses per second per L/min


try:
    while True:
        pulse_count = 0  # Reset counter
        time.sleep(1)    # Count for 1 second
        flow_rate = pulse_count / calibration  # Liters per minute
        print("Flow rate: {:.2f} L/min".format(flow_rate))
except KeyboardInterrupt:
    print("Stopped")



def read_internal_temp():
    sensor = ADC(4)
    voltage = sensor.read_u16() * 3.3 / 65535
    temp_c = 27 - (voltage - 0.706) / 0.001721
    return temp_c


