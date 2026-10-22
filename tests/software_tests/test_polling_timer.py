# tests/software_tests/test_polling_timer.py
"""Which node the radio loop polls next (communication_indoor.PollingTimer)."""
import communication_indoor
from communication_indoor import PollingTimer


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_nodes_that_dont_answer_cant_keep_another_waiting(monkeypatch):
    '''Nodes 1 and 2 aren't answering: each poll holds the radio for a 45 s
    timeout. Node 3 still gets its turn, rather than waiting until they've
    been given up on.'''
    clock = Clock()
    monkeypatch.setattr(communication_indoor.time, "monotonic", clock)
    timer = PollingTimer([1, 2, 3], poll_interval=60)
    clock.t += 60
    polled = []
    for _ in range(6):
        due = timer.due_nodes()
        node = due[0]
        polled.append(node)
        timer.mark_polled(node)
        clock.t += 45 if node in (1, 2) else 2   # a timeout, or a quick answer
    assert 3 in polled[:3]
    assert polled.count(3) >= 2


def test_the_longest_waiting_goes_first(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(communication_indoor.time, "monotonic", clock)
    timer = PollingTimer([1, 2, 3], poll_interval=60)
    for node, ago in ((1, 61), (2, 90), (3, 75)):
        timer._last_poll[node] = clock.t - ago
    assert timer.due_nodes() == [2, 3, 1]
    timer._last_poll[2] = clock.t   # just polled: not due
    assert timer.due_nodes() == [3, 1]
