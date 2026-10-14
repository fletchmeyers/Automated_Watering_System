# tests/software_tests/test_usb_sync.py
"""
USB sync end to end: the Pi's usb_sync.py talking to the Pico's real
SerialLink, dispatch_command() and send_sync_chunk() through a fake serial
port, plus how the radio side steps aside while it runs.
"""
import json
import os
from unittest.mock import MagicMock

import pytest

import communication_garden as garden
import communication_indoor as indoor
import sync_indoor
import usb_sync
from communication_garden import PacketSender, SerialLink, send_sync_chunk, send_storage_info
from communication_indoor import SyncManager
from sync_garden import dispatch_command


@pytest.fixture
def sd(tmp_path, monkeypatch):
    monkeypatch.setattr(garden, "SD_DATA_FILE", str(tmp_path / "data.txt"))
    monkeypatch.setattr(garden, "SD_SENDING_FILE", str(tmp_path / "sending.txt"))
    monkeypatch.setattr(garden, "SD_CURSOR_FILE", str(tmp_path / "sync_cursor.txt"))
    archive = tmp_path / "archive"
    archive.mkdir()
    monkeypatch.setattr(indoor, "ARCHIVE_DIR", archive)
    return tmp_path


def write_log(sd, start, count):
    with open(sd / "data.txt", "a") as f:
        for i in range(start, start + count):
            f.write(json.dumps({"t": "rt", "tmp": 20.0, "i": i,
                                "ts": "2026-10-04T12:00:00"}, separators=(",", ":")) + "\n")


def stored(sd):
    lines = []
    for path in sorted((sd / "archive").glob("sync_*.txt")):
        lines += [json.loads(l) for l in path.read_text().splitlines()]
    return lines


class PicoOnUsb:
    '''A serial port with the Pico's real code on the other end.'''

    class _Data:
        '''The Pico's usb_cdc.data: bytes the Pi wrote, waiting to be read.'''
        def __init__(self):
            self.inbox = b""

        @property
        def in_waiting(self):
            return len(self.inbox)

        def read(self, n):
            out, self.inbox = self.inbox[:n], self.inbox[n:]
            return out

        def write(self, data):
            self.outbox.append(data)

    def __init__(self, node_id=1, noise=False):
        self.data = self._Data()
        self.replies = []
        self.data.outbox = self.replies
        self.link = SerialLink(self.data)
        self.sender = PacketSender(node_id, self.link)
        self.node_id = node_id
        self.noise = noise
        self.commands = []

    # Pi side of the port
    def write(self, data):
        self.data.inbox += data
        command = self.link.receive()          # what code.py does each loop
        if command is None:
            return
        command.setdefault("n", self.node_id)
        self.commands.append(command)
        if self.noise:                         # the M0 prints debug lines on the same port
            self.replies.append(b"[SYNC] Sent 8 lines (gen 1, offset 512/9000).\n")
        dispatch_command(command, self.sender, MagicMock(), MagicMock(),
                         lambda: "2026-10-04T12:00:00", MagicMock(), send_sync_chunk,
                         self.node_id, send_storage_info)

    def readline(self):
        return self.replies.pop(0) if self.replies else b""

    def reset_input_buffer(self):
        self.replies.clear()

    def close(self):
        pass


def test_usb_sync_pulls_everything_in_big_chunks_with_no_pauses(sd, monkeypatch):
    sleeps = []
    monkeypatch.setattr(garden.time, "sleep", sleeps.append)
    write_log(sd, 0, 450)
    pico = PicoOnUsb(noise=True)

    count = usb_sync.sync_node(usb_sync.UsbNode(pico), 1, db_conn=None, chunk_lines=200)

    assert count == 450
    assert [l["i"] for l in stored(sd)] == list(range(450))
    assert not any("j" in l for l in stored(sd))
    assert all(l["n"] == 1 for l in stored(sd))
    assert [c["k"] for c in pico.commands] == [200, 200, 200, 0]   # last one confirms
    assert sleeps == []                                            # no radio-style gaps
    assert not any(r.startswith(b"~") for r in pico.replies)       # no fragments

    # The node's cursor was confirmed to the end, so nothing gets sent twice.
    assert usb_sync.sync_node(usb_sync.UsbNode(pico), 1, db_conn=None) == 0
    assert len(stored(sd)) == 450


def test_find_node_answers_info_and_skips_debug_lines(sd, monkeypatch):
    pico = PicoOnUsb(node_id=2, noise=True)
    monkeypatch.setattr(usb_sync, "open_port", lambda path: pico)
    node, info = usb_sync.find_node(["/dev/ttyACM0"])
    assert node is not None
    assert info["t"] == "info" and info["n"] == 2


