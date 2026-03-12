import pytest
import json
import tempfile
import os
from pathlib import Path
from unittest.mock import patch, MagicMock
from indoor.sync_indoor import sensor_health_report, check_and_forward_command, request_sync, COMMAND_FILE

# ── Helpers ───────────────────────────────────────────────────────────────────

def make_data_file(lines):
    """Write a list of dicts as JSON lines to a temp file, return its path."""
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
    for line in lines:
        f.write(json.dumps(line) + "\n")
    f.close()
    return f.name


# ── sensor_health_report ──────────────────────────────────────────────────────

def test_health_report_missing_file():
    result = sensor_health_report("/nonexistent/path/data.txt")
    assert result == {}


def test_health_report_empty_file():
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
    f.close()
    try:
        result = sensor_health_report(f.name)
        assert result == {}
    finally:
        os.unlink(f.name)


def test_health_report_only_control_packets():
    """ts/sync/sync_ack packets should be excluded — result should be empty."""
    path = make_data_file([
        {"t": "ts", "v": "2026-03-12T10:00:00"},
        {"t": "sync_ack", "ts": "2026-03-12T10:00:01"},
        {"t": "sync", "ts": "2026-03-12T10:00:02"},
    ])
    try:
        result = sensor_health_report(path)
        assert result == {}
    finally:
        os.unlink(path)


def test_health_report_counts_sensor_types():
    path = make_data_file([
        {"t": "batt", "v": 3.9, "soc": 85.0},
        {"t": "batt", "v": 3.8, "soc": 84.0},
        {"t": "uv",   "uv": 10, "uvi": 0.5, "lux": 200.0},
    ])
    try:
        result = sensor_health_report(path)
        assert result["types"]["batt"] == 2
        assert result["types"]["uv"] == 1
        assert result["window"] == 3
        assert result["expected"] == 2
    finally:
        os.unlink(path)


def test_health_report_detects_missing_sensor():
    """A sensor present early in the window but absent recently should be flagged."""
    packets = (
        [{"t": "batt"}, {"t": "uv"}] * 10   # batt and uv appear early
        + [{"t": "batt"}] * 10               # only batt in recent quarter
    )
    path = make_data_file(packets)
    try:
        result = sensor_health_report(path)
        assert "uv" in result["missing"]
        assert "batt" not in result["missing"]
    finally:
        os.unlink(path)


def test_health_report_no_missing_sensors():
    packets = [{"t": "batt"}, {"t": "uv"}] * 20
    path = make_data_file(packets)
    try:
        result = sensor_health_report(path)
        assert result["missing"] == []
    finally:
        os.unlink(path)


def test_health_report_respects_n_lines():
    """Only the last n lines should be examined."""
    # First 50 lines have a "old" type that should not appear in results
    packets = [{"t": "old"}] * 50 + [{"t": "batt"}] * 10
    path = make_data_file(packets)
    try:
        result = sensor_health_report(path, n=10)
        assert "old" not in result["types"]
        assert "batt" in result["types"]
    finally:
        os.unlink(path)


def test_health_report_skips_malformed_lines():
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
    f.write('{"t": "batt", "v": 3.9}\n')
    f.write("this is not json\n")
    f.write('{"t": "batt", "v": 3.8}\n')
    f.close()
    try:
        result = sensor_health_report(f.name)
        assert result["types"]["batt"] == 2
        assert result["window"] == 2
    finally:
        os.unlink(f.name)


def test_health_report_skips_blank_lines():
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
    f.write('{"t": "batt"}\n\n\n{"t": "uv"}\n')
    f.close()
    try:
        result = sensor_health_report(f.name)
        assert result["window"] == 2
    finally:
        os.unlink(f.name)


# ── check_and_forward_command ─────────────────────────────────────────────────
def test_forward_command_no_file(tmp_path):
    cmd_file = tmp_path / "pico_command.json"
    mock_radio = MagicMock()
    with patch("indoor.sync_indoor.COMMAND_FILE", str(cmd_file)):
        result = check_and_forward_command(mock_radio)
    assert result is False
    mock_radio.send.assert_not_called()


def test_forward_command_sends_and_deletes(tmp_path):
    cmd_file = tmp_path / "pico_command.json"
    cmd_file.write_text('{"t": "sync", "ts": "2026-03-12T10:00:00"}')
    mock_radio = MagicMock()
    with patch("indoor.sync_indoor.COMMAND_FILE", str(cmd_file)):
        result = check_and_forward_command(mock_radio)
    assert result is True
    mock_radio.send.assert_called_once()
    assert not cmd_file.exists()


def test_forward_command_deletes_file_on_send_failure(tmp_path):
    cmd_file = tmp_path / "pico_command.json"
    cmd_file.write_text('{"t": "sync", "ts": "2026-03-12T10:00:00"}')
    mock_radio = MagicMock()
    mock_radio.send.side_effect = RuntimeError("radio error")
    with patch("indoor.sync_indoor.COMMAND_FILE", str(cmd_file)):
        result = check_and_forward_command(mock_radio)
    assert result is False
    assert not cmd_file.exists()


def test_forward_command_handles_corrupt_file(tmp_path):
    cmd_file = tmp_path / "pico_command.json"
    cmd_file.write_text("not valid json{{{")
    mock_radio = MagicMock()
    with patch("indoor.sync_indoor.COMMAND_FILE", str(cmd_file)):
        result = check_and_forward_command(mock_radio)
    assert result is False
    mock_radio.send.assert_not_called()


# ── request_sync ──────────────────────────────────────────────────────────────

def test_request_sync_writes_command_file(tmp_path):
    cmd_file = tmp_path / "pico_command.json"
    with patch("indoor.sync_indoor.COMMAND_FILE", str(cmd_file)), \
         patch("indoor.sync_indoor.datetime") as mock_dt:
        mock_dt.now.return_value.strftime.return_value = "2026-03-12T10:00:00"
        request_sync()
    assert cmd_file.exists()
    data = json.loads(cmd_file.read_text())
    assert data["t"] == "sync"
    assert data["ts"] == "2026-03-12T10:00:00"