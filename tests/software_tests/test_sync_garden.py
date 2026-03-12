import pytest
import json
import time
from unittest.mock import MagicMock, patch
from outdoor.sync_garden import (
    _parse_iso_timestamp,
    check_for_command,
    handle_sync,
    interruptible_sleep,
)


# ── _parse_iso_timestamp ──────────────────────────────────────────────────────

def test_parse_valid_timestamp():
    result = _parse_iso_timestamp("2026-03-12T10:30:45")
    assert result.tm_year == 2026
    assert result.tm_mon == 3
    assert result.tm_mday == 12
    assert result.tm_hour == 10
    assert result.tm_min == 30
    assert result.tm_sec == 45


def test_parse_timestamp_midnight():
    result = _parse_iso_timestamp("2026-01-01T00:00:00")
    assert result.tm_hour == 0
    assert result.tm_min == 0
    assert result.tm_sec == 0


def test_parse_timestamp_end_of_day():
    result = _parse_iso_timestamp("2026-12-31T23:59:59")
    assert result.tm_hour == 23
    assert result.tm_min == 59
    assert result.tm_sec == 59


def test_parse_timestamp_wday_yday_isdst():
    """wday/yday/isdst should be set to 0, -1, -1 as PCF8523 doesn't use them."""
    result = _parse_iso_timestamp("2026-03-12T10:00:00")
    assert result.tm_wday == 0
    assert result.tm_yday == -1
    assert result.tm_isdst == -1


def test_parse_timestamp_missing_time_part():
    with pytest.raises(ValueError):
        _parse_iso_timestamp("2026-03-12")


def test_parse_timestamp_missing_t_separator():
    with pytest.raises(ValueError):
        _parse_iso_timestamp("2026-03-12 10:30:45")


def test_parse_timestamp_empty_string():
    with pytest.raises(ValueError):
        _parse_iso_timestamp("")


def test_parse_timestamp_non_numeric():
    with pytest.raises(ValueError):
        _parse_iso_timestamp("YYYY-MM-DDTHH:MM:SS")


# ── check_for_command ─────────────────────────────────────────────────────────

def test_check_for_command_no_packet():
    radio = MagicMock()
    radio.receive.return_value = None
    result = check_for_command(radio)
    assert result is None


def test_check_for_command_valid_packet():
    radio = MagicMock()
    command = {"t": "sync", "ts": "2026-03-12T10:00:00"}
    # Prepend 4 fake header bytes as RFM69 with_header=True does
    payload = b"\x00\x00\x00\x00" + json.dumps(command).encode("utf-8")
    radio.receive.return_value = payload
    result = check_for_command(radio)
    assert result == command


def test_check_for_command_passes_timeout():
    radio = MagicMock()
    radio.receive.return_value = None
    check_for_command(radio, timeout=2.0)
    radio.receive.assert_called_once_with(timeout=2.0, with_header=True)


def test_check_for_command_malformed_json():
    radio = MagicMock()
    radio.receive.return_value = b"\x00\x00\x00\x00not valid json{{{"
    result = check_for_command(radio)
    assert result is None


def test_check_for_command_non_utf8_payload():
    radio = MagicMock()
    radio.receive.return_value = b"\x00\x00\x00\x00\xff\xfe\xfd"
    result = check_for_command(radio)
    assert result is None


# ── handle_sync ───────────────────────────────────────────────────────────────

def test_handle_sync_sets_rtc():
    rtc = MagicMock()
    sender = MagicMock()
    get_timestamp_fn = MagicMock(return_value="2026-03-12T10:00:00")
    command = {"t": "sync", "ts": "2026-03-12T10:00:00"}
    handle_sync(command, rtc, sender, get_timestamp_fn)
    assert rtc.datetime is not None


def test_handle_sync_sends_ack():
    rtc = MagicMock()
    sender = MagicMock()
    get_timestamp_fn = MagicMock(return_value="2026-03-12T10:00:00")
    command = {"t": "sync", "ts": "2026-03-12T10:00:00"}
    handle_sync(command, rtc, sender, get_timestamp_fn)
    sender.send.assert_called_once()
    sent_packet = sender.send.call_args[0][0]
    assert sent_packet["t"] == "sync_ack"
    assert sent_packet["ts"] == "2026-03-12T10:00:00"


def test_handle_sync_missing_ts_field():
    """Missing ts field should return early without touching RTC or sending ack."""
    rtc = MagicMock()
    sender = MagicMock()
    handle_sync({"t": "sync"}, rtc, sender, MagicMock())
    sender.send.assert_not_called()
    rtc.assert_not_called()
    assert rtc.method_calls == []


def test_handle_sync_bad_timestamp():
    """Malformed timestamp should return early without sending ack."""
    rtc = MagicMock()
    sender = MagicMock()
    handle_sync({"t": "sync", "ts": "not-a-timestamp"}, rtc, sender, MagicMock())
    sender.send.assert_not_called()


def test_handle_sync_ack_uses_get_timestamp_fn():
    """The ack ts should come from get_timestamp_fn, not the command ts."""
    rtc = MagicMock()
    sender = MagicMock()
    get_timestamp_fn = MagicMock(return_value="2026-03-12T10:00:05")
    handle_sync({"t": "sync", "ts": "2026-03-12T10:00:00"}, rtc, sender, get_timestamp_fn)
    sent_packet = sender.send.call_args[0][0]
    assert sent_packet["ts"] == "2026-03-12T10:00:05"


# ── interruptible_sleep ───────────────────────────────────────────────────────

def test_interruptible_sleep_no_command():
    """Should return None after sleeping the full duration."""
    radio = MagicMock()
    radio.receive.return_value = None
    result = interruptible_sleep(radio, duration=1.0, chunk=0.5)
    assert result is None
    assert radio.receive.call_count == 2  # 1.0s / 0.5s chunks


def test_interruptible_sleep_returns_command_immediately():
    """Should return as soon as a command is received."""
    radio = MagicMock()
    command = {"t": "sync", "ts": "2026-03-12T10:00:00"}
    payload = b"\x00\x00\x00\x00" + json.dumps(command).encode("utf-8")
    radio.receive.return_value = payload
    result = interruptible_sleep(radio, duration=10.0, chunk=0.5)
    assert result == command
    assert radio.receive.call_count == 1  # returned on first chunk


def test_interruptible_sleep_returns_command_mid_sleep():
    """Should return early when command arrives partway through sleep."""
    radio = MagicMock()
    command = {"t": "sync", "ts": "2026-03-12T10:00:00"}
    payload = b"\x00\x00\x00\x00" + json.dumps(command).encode("utf-8")
    # First two chunks return nothing, third returns a command
    radio.receive.side_effect = [None, None, payload]
    result = interruptible_sleep(radio, duration=10.0, chunk=0.5)
    assert result == command
    assert radio.receive.call_count == 3