def test_stale_radio_offset_never_rewinds_the_node(sd, monkeypatch):
    monkeypatch.setattr(garden.time, "sleep", lambda s: None)
    write_log(sd, 0, 20)

    class Radio:
        def __init__(self):
            self.sent = []

        def send(self, data):
            self.sent.append(data)

    radio = Radio()
    sender = PacketSender(1, radio)

    send_sync_chunk(sender, {"t": "sync", "k": 8})                 # radio got lines 0-7...
    send_sync_chunk(sender, {"t": "sync", "k": 8, "g": 1, "o": 0}) # (resend, same offset)
    usb_sync.sync_node(usb_sync.UsbNode(PicoOnUsb()), 1, db_conn=None)   # ...then USB took the rest

    radio.sent.clear()
    send_sync_chunk(sender, {"t": "sync", "k": 8, "g": 1, "o": 0}) # a stale radio retry
    sent = [json.loads(p) for p in radio.sent]
    assert [p["t"] for p in sent] == ["se"]                        # nothing resent
    assert sent[0]["c"] == 0


def test_radio_sync_steps_aside_for_a_usb_sync(sd):
    sync = SyncManager([1, 2], chunk_lines=8)
    assert sync.next_command()["n"] == 1
    sync._cursors[1] = {"g": 3, "o": 800}
    sync.release(1)
    assert 1 not in sync.sessions and sync.active is None and not sync.awaiting
    assert 1 not in sync._cursors                     # next radio request lets the node choose
    assert sync.next_command(lambda n: n != 1)["n"] == 2


def test_usb_sync_flag_is_ignored_once_its_process_is_gone(tmp_path, monkeypatch):
    flag = tmp_path / "usb.json"
    monkeypatch.setattr(sync_indoor, "USB_SYNC_FILE", str(flag))
    assert sync_indoor.usb_sync_node() is None
    flag.write_text(json.dumps({"n": 2, "pid": os.getpid()}))
    assert sync_indoor.usb_sync_node() == 2
    flag.write_text(json.dumps({"n": 2, "pid": 2 ** 22 + 12345}))   # no such process
    assert sync_indoor.usb_sync_node() is None


