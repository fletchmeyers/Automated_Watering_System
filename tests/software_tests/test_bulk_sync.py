# tests/software_tests/test_bulk_sync.py
"""
End-to-end tests for the Pi-driven SD sync: the Pico's real send_sync_chunk()
and PacketSender fragmentation on one side, the Pi's real SyncManager and
FragmentReassembler on the other, joined by a fake radio that can drop packets.
"""
import json

import pytest

import communication_garden as garden
import communication_indoor as indoor
from communication_garden import PacketSender, send_sync_chunk, RADIO_MAX_BYTES
from communication_indoor import FragmentReassembler, SyncManager


class FakeRadio:
    '''Collects sent payloads; drop_at lists 0-based send indexes to lose in transit.'''
    def __init__(self, drop_at=()):
        self.sent = []
        self.count = 0
        self.drop_at = set(drop_at)

    def send(self, data):
        assert len(data) <= RADIO_MAX_BYTES, f"{len(data)}-byte packet sent"
        if self.count not in self.drop_at:
            self.sent.append(bytes(data))
        self.count += 1

    def take(self):
        out, self.sent = self.sent, []
        return out


@pytest.fixture
def sd(tmp_path, monkeypatch):
    '''Point the Pico's SD paths and the Pi's archive dir at a temp dir.'''
    monkeypatch.setattr(garden, "SD_DATA_FILE", str(tmp_path / "data.txt"))
    monkeypatch.setattr(garden, "SD_SENDING_FILE", str(tmp_path / "sending.txt"))
    monkeypatch.setattr(garden, "SD_CURSOR_FILE", str(tmp_path / "sync_cursor.txt"))
    monkeypatch.setattr(garden.time, "sleep", lambda s: None)
    archive = tmp_path / "archive"
    archive.mkdir()
    monkeypatch.setattr(indoor, "ARCHIVE_DIR", archive)
    return tmp_path


def write_log(sd, start, count):
    '''Append count log lines to data.txt, long enough that each needs 2 fragments.'''
    with open(sd / "data.txt", "a") as f:
        for i in range(start, start + count):
            line = {"t": "pw0", "mw": 134.3, "ma": 27.8, "v": 12.784,
                    "i": i, "ts": "2026-08-08T13:33:06"}
            f.write(json.dumps(line, separators=(",", ":")) + "\n")


def stored(sd):
    '''Every line the Pi has committed, in order.'''
    lines = []
    for path in sorted((sd / "archive").glob("sync_*.txt")):
        lines += [json.loads(l) for l in path.read_text().splitlines()]
    return lines


def pi_receive(sync, frags, packets):
    '''Mirror of main.py's packet handling for the packets a chunk produces.'''
    for payload in packets:
        if payload[:1] == b"~":
            payload = frags.feed(payload)
            if payload is None:
                continue
        data = json.loads(payload.decode("utf-8"))
        if data.get("t") == "se":
            sync.handle_end(data)
        elif sync.awaiting and "q" not in data:
            sync.collect(data)


def run_session(sync, sender, radio, frags, max_chunks=100):
    '''Request chunks until the session ends; returns the number of requests sent.'''
    requests = 0
    while requests < max_chunks:
        command = sync.next_command()
        if command is None:
            break
        sync.on_send(command)
        send_sync_chunk(sender, command)
        pi_receive(sync, frags, radio.take())
        requests += 1
        if sync.active is None:
            break
    return requests


# ── Fragmentation ────────────────────────────────────────────────────────────

def test_short_packet_is_not_fragmented():
    radio = FakeRadio()
    PacketSender(1, radio).send({"t": "batt", "v": 3.85, "soc": 72.0})
    assert len(radio.sent) == 1
    assert not radio.sent[0].startswith(b"~")


def test_long_packet_round_trips_through_fragments():
    radio = FakeRadio()
    data = json.dumps({"t": "x", "junk": "a" * 150}).encode()
    PacketSender(1, radio).send_raw(data)
    assert len(radio.sent) > 1

    frags = FragmentReassembler()
    results = [frags.feed(p) for p in radio.sent]
    assert results[:-1] == [None] * (len(results) - 1)
    assert results[-1] == data


def test_fragments_reassemble_out_of_order():
    radio = FakeRadio()
    data = b"z" * 130
    PacketSender(3, radio).send_raw(data)
    frags = FragmentReassembler()
    results = [frags.feed(p) for p in reversed(radio.sent)]
    assert results[-1] == data


def test_incomplete_message_is_dropped_after_max_age(monkeypatch):
    radio = FakeRadio(drop_at={1})
    PacketSender(1, radio).send_raw(b"y" * 130)
    frags = FragmentReassembler(max_age=15)
    for p in radio.sent:
        assert frags.feed(p) is None

    clock = [indoor.time.monotonic() + 60]
    monkeypatch.setattr(indoor.time, "monotonic", lambda: clock[0])
    # Any later fragment triggers cleanup of the stale one.
    other = FakeRadio()
    PacketSender(2, other).send_raw(b"w" * 130)
    frags.feed(other.sent[0])
    assert (1, 0) not in frags._pending


