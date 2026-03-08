import unittest
import json
from tests.software_tests.mock_hardware import MockMAX17048, MockLTR390, MockSeesaw, MockRFM69, MockRTC

from unittest.mock import MagicMock, patch
from outdoor.communication_garden import package_battery_data, package_uv_data, make_soil_fn, PacketSender





class TestPackaging(unittest.TestCase):

    def test_sequence_starts_at_zero(self):
        sender = PacketSender(node_id=1, radio=MockRFM69())
        sender.send({"t": "batt"})
        sent = json.loads(sender.radio.last_sent.decode("utf-8"))
        self.assertEqual(sent["q"], 0)

    def test_node_id_in_packet(self):
        sender = PacketSender(node_id=42, radio=MockRFM69())
        sender.send({"t": "batt"})
        sent = json.loads(sender.radio.last_sent.decode("utf-8"))
        self.assertEqual(sent["n"], 42)

    def test_oversized_packet_does_not_raise(self):
        # Radio rejects oversized packet internally, but send() should handle it gracefully
        sender = PacketSender(node_id=1, radio=MockRFM69())
        big_data = {"t": "x", "junk": "a" * 100}
        try:
            sender.send(big_data)
        except Exception as e:
            self.fail(f"send() raised unexpectedly: {e}")

    def test_send_mutates_original_dict(self):
        # Confirm that send() adds "n" and "q" to the dict it receives
        # This matters because main.py appends data to sd_buffer *after* send(),
        # so the SD copy will include n and q. Test confirms this is intentional.
        sender = PacketSender(node_id=1, radio=MockRFM69())
        data = {"t": "batt"}
        sender.send(data)
        self.assertIn("n", data)
        self.assertIn("q", data)

    def test_battery_data_keys(self):
        data = package_battery_data(MockMAX17048())
        self.assertIn("t", data)
        self.assertIn("v", data)
        self.assertIn("soc", data)
        self.assertEqual(data["t"], "batt")

    def test_battery_data_rounding(self):
        data = package_battery_data(MockMAX17048())
        self.assertEqual(data["v"], 3.85)   # already 2dp
        self.assertEqual(data["soc"], 72.0)

    def test_uv_data_keys(self):
        data = package_uv_data(MockLTR390())
        self.assertIn("t", data)
        self.assertIn("uv", data)
        self.assertIn("uvi", data)
        self.assertIn("lux", data)
        self.assertEqual(data["t"], "uv")

    def test_uv_data_rounding(self):
        data = package_uv_data(MockLTR390())
        # uvi should be 2dp, lux 1dp
        self.assertEqual(data["uvi"], round(data["uvi"], 2))
        self.assertEqual(data["lux"], round(data["lux"], 1))

    def test_soil_fn_factory(self):
        fn = make_soil_fn(2, MockSeesaw())
        data = fn()
        self.assertEqual(data["t"], "s2")
        self.assertIn("m", data)
        self.assertIn("tmp", data)

    def test_soil_fn_different_ids(self):
        fn0 = make_soil_fn(0, MockSeesaw())
        fn2 = make_soil_fn(2, MockSeesaw())
        self.assertEqual(fn0()["t"], "s0")
        self.assertEqual(fn2()["t"], "s2")

    def test_soil_fn_returns_callable(self):
        result = make_soil_fn(2, MockSeesaw())
        self.assertTrue(callable(result))

    def test_soil_data_value_types(self):
        fn = make_soil_fn(2, MockSeesaw())
        data = fn()
        self.assertIsInstance(data["m"], (int, float))
        self.assertIsInstance(data["tmp"], float)

    def test_send_packet_increments_sequence(self):
        sender = PacketSender(node_id=1, radio=MockRFM69())
        sender.send({"t": "batt"})
        sender.send({"t": "uv"})
        sent = json.loads(sender.radio.last_sent.decode("utf-8"))
        self.assertEqual(sent["q"], 1)

    def test_packet_fits_in_radio_limit(self):
        sender = PacketSender(node_id=1, radio=MockRFM69())
        data = package_battery_data(MockMAX17048())
        sender.send(data)
        self.assertLessEqual(len(sender.radio.last_sent), 60)

    def test_all_sensor_packets_fit_radio_limit(self):
        sender = PacketSender(node_id=1, radio=MockRFM69())
        packets = [
            package_battery_data(MockMAX17048()),
            package_uv_data(MockLTR390()),
            make_soil_fn(2, MockSeesaw())(),
        ]
        for packet in packets:
            sender.send(packet)
            self.assertLessEqual(
                len(sender.radio.last_sent), 60,
                f"Packet too large: {sender.radio.last_sent}"
            )

    def test_timestamp_packet_fits_in_radio_limit(self):
        sender = PacketSender(node_id=1, radio=MockRFM69())
        sender.send({"t": "ts", "v": "2026-03-06T12:00:00"})
        self.assertLessEqual(len(sender.radio.last_sent), 60)


if __name__ == "__main__":
    unittest.run()