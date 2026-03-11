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

class MockRFM69:
    def __init__(self):
        self.last_sent = None
    def send(self, data):
        self.last_sent = data

class MockRTC:
    class datetime:
        tm_year, tm_mon, tm_mday = 2026, 3, 6
        tm_hour, tm_min, tm_sec = 12, 0, 0