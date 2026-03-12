# tests/software_tests/conftest.py
import sys
from unittest.mock import MagicMock
from pathlib import Path

# __file__             = Automated_Watering_System/tests/software_tests/conftest.py
# .parent              = Automated_Watering_System/tests/software_tests
# .parent.parent       = Automated_Watering_System/tests
# .parent.parent.parent = Automated_Watering_System
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root / "indoor"))
sys.path.insert(0, str(project_root / "outdoor"))

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