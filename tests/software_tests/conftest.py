# tests/conftest.py
import sys
from unittest.mock import MagicMock

sys.modules['board'] = MagicMock()
sys.modules['busio'] = MagicMock()
sys.modules['digitalio'] = MagicMock()
sys.modules['storage'] = MagicMock()
sys.modules['sdcardio'] = MagicMock()
sys.modules['adafruit_rfm69'] = MagicMock()
sys.modules['adafruit_max1704x'] = MagicMock()
sys.modules['adafruit_ltr390'] = MagicMock()
sys.modules['adafruit_pcf8523'] = MagicMock()
sys.modules['adafruit_pcf8523.pcf8523'] = MagicMock()
sys.modules['adafruit_seesaw'] = MagicMock()
sys.modules['adafruit_seesaw.seesaw'] = MagicMock()