# ── Sync sessions ────────────────────────────────────────────────────────────

def test_full_sync_stores_every_line_once(sd):
    write_log(sd, 0, 55)
    radio = FakeRadio()
    sync = SyncManager([1], chunk_lines=20)
    run_session(sync, PacketSender(1, radio), radio, FragmentReassembler())

    assert [l["i"] for l in stored(sd)] == list(range(55))
    assert all(l["n"] == 1 for l in stored(sd))


def test_next_session_deletes_finished_file_and_picks_up_new_data(sd):
    write_log(sd, 0, 30)
    radio = FakeRadio()
    sender, frags = PacketSender(1, radio), FragmentReassembler()
    sync = SyncManager([1], chunk_lines=20)
    run_session(sync, sender, radio, frags)

    write_log(sd, 30, 10)
    sync.request_now(1)
    run_session(sync, sender, radio, frags)

    assert [l["i"] for l in stored(sd)] == list(range(40))


def test_dropped_line_triggers_chunk_retry_without_duplicates(sd):
    write_log(sd, 0, 25)
    # Every log line takes 2 packets; drop the 2nd fragment of the 3rd line.
    radio = FakeRadio(drop_at={5})
    sync = SyncManager([1], chunk_lines=20)
    run_session(sync, PacketSender(1, radio), radio, FragmentReassembler())

    assert [l["i"] for l in stored(sd)] == list(range(25))


def test_lost_se_and_resend_does_not_duplicate(sd):
    write_log(sd, 0, 30)
    radio = FakeRadio()
    sender, frags = PacketSender(1, radio), FragmentReassembler()
    sync = SyncManager([1], chunk_lines=20)

    # First chunk goes out but its "se" never reaches the Pi...
    command = sync.next_command()
    sync.on_send(command)
    send_sync_chunk(sender, command)
    pi_receive(sync, frags, radio.take()[:-1])
    assert sync.awaiting

    # ...so CommandManager resends the same request 10s later.
    sync.on_send(command)
    send_sync_chunk(sender, command)
    pi_receive(sync, frags, radio.take())
    run_session(sync, sender, radio, frags)

    assert [l["i"] for l in stored(sd)] == list(range(30))


def test_stale_retry_after_rotation_does_not_skip_new_file(sd):
    write_log(sd, 0, 20)
    radio = FakeRadio()
    sender, frags = PacketSender(1, radio), FragmentReassembler()
    sync = SyncManager([1], chunk_lines=20)
    run_session(sync, sender, radio, frags)          # all 20 lines stored

    write_log(sd, 20, 20)
    sync.request_now(1)
    command = sync.next_command()                    # confirms the end of generation 1
    sync.on_send(command)
    send_sync_chunk(sender, command)                 # node rotates to generation 2...
    radio.take()                                     # ...but the whole reply is lost

    # The retry still carries generation 1's end offset. It must not be
    # applied to generation 2 — the node should resend from the start.
    sync.on_send(command)
    send_sync_chunk(sender, command)
    pi_receive(sync, frags, radio.take())
    run_session(sync, sender, radio, frags)

    assert [l["i"] for l in stored(sd)] == list(range(40))


def test_session_stops_at_line_cap_and_resumes_next_hour(sd):
    write_log(sd, 0, 100)
    radio = FakeRadio()
    sender, frags = PacketSender(1, radio), FragmentReassembler()
    sync = SyncManager([1], lines_per_session=40, chunk_lines=20)

    run_session(sync, sender, radio, frags)
    assert len(stored(sd)) == 40
    assert sync.next_command() is None               # already synced this hour

    sync._last_hour.clear()                          # next hour
    run_session(sync, sender, radio, frags)
    assert [l["i"] for l in stored(sd)] == list(range(80))


def test_pi_restart_resumes_from_node_cursor(sd):
    write_log(sd, 0, 60)
    radio = FakeRadio()
    sender, frags = PacketSender(1, radio), FragmentReassembler()
    run_session(SyncManager([1], lines_per_session=40, chunk_lines=20), sender, radio, frags)

    # A fresh SyncManager has no cursor, so the node resumes from the last
    # offset it was told about — at most one chunk is sent twice.
    run_session(SyncManager([1], chunk_lines=20), sender, radio, frags)
    ids = [l["i"] for l in stored(sd)]
    assert sorted(set(ids)) == list(range(60))
    assert len(ids) - len(set(ids)) <= 20


def test_nothing_logged_ends_session_cleanly(sd):
    radio = FakeRadio()
    sync = SyncManager([1])
    requests = run_session(sync, PacketSender(1, radio), radio, FragmentReassembler())
    assert requests == 1
    assert sync.active is None
    assert stored(sd) == []


def test_timed_out_chunk_aborts_session_until_next_hour(sd):
    sync = SyncManager([1])
    assert sync.next_command() is not None
    sync.abort()
    assert sync.active is None
    assert sync.next_command() is None
