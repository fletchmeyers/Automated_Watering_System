import unittest
import json
from unittest.mock import MagicMock, patch
from tests.software_tests.mock_hardware import (
    MockMAX17048, MockLTR390, MockSeesaw, MockRFM69, MockRTC,
    MockSHT40, MockSGP40, MockINA238,
)
from garden.communication_garden import (
    package_battery_data,
    package_uv_data,
    package_radio_temp,
    package_sht40_data,
    package_sgp40_data,
    package_ina238_data,
    make_soil_fn,
    make_sgp40_compensated_fn,
    PacketSender,
)


# ── PacketSender ──────────────────────────────────────────────────────────────

class TestPacketSender(unittest.TestCase):

    def test_sequence_starts_at_zero(self):
        sender = PacketSender(node_id=1, radio=MockRFM69())
        sender.send({"t": "batt"})
        sent = json.loads(sender.radio.last_sent.decode("utf-8"))
        self.assertEqual(sent["q"], 0)

    def test_sequence_increments(self):
        sender = PacketSender(node_id=1, radio=MockRFM69())
        sender.send({"t": "batt"})
        sender.send({"t": "uv"})
        sent = json.loads(sender.radio.last_sent.decode("utf-8"))
        self.assertEqual(sent["q"], 1)

    def test_node_id_in_packet(self):
        sender = PacketSender(node_id=42, radio=MockRFM69())
        sender.send({"t": "batt"})
        sent = json.loads(sender.radio.last_sent.decode("utf-8"))
        self.assertEqual(sent["n"], 42)

    def test_does_not_mutate_original_dict(self):
        # send() builds a new ordered dict — the caller's dict must be untouched
        sender = PacketSender(node_id=1, radio=MockRFM69())
        data = {"t": "batt"}
        sender.send(data)
        self.assertNotIn("n", data)
        self.assertNotIn("q", data)

    def test_key_order_t_q_n_first(self):
        # t, q, n must be the first three keys in the transmitted JSON
        sender = PacketSender(node_id=1, radio=MockRFM69())
        sender.send({"t": "batt", "v": 3.85, "soc": 72.0})
        sent = json.loads(sender.radio.last_sent.decode("utf-8"))
        keys = list(sent.keys())
        self.assertEqual(keys[:3], ["t", "q", "n"])

    def test_key_order_sensor_data_after_header(self):
        # Sensor-specific keys must follow t/q/n
        sender = PacketSender(node_id=1, radio=MockRFM69())
        sender.send({"t": "batt", "v": 3.85, "soc": 72.0})
        sent = json.loads(sender.radio.last_sent.decode("utf-8"))
        keys = list(sent.keys())
        self.assertIn("v", keys[3:])
        self.assertIn("soc", keys[3:])

    def test_oversized_packet_does_not_raise(self):
        # Radio rejects oversized packets via AssertionError — send() must catch it
        sender = PacketSender(node_id=1, radio=MockRFM69())
        big_data = {"t": "x", "junk": "a" * 100}
        try:
            sender.send(big_data)
        except Exception as e:
            self.fail(f"send() raised unexpectedly: {e}")

    def test_send_batch_end_packet_structure(self):
        sender = PacketSender(node_id=1, radio=MockRFM69())
        sender.send_batch_end(expected=5, sent=4)
        sent = json.loads(sender.radio.last_sent.decode("utf-8"))
        self.assertEqual(sent["t"], "batch_end")
        self.assertEqual(sent["expected"], 5)
        self.assertEqual(sent["sent"], 4)
        self.assertIn("q", sent)
        self.assertIn("n", sent)

    def test_send_batch_end_key_order(self):
        sender = PacketSender(node_id=1, radio=MockRFM69())
        sender.send_batch_end(expected=5, sent=4)
        sent = json.loads(sender.radio.last_sent.decode("utf-8"))
        keys = list(sent.keys())
        self.assertEqual(keys[:3], ["t", "q", "n"])

    def test_packet_fits_radio_limit(self):
        sender = PacketSender(node_id=1, radio=MockRFM69())
        data = package_battery_data(MockMAX17048())
        sender.send(data)
        self.assertLessEqual(len(sender.radio.last_sent), 60)

    def test_timestamp_packet_fits_radio_limit(self):
        sender = PacketSender(node_id=1, radio=MockRFM69())
        sender.send({"t": "ts", "v": "2026-03-06T12:00:00"})
        self.assertLessEqual(len(sender.radio.last_sent), 60)

    def test_all_sensor_packets_fit_radio_limit(self):
        sender = PacketSender(node_id=1, radio=MockRFM69())
        packets = [
            package_battery_data(MockMAX17048()),
            package_uv_data(MockLTR390()),
            package_radio_temp(MockRFM69()),
            package_sht40_data(MockSHT40()),
            package_sgp40_data(MockSGP40()),
            package_ina238_data(0, MockINA238()),
            make_soil_fn(2, MockSeesaw())(),
        ]
        for packet in packets:
            sender.send(packet)
            self.assertLessEqual(
                len(sender.radio.last_sent), 60,
                f"Packet too large: {sender.radio.last_sent}"
            )


# ── package_battery_data ──────────────────────────────────────────────────────

