# tests/software_tests/test_fair_sync.py
# Sync sessions for several nodes share the radio, taking turns chunk by chunk.
import pytest

import communication_indoor
from communication_indoor import SyncManager


@pytest.fixture(autouse=True)
def archive(tmp_path, monkeypatch):
    '''Keep synced lines out of the real archive/ folder.'''
    monkeypatch.setattr(communication_indoor, "ARCHIVE_DIR", tmp_path)


class FakeNodes:
    '''Nodes with `backlog[n]` lines logged, answering chunk requests like send_sync_chunk().'''

    def __init__(self, backlog, lose_lines_for=()):
        self.backlog = dict(backlog)
        self.lose_lines_for = set(lose_lines_for)   # nodes whose next chunk arrives short

    def answer(self, sync, command):
        n, k = command["n"], command["k"]
        start = command.get("o", 0)
        count = max(0, min(k, self.backlog[n] - start))
        lines = [{"t": "rt", "tmp": 20.0, "ts": f"2026-10-03T00:00:{i % 60:02}", "i": i}
                 for i in range(start, start + count)]
        sync.on_send(command)
        if n in self.lose_lines_for:
            self.lose_lines_for.discard(n)
            lines = lines[:-1]
        for line in lines:
            sync.collect(line)
        sync.handle_end({"t": "se", "g": 1, "o": start + count, "c": count,
                         "m": int(start + count < self.backlog[n])})


def drive(sync, nodes, max_requests=200, reachable=lambda n: True):
    '''Run chunk requests until nothing is left to do; return the node order served.'''
    served = []
    for _ in range(max_requests):
        command = sync.next_command(reachable)
        if command is None:
            break
        served.append(command["n"])
        nodes.answer(sync, command)
    return served


def test_two_nodes_take_turns_chunk_by_chunk():
    sync = SyncManager([1, 2], chunk_lines=8)
    served = drive(sync, FakeNodes({1: 40, 2: 40}))
    assert served == [1, 2] * 5
    assert sync.sessions == {} and sync.active is None


def test_when_one_node_catches_up_the_other_carries_on():
    sync = SyncManager([1, 2], chunk_lines=8)
    served = drive(sync, FakeNodes({1: 80, 2: 16}))
    assert served == [1, 2, 1, 2] + [1] * 8          # node 1: 10 chunks, node 2: 2


def test_each_node_has_its_own_hourly_limit():
    sync = SyncManager([1, 2], lines_per_session=16, chunk_lines=8)
    served = drive(sync, FakeNodes({1: 100, 2: 100}))
    assert served == [1, 2, 1, 2]                       # 16 lines each, then wait for the next hour
    assert sync.next_command() is None


def test_short_chunk_is_retried_on_that_nodes_next_turn():
    sync = SyncManager([1, 2], chunk_lines=8)
    nodes = FakeNodes({1: 16, 2: 16}, lose_lines_for={1})
    served = drive(sync, nodes)
    assert served == [1, 2, 1, 2, 1]                    # node 1's first chunk asked for twice
    assert sync._cursors == {1: {"g": 1, "o": 16}, 2: {"g": 1, "o": 16}}


def test_node_that_stops_being_available_drops_out_and_the_other_continues():
    sync = SyncManager([1, 2], chunk_lines=8)
    nodes = FakeNodes({1: 40, 2: 40})
    awake = {1, 2}
    served = []
    for _ in range(20):
        command = sync.next_command(lambda n: n in awake)
        if command is None:
            break
        served.append(command["n"])
        nodes.answer(sync, command)
        if len(served) == 3:
            awake.discard(2)                              # node 2 goes to sleep mid-session
    assert served == [1, 2, 1, 1, 1, 1]
    assert 2 not in sync.sessions


def test_timeout_ends_only_that_nodes_session():
    sync = SyncManager([1, 2], chunk_lines=8)
    nodes = FakeNodes({1: 24, 2: 24})
    nodes.answer(sync, sync.next_command())              # node 1, chunk 1
    assert sync.next_command()["n"] == 2
    sync.abort()                                          # node 2's chunk timed out
    assert list(sync.sessions) == [1]
    assert drive(sync, nodes) == [1, 1]


def test_manual_request_joins_the_rotation():
    sync = SyncManager([1], chunk_lines=8)               # node 2 isn't on the hourly list
    nodes = FakeNodes({1: 24, 2: 24})
    nodes.answer(sync, sync.next_command())
    sync.request_now(2)
    assert drive(sync, nodes) == [2, 1, 2, 1, 2]
