import unittest
from unittest.mock import MagicMock
from outdoor.device_setup import try_init, get_timestamp

class TestTryInit(unittest.TestCase):

    def test_returns_object_on_success(self):
        mock_sensor = MagicMock()
        result = try_init("test", lambda: mock_sensor)
        self.assertEqual(result, mock_sensor)

    def test_returns_none_on_failure(self):
        def bad_init():
            raise RuntimeError("No I2C device")
        result = try_init("test", bad_init)
        self.assertIsNone(result)

    def test_does_not_raise_on_failure(self):
        try:
            try_init("test", lambda: 1 / 0)
        except Exception as e:
            self.fail(f"try_init raised unexpectedly: {e}")


class TestGetTimestamp(unittest.TestCase):

    def _make_rtc(self, year, mon, mday, hour, minute, sec):
        t = MagicMock()
        t.tm_year = year
        t.tm_mon = mon
        t.tm_mday = mday
        t.tm_hour = hour
        t.tm_min = minute
        t.tm_sec = sec
        rtc = MagicMock()
        rtc.datetime = t
        return rtc

    def test_format_is_iso8601(self):
        rtc = self._make_rtc(2026, 3, 6, 14, 5, 9)
        ts = get_timestamp(rtc)
        self.assertEqual(ts, "2026-03-06T14:05:09")

    def test_zero_pads_single_digit_fields(self):
        rtc = self._make_rtc(2026, 1, 1, 0, 0, 0)
        ts = get_timestamp(rtc)
        self.assertEqual(ts, "2026-01-01T00:00:00")

    def test_end_of_year(self):
        rtc = self._make_rtc(2026, 12, 31, 23, 59, 59)
        ts = get_timestamp(rtc)
        self.assertEqual(ts, "2026-12-31T23:59:59")