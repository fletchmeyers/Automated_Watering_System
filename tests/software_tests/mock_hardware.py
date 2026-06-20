class MockMAX17048:
    cell_voltage = 3.85
    cell_percent = 72.0

class MockLTR390:
    uvs = 10
    uvi = 1.5
    lux = 200.0

class MockSeesaw:
    def moisture_read(self):
        return 512
    def get_temp(self):
        return 22.5

class MockSHT40:
    measurements = (23.5, 55.0)  # (temperature °C, relative humidity %)

class MockSGP40:
    raw = 32000  # typical clean-air value

    def measure_raw(self, temperature, relative_humidity):
        return 30000  # compensated read

class MockINA238:
    bus_voltage = 12.015
    current = 0.250   # amps — package_ina238_data multiplies by 1000 to get mA
    power = 3.004     # watts — multiplied by 1000 to get mW

class MockRFM69:
    temperature = 28  # °C — used by package_radio_temp

    def __init__(self):
        self.last_sent = None

    def send(self, data):
        self.last_sent = data

class MockRTC:
    class datetime:
        tm_year, tm_mon, tm_mday = 2026, 3, 6
        tm_hour, tm_min, tm_sec = 12, 0, 0