def test_lines_cut_off_by_the_read_timeout_are_joined_back_up(sd, monkeypatch):
    monkeypatch.setattr(garden.time, "sleep", lambda s: None)
    write_log(sd, 0, 30)

    class SlowPico(PicoOnUsb):
        '''pyserial's readline() returns whatever has arrived when its timeout
        hits — here, every line comes back in two halves.'''
        def readline(self):
            if not self.replies:
                return b""
            line = self.replies.pop(0)
            if len(line) > 4 and line.endswith(b"\n"):
                self.replies.insert(0, line[len(line) // 2:])
                return line[:len(line) // 2]
            return line

    count = usb_sync.sync_node(usb_sync.UsbNode(SlowPico()), 1, db_conn=None, chunk_lines=200)
    assert count == 30
    assert [l["i"] for l in stored(sd)] == list(range(30))


def test_find_node_waits_for_a_board_that_reset_when_its_port_opened(monkeypatch):
    '''A USB-serial board (ESP32 V2) reboots as its port opens and misses the
    first "info"; find_node keeps asking on the same port for such boards.'''
    class Rebooting:
        def __init__(self):
            self.replies, self.asked = [], 0
        def reset_input_buffer(self):
            pass
        def write(self, data):
            self.asked += 1
            if self.asked > 1:                       # the first one arrived mid-boot
                self.replies.append(b'{"t":"info","q":1,"n":14}\n')
        def readline(self):
            return self.replies.pop(0) if self.replies else b""
        def close(self):
            pass

    board = Rebooting()
    monkeypatch.setattr(usb_sync, "open_port", lambda path: board)
    monkeypatch.setattr(usb_sync, "resets_on_open", lambda path: True)
    real_ask = usb_sync.UsbNode.ask
    monkeypatch.setattr(usb_sync.UsbNode, "ask",
                        lambda self, cmd, kind, timeout=1: real_ask(self, cmd, kind, timeout=0.2))
    node, info = usb_sync.find_node(["/dev/ttyACM0"], quiet=True)
    assert info["n"] == 14 and board.asked == 2


# â”€â”€ The garden-usb watcher â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class FakePort:
    def __init__(self, path):
        self.port = path
        self.closed = False

    def close(self):
        self.closed = True


class Bench:
    '''Boards plugged into the Pi, a clock, and what the watcher did.'''

    def __init__(self, tmp_path, monkeypatch, info=None):
        monkeypatch.setattr(usb_sync, "USB_SYNC_FILE", str(tmp_path / "usb.json"))
        monkeypatch.setattr(usb_sync, "NODE_SETUP_FILE", str(tmp_path / "setup.json"))
        self.tmp_path = tmp_path
        self.boards = {}
        self.now = 0.0
        self.info = info          # what a board answers ("info"), or None: no answer
        self.asked = []
        self.pulled = []
        self.watcher = usb_sync.Watcher(devices=lambda: dict(self.boards), find=self.find,
                                        pull=self.pull, clock=lambda: self.now)

    def find(self, ports, quiet=False):
        self.asked.append(ports)
        # While it's asked, the watcher holds the USB for itself.
        assert json.loads((self.tmp_path / "usb.json").read_text())["pid"] == os.getpid()
        if self.info is None:
            return None, None
        return usb_sync.UsbNode(FakePort(ports[0])), dict(self.info)

    def pull(self, node, info):
        self.pulled.append(info["n"])

    def at(self, seconds):
        self.now = seconds
        self.watcher.step()


def test_a_board_plugged_in_is_asked_once_it_has_started_and_its_backlog_pulled(tmp_path, monkeypatch):
    bench = Bench(tmp_path, monkeypatch, info={"t": "info", "n": 1, "ub": 5000})
    bench.boards["pico"] = ["/dev/ttyACM1", "/dev/ttyACM0"]
    bench.at(0)
    bench.at(usb_sync.SETTLE - 1)
    assert bench.asked == []                                 # still starting up
    bench.at(usb_sync.SETTLE)
    assert bench.asked == [["/dev/ttyACM1", "/dev/ttyACM0"]]  # data port first
    assert bench.pulled == [1]
    assert not (tmp_path / "usb.json").exists()              # handed the USB back

    bench.at(100)
    bench.at(1000)
    assert bench.pulled == [1]                               # once per plug-in

    del bench.boards["pico"]                                 # unplugged...
    bench.at(1001)
    bench.boards["pico"] = ["/dev/ttyACM1", "/dev/ttyACM0"]  # ...and back
    bench.at(1002)
    bench.at(1002 + usb_sync.SETTLE)
    assert bench.pulled == [1, 1]


def test_nothing_waiting_means_nothing_pulled(tmp_path, monkeypatch):
    bench = Bench(tmp_path, monkeypatch, info={"t": "info", "n": 3, "ub": 0})
    bench.boards["esp32"] = ["/dev/ttyUSB0"]
    bench.at(0)
    bench.at(usb_sync.SETTLE)
    bench.at(usb_sync.SETTLE + 10)
    assert len(bench.asked) == 1 and bench.pulled == []


def test_a_board_that_never_answers_is_left_alone_after_a_while(tmp_path, monkeypatch):
    bench = Bench(tmp_path, monkeypatch, info=None)
    bench.boards["something"] = ["/dev/ttyACM0"]
    bench.at(0)
    for t in range(usb_sync.SETTLE, usb_sync.GIVE_UP + 1, 5):
        bench.at(t)
    asked = len(bench.asked)
    assert asked > 1                                         # kept trying while it might be booting
    bench.at(usb_sync.GIVE_UP + 100)
    assert len(bench.asked) == asked                         # then stopped


def test_the_watcher_waits_while_node_setup_has_the_usb(tmp_path, monkeypatch):
    bench = Bench(tmp_path, monkeypatch, info={"t": "info", "n": 1, "ub": 10})
    (tmp_path / "setup.json").write_text(json.dumps({"pid": os.getpid()}))
    bench.boards["pico"] = ["/dev/ttyACM1"]
    bench.at(0)
    bench.at(30)
    assert bench.asked == []
    (tmp_path / "setup.json").unlink()                       # node_setup.py finished
    bench.at(33)
    assert bench.pulled == [1]


def test_a_failed_pull_is_not_retried_until_the_board_is_plugged_in_again(tmp_path, monkeypatch, capsys):
    bench = Bench(tmp_path, monkeypatch, info={"t": "info", "n": 1, "ub": 10})

    def broken(node, info):
        raise RuntimeError("node stopped answering over USB")
    bench.watcher._pull = broken
    bench.boards["pico"] = ["/dev/ttyACM1"]
    bench.at(0)
    bench.at(usb_sync.SETTLE)
    bench.at(usb_sync.SETTLE + 10)
    assert len(bench.asked) == 1
    assert "failed: node stopped answering" in capsys.readouterr().out
    assert not (tmp_path / "usb.json").exists()


def test_node_setup_waits_for_a_pull_then_holds_the_usb(tmp_path, monkeypatch):
    import node_setup
    monkeypatch.setattr(node_setup, "USB_SYNC_FILE", str(tmp_path / "usb.json"))
    monkeypatch.setattr(node_setup, "NODE_SETUP_FILE", str(tmp_path / "setup.json"))
    (tmp_path / "usb.json").write_text(json.dumps({"n": 1, "pid": os.getpid()}))
    waits = []

    def pull_finishes(seconds):
        waits.append(seconds)
        (tmp_path / "usb.json").unlink()
    monkeypatch.setattr(node_setup.time, "sleep", pull_finishes)

    with node_setup.usb_turn():
        assert waits                                         # it waited for the pull
        assert json.loads((tmp_path / "setup.json").read_text())["pid"] == os.getpid()
    assert not (tmp_path / "setup.json").exists()
