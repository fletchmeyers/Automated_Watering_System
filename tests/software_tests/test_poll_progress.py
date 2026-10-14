# tests/software_tests/test_poll_progress.py
"""The dashboard's Refresh, step by step: sync_indoor's poll progress file,
moved along by the radio loop and the Wi-Fi service."""
import asyncio
import json

import pytest

import sync_indoor
import wifi_nodes
from sync_indoor import (
    get_poll_progress, poll_progress, poll_progress_answered, poll_progress_failed,
    poll_progress_reading, poll_progress_sent, start_poll_progress, wait_for_poll_result,
)
from test_wifi_nodes import service, wifi_node   # noqa: F401  (the fixture)


@pytest.fixture(autouse=True)
def progress_file(tmp_path, monkeypatch):
    monkeypatch.setattr(sync_indoor, "POLL_PROGRESS_FILE", str(tmp_path / "progress.json"))
    monkeypatch.setattr(sync_indoor, "POLL_RESULT_FILE", str(tmp_path / "result.json"))


def states():
    return {s: v["state"] for s, v in get_poll_progress()["steps"].items()}


def test_a_radio_poll_goes_green_step_by_step():
    start_poll_progress(2, "radio", "abc")
    p = get_poll_progress()
    assert p["id"] == "abc" and p["link"] == "radio" and not p["end"]
    assert states() == {"pi": "done", "sent": "active", "heard": "wait", "data": "wait", "saved": "wait"}
    assert p["steps"]["sent"]["note"] == "queued"

    poll_progress_sent(2, "radio")
    assert states()["sent"] == "done" and states()["heard"] == "active"
    assert get_poll_progress()["steps"]["sent"]["note"] == "over the radio"

    poll_progress(2, "heard")
    poll_progress_reading(2)
    poll_progress_reading(2)
    assert states()["data"] == "active"
    assert get_poll_progress()["steps"]["data"]["note"] == "2 so far"

    poll_progress_answered(2, [{"t": "sht"}, {"t": "rt"}])
    p = get_poll_progress()
    assert set(states().values()) == {"done"} and p["end"]
    assert p["steps"]["data"]["note"] == "2 readings"


def test_a_retry_shows_amber_until_the_node_answers():
    start_poll_progress(1, "radio")
    poll_progress_sent(1, "radio")
    poll_progress_sent(1, "radio")
    assert states()["heard"] == "retry"
    assert "sent again (try 2)" == get_poll_progress()["steps"]["heard"]["note"]
    poll_progress(1, "heard")
    assert states()["heard"] == "done"


def test_no_answer_fails_the_step_it_got_to():
    start_poll_progress(2, "radio")
    poll_progress_sent(2, "radio")
    poll_progress_failed(2, "no answer over the radio in 45 s")
    p = get_poll_progress()
    assert states() == {"pi": "done", "sent": "done", "heard": "failed", "data": "wait", "saved": "wait"}
    assert p["end"] and p["steps"]["heard"]["note"] == "no answer over the radio in 45 s"
    poll_progress(2, "heard")                       # a late answer doesn't undo it
    assert states()["heard"] == "failed"


def test_an_empty_answer_finishes_amber():
    '''A node with no sensors answers with just a batch_end: no header, no readings.'''
    start_poll_progress(2, "radio")
    poll_progress_sent(2, "radio")
    poll_progress_answered(2, None)
    assert states() == {"pi": "done", "sent": "done", "heard": "done", "data": "warn", "saved": "warn"}
    assert get_poll_progress()["end"]


def test_other_nodes_and_automatic_polls_leave_it_alone():
    poll_progress(1, "heard")                       # no Refresh running: nothing written
    assert get_poll_progress() is None
    start_poll_progress(1, "radio")
    poll_progress(3, "heard")                       # another node's answer
    poll_progress_reading(3)
    assert states()["heard"] == "wait"


def test_the_api_stops_waiting_once_the_radio_loop_gives_up():
    start_poll_progress(2, "radio")
    poll_progress_failed(2, "no answer over the radio in 45 s")
    assert wait_for_poll_result(timeout=5, node_id=2) is None


def test_an_empty_answer_is_a_result_not_a_timeout(tmp_path):
    (tmp_path / "result.json").write_text("[]")
    assert wait_for_poll_result(timeout=5) == []


def test_a_wifi_refresh_is_shown_step_by_step(service):
    from test_wifi_nodes import run_with_node
    svc, conn, tmp_path = service
    start_poll_progress(20, "wifi", "r1")
    [reply] = run_with_node(svc, [{"t": "poll", "n": 20}])
    assert reply["status"] == "ok"
    p = get_poll_progress()
    assert p["end"] and set(states().values()) == {"done"}
    assert p["steps"]["sent"]["note"] == "over Wi-Fi"
    assert p["steps"]["data"]["note"] == "1 reading"


def test_a_wifi_node_that_isnt_connected(service):
    svc, conn, tmp_path = service
    start_poll_progress(20, "wifi")
    reply = asyncio.run(svc.run_request({"t": "poll", "n": 20}))
    assert reply["status"] == "offline"
