# tests/software_tests/test_flask_api.py
"""The Pi's API (flask_api.py): Refresh and its progress, sleeping nodes,
node info. Needs Flask (on the Pi it's there; elsewhere these are skipped)."""
import json
import os
from datetime import datetime, timedelta

import pytest

pytest.importorskip("flask")
pytest.importorskip("flask_cors")
pytest.importorskip("requests")
os.environ["GARDEN_API_NO_THREADS"] = "1"   # no background threads: no real database or weather

import flask_api   # noqa: E402
import sync_indoor  # noqa: E402


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(sync_indoor, "NODE_INFO_FILE", tmp_path / "node_info.json")
    monkeypatch.setattr(sync_indoor, "POLL_PROGRESS_FILE", str(tmp_path / "progress.json"))
    monkeypatch.setattr(sync_indoor, "POLL_RESULT_FILE", str(tmp_path / "result.json"))
    monkeypatch.setattr(sync_indoor, "COMMAND_FILE", str(tmp_path / "command.json"))
    monkeypatch.setattr(flask_api, "_last_poll", {})
    monkeypatch.setattr(flask_api, "load_nodes", lambda **kw: {2: {"name": "M0"}})
    monkeypatch.setattr(flask_api, "is_wifi_node", lambda n: False)
    return flask_api.app.test_client()


def poll(client, node=2, rid="r1"):
    resp = client.post("/api/poll", json={"node_id": node, "rid": rid})
    return resp.status_code, resp.get_json()


def test_no_refresh_running(api):
    assert api.get("/api/poll_progress").get_json() == {"status": "idle"}


def test_a_sleeping_node_fails_at_once(api):
    wake = (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")
    sync_indoor.update_node_info(2, sleep_until=wake)
    status, data = poll(api)
    assert status == 200 and data["status"] == "asleep" and data["until"] == wake
    progress = api.get("/api/poll_progress").get_json()
    assert progress["status"] == "finished" and progress["id"] == "r1"
    assert progress["steps"]["sent"]["state"] == "failed"
    assert progress["steps"]["sent"]["note"] == f"asleep until {wake[11:16]}"


def test_a_radio_refresh_returns_the_readings(api, monkeypatch):
    sent = []
    monkeypatch.setattr(flask_api, "request_poll", lambda n, link_plan=None: sent.append(n))
    monkeypatch.setattr(flask_api, "wait_for_poll_result",
                        lambda timeout, node_id: [{"t": "sht", "tmp": 21.5, "n": 2}])
    status, data = poll(api)
    assert sent == [2] and data == {"status": "ok", "packets": [{"t": "sht", "tmp": 21.5, "n": 2}]}

    # Another one straight away is turned back until the cooldown's over.
    status, data = poll(api)
    assert status == 429 and data["status"] == "cooldown" and data["retry_after"] > 0


def test_an_empty_answer_is_not_a_timeout(api, monkeypatch):
    monkeypatch.setattr(flask_api, "request_poll", lambda n, link_plan=None: None)
    monkeypatch.setattr(flask_api, "wait_for_poll_result", lambda timeout, node_id: [])
    assert poll(api)[1]["status"] == "empty"


def test_no_answer_says_why(api, monkeypatch):
    monkeypatch.setattr(flask_api, "request_poll", lambda n, link_plan=None: None)
    monkeypatch.setattr(flask_api, "wait_for_poll_result", lambda timeout, node_id: None)
    assert poll(api)[1]["status"] == "timeout"
    progress = api.get("/api/poll_progress").get_json()
    failed = [s for s in progress["steps"].values() if s["state"] == "failed"]
    assert failed and "garden-radio" in failed[0]["note"]


def test_a_wifi_node_that_isnt_connected(api, monkeypatch):
    monkeypatch.setattr(flask_api, "is_wifi_node", lambda n: True)
    monkeypatch.setattr(flask_api, "ask_wifi_service", lambda request, timeout: {"status": "offline"})
    assert poll(api)[1]["status"] == "offline"
    progress = api.get("/api/poll_progress").get_json()
    assert progress["link"] == "wifi"
    assert any(s.get("note") == "not connected to the Pi over Wi-Fi" for s in progress["steps"].values())


def test_node_info_includes_offline_sensors_and_sleep(api):
    sync_indoor.record_sensors_off(1, ["sht"])
    sync_indoor.update_node_info(2, sleep_until="2026-10-14T18:45:00", sleep_reason="battery 5%, under 25%")
    nodes = api.get("/api/node_info").get_json()["nodes"]
    assert list(nodes["1"]["sensors_off"]) == ["sht"]
    assert nodes["2"]["sleep_reason"] == "battery 5%, under 25%"