class TestPackageBatteryData(unittest.TestCase):

    def test_keys_present(self):
        data = package_battery_data(MockMAX17048())
        self.assertEqual(data["t"], "batt")
        self.assertIn("v", data)
        self.assertIn("soc", data)

    def test_rounding(self):
        data = package_battery_data(MockMAX17048())
        self.assertEqual(data["v"], round(data["v"], 2))
        self.assertEqual(data["soc"], round(data["soc"], 1))

    def test_values(self):
        data = package_battery_data(MockMAX17048())
        self.assertEqual(data["v"], 3.85)
        self.assertEqual(data["soc"], 72.0)


# ── package_uv_data ───────────────────────────────────────────────────────────

class TestPackageUVData(unittest.TestCase):

    def test_keys_present(self):
        data = package_uv_data(MockLTR390())
        self.assertEqual(data["t"], "uv")
        self.assertIn("uv", data)
        self.assertIn("uvi", data)
        self.assertIn("lux", data)

    def test_rounding(self):
        data = package_uv_data(MockLTR390())
        self.assertEqual(data["uvi"], round(data["uvi"], 2))
        self.assertEqual(data["lux"], round(data["lux"], 1))

    def test_values(self):
        data = package_uv_data(MockLTR390())
        self.assertEqual(data["uv"], 10)
        self.assertEqual(data["uvi"], 1.5)
        self.assertEqual(data["lux"], 200.0)


# ── package_radio_temp ────────────────────────────────────────────────────────

class TestPackageRadioTemp(unittest.TestCase):

    def test_keys_present(self):
        data = package_radio_temp(MockRFM69())
        self.assertEqual(data["t"], "rt")
        self.assertIn("tmp", data)

    def test_value(self):
        data = package_radio_temp(MockRFM69())
        self.assertEqual(data["tmp"], 28)


# ── package_sht40_data ────────────────────────────────────────────────────────

class TestPackageSHT40Data(unittest.TestCase):

    def test_keys_present(self):
        data = package_sht40_data(MockSHT40())
        self.assertEqual(data["t"], "sht")
        self.assertIn("tmp", data)
        self.assertIn("rh", data)

    def test_rounding(self):
        data = package_sht40_data(MockSHT40())
        self.assertEqual(data["tmp"], round(data["tmp"], 2))
        self.assertEqual(data["rh"], round(data["rh"], 1))

    def test_values(self):
        data = package_sht40_data(MockSHT40())
        self.assertEqual(data["tmp"], 23.5)
        self.assertEqual(data["rh"], 55.0)


# ── package_sgp40_data ────────────────────────────────────────────────────────

class TestPackageSGP40Data(unittest.TestCase):

    def test_keys_present(self):
        data = package_sgp40_data(MockSGP40())
        self.assertEqual(data["t"], "voc")
        self.assertIn("voc", data)

    def test_uncompensated_uses_raw(self):
        data = package_sgp40_data(MockSGP40())
        self.assertEqual(data["voc"], 32000)

    def test_compensated_calls_measure_raw(self):
        data = package_sgp40_data(MockSGP40(), temp=23.5, humidity=55.0)
        self.assertEqual(data["voc"], 30000)

    def test_make_sgp40_compensated_fn(self):
        fn = make_sgp40_compensated_fn(MockSHT40(), MockSGP40())
        data = fn()
        self.assertEqual(data["t"], "voc")
        self.assertIn("voc", data)
        # Compensated path returns MockSGP40.measure_raw() value
        self.assertEqual(data["voc"], 30000)


# ── package_ina238_data ───────────────────────────────────────────────────────

class TestPackageINA238Data(unittest.TestCase):

    def test_keys_present(self):
        data = package_ina238_data(0, MockINA238())
        self.assertEqual(data["t"], "pw0")
        self.assertIn("v", data)
        self.assertIn("ma", data)
        self.assertIn("mw", data)

    def test_sensor_id_in_type(self):
        for sid in range(4):
            data = package_ina238_data(sid, MockINA238())
            self.assertEqual(data["t"], f"pw{sid}")

    def test_unit_conversion(self):
        # current and power are stored in amps/watts but reported as mA/mW
        data = package_ina238_data(0, MockINA238())
        self.assertAlmostEqual(data["ma"], 250.0, places=0)
        self.assertAlmostEqual(data["mw"], 3004.0, places=0)

    def test_rounding(self):
        data = package_ina238_data(0, MockINA238())
        self.assertEqual(data["v"], round(data["v"], 3))
        self.assertEqual(data["ma"], round(data["ma"], 1))
        self.assertEqual(data["mw"], round(data["mw"], 1))


# ── make_soil_fn ──────────────────────────────────────────────────────────────

class TestMakeSoilFn(unittest.TestCase):

    def test_returns_callable(self):
        self.assertTrue(callable(make_soil_fn(0, MockSeesaw())))

    def test_type_tag(self):
        for sid in range(3):
            fn = make_soil_fn(sid, MockSeesaw())
            self.assertEqual(fn()["t"], f"s{sid}")

    def test_keys_present(self):
        data = make_soil_fn(2, MockSeesaw())()
        self.assertIn("m", data)
        self.assertIn("tmp", data)

    def test_value_types(self):
        data = make_soil_fn(2, MockSeesaw())()
        self.assertIsInstance(data["m"], (int, float))
        self.assertIsInstance(data["tmp"], float)

    def test_values(self):
        data = make_soil_fn(1, MockSeesaw())()
        self.assertEqual(data["m"], 512)
        self.assertAlmostEqual(data["tmp"], 22.5, places=2)


if __name__ == "__main__":
    unittest.main()