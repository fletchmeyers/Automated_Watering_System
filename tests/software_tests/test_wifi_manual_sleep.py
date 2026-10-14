# tests/software_tests/test_wifi_manual_sleep.py
"""Sleeping a node by hand (sync_indoor.py sleep) when it's on Wi-Fi."""
import asyncio
import json

import pytest

import sync_indoor
import wifi_nodes
from test_wifi_nodes import service, wifi_node   # noqa: F401  (the fixture)


@pytest.fixture(autouse=True)
def node_info(tmp_path, monkeypatch):
    monkeypatch.setattr(sync_indoor, "NODE_INFO_FILE", tmp_path / "node_info.json")


async def sleepy_node(port, answer):
    '''Answers polls with nothing, and a sleep command with answer (or not at all).'''
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b'{"t":"hello","q":0,"n":20}\n')
    await writer.drain()
    q = 1
    try:
        while line := await reader.readline():
            command = json.loads(line)
            if command["t"] == "poll":
                reply = {"t": "batch_end", "exp": 0, "snt": 0}
            elif command["t"] == "sleep" and answer is not None:
                reply = {"t": "sleep_ack", "w": command["w"], **answer}
            else:
                continue
            writer.write((json.dumps({**reply, "q": q, "n": 20}) + "\n").encode())
            q += 1
            await writer.drain()
    finally:
        writer.close()


def ask(svc, request, answer):
    svc.poll_interval = 60

    async def scenario():
        server = await asyncio.start_server(svc.handle, "127.0.0.1", 0)
        control = await asyncio.start_server(svc.control, "127.0.0.1", 0)
        node = asyncio.create_task(sleepy_node(server.sockets[0].getsockname()[1], answer))
        while 20 not in svc.links:
            await asyncio.sleep(0.02)
        reply = await asyncio.to_thread(wifi_nodes.ask_wifi_service, request, 10,
                                        control.sockets[0].getsockname()[1])
        node.cancel()
        server.close()
        control.close()
        return reply

    return asyncio.run(scenario())


def test_a_wifi_node_sleeps_now_and_its_recorded(service):
    svc, conn, tmp_path = service
    reply = ask(svc, {"t": "sleep", "n": 20, "minutes": 5}, {"ok": 1})
    assert reply["status"] == "ok" and reply["acked"]
    assert sync_indoor.get_node_info()["20"]["sleep_until"] == reply["until"]
    assert sync_indoor.recorded_wake(20) is not None   # neither service polls it now


def test_a_refusal_says_why(service):
    svc, conn, tmp_path = service
    reply = ask(svc, {"t": "sleep", "n": 20, "minutes": 5}, {"ok": 0, "why": "clock not set"})
    assert reply == {"status": "refused", "why": "clock not set"}
    assert sync_indoor.recorded_wake(20) is None


def test_no_answer_counts_as_asleep(service):
    svc, conn, tmp_path = service
    reply = ask(svc, {"t": "sleep", "n": 20, "minutes": 5}, None)
    assert reply["status"] == "ok" and not reply["acked"]


def test_bad_minutes(service):
    svc, conn, tmp_path = service
    reply = ask(svc, {"t": "sleep", "n": 20, "minutes": 0}, {"ok": 1})
    assert reply["status"] == "error" and "minutes" in reply["error"]


# ── sync_indoor.py sleep ─────────────────────────────────────────────────────

def test_the_command_goes_over_the_link_in_use(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sync_indoor, "SLEEP_REQUEST_FILE", str(tmp_path / "sleep.json"))
    asked = []

    def wifi(request, timeout):
        asked.append(request)
        return {"status": "ok", "until": "2026-10-14T15:05:00", "acked": True}

    assert sync_indoor.sleep_now(3, 5, link="wifi", ask_wifi=wifi)
    assert asked == [{"t": "sleep", "n": 3, "minutes": 5}]
    assert "asleep until 2026-10-14 15:05:00" in capsys.readouterr().out
    assert not (tmp_path / "sleep.json").exists()

    assert sync_indoor.sleep_now(2, 5, link="radio")
    assert json.loads((tmp_path / "sleep.json").read_text()) == {"n": 2, "minutes": 5}


@pytest.mark.parametrize("reply, said", [
    ({"status": "refused", "why": "clock not set"}, "it refused (clock not set)"),
    ({"status": "offline"}, "isn't connected to the Pi over Wi-Fi"),
    ({"status": "unavailable"}, "garden-wifi) isn't running"),
])
def test_why_it_didnt_sleep(reply, said, capsys):
    assert not sync_indoor.sleep_now(3, 5, link="wifi", ask_wifi=lambda r, timeout: reply)
    assert said in capsys.readouterr().out
