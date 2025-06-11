'''
Set up SPI for microSD, I2C bus, and other sensors (flow meter, battery monitors)

'''
import time
import machine
import sdcard
import uos
import ADC




#SPI setup
cs = machine.Pin(17, machine.Pin.OUT) # if we add more SPI devices, add their CS pins here
spi = machine.SPI(0,
                  baudrate=1000000,
                  polarity=0,
                  phase=0,
                  bits=8,
                  firstbit=machine.SPI.MSB,
                  sck=machine.Pin(18),
                  mosi=machine.Pin(19),
                  miso=machine.Pin(16))

# Initialize SD card and mount filesystem
sd = sdcard.SDCard(spi, cs)
vfs = uos.VfsFat(sd)
uos.mount(vfs, "/sd")

# Open csv file and begin writing to it:
with open("/sd/test01.txt", "w") as file:
    file.write(f"File started!  System has been online for {time.ticks_ms()} seconds.\r\n")

# Test read
with open("/sd/test01.txt", "r") as file:
    data = file.read()
    print(data)

#Onboard temp sensor on MCU
def read_internal_temp():
    sensor = ADC(4)
    voltage = sensor.read_u16() * 3.3 / 65535
    temp_c = 27 - (voltage - 0.706) / 0.001721
    return temp_c


#The following code should work for the flow sensor but I'm gonna keep it out until it's actually been tested    
'''
# Interrupt callback for flow sensor
def pulse_handler(pin):
    global pulse_count
    pulse_count += 1

# flow sensor
flow_pin = machine.Pin(15, machine.Pin.IN, machine.Pin.PULL_UP)  # GP15 with pull-up
flow_pin.irq(trigger=machine.Pin.IRQ_FALLING, handler=pulse_handler)
flow_sensor_calibration = 7.407  # Pulses per second per L/min
pulse_count = 0


try:
    while True:
        pulse_count = 0  # Reset counter
        time.sleep(1)    # Count for 1 second
        flow_rate = pulse_count / flow_sensor_calibration  # Liters per minute
        print("Flow rate: {:.2f} L/min".format(flow_rate))
except KeyboardInterrupt:
    print("Stopped")




'''