import json
import threading
import time

import pytest

from app.services import LocalQueue, Queue
from tests.helpers import FakeClock


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def q(tmp_path, clock) -> LocalQueue:
    return LocalQueue(tmp_path / "q" / "queue.sqlite3", visibility_timeout=30, max_receive_count=3, clock=clock)


def test_implements_interface(q):
    assert isinstance(q, Queue)
    assert q.max_receive_count == 3


def test_enqueue_then_receive(q):
    mid = q.enqueue(json.dumps({"a": 1}))
    [msg] = q.receive()
    assert msg.message_id == mid
    assert json.loads(msg.body) == {"a": 1}
    assert msg.receive_count == 1
    assert msg.receipt_handle


def test_receive_on_empty_queue_returns_nothing(q):
    assert q.receive() == []


def test_received_message_is_hidden_while_in_flight(q, clock):
    q.enqueue("m")
    assert len(q.receive()) == 1
    assert q.receive() == []
    clock.advance(29)
    assert q.receive() == []


def test_fifo_order(q):
    for i in range(5):
        q.enqueue(str(i))
    assert [m.body for m in q.receive(max_messages=5)] == ["0", "1", "2", "3", "4"]


def test_delete_acknowledges(q, clock):
    q.enqueue("m")
    [msg] = q.receive()
    assert q.delete(msg.receipt_handle) is True
    clock.advance(1000)
    assert q.receive() == []
    assert q.stats().visible == q.stats().in_flight == q.stats().dead == 0


def test_delete_unknown_or_repeated_handle_returns_false(q):
    q.enqueue("m")
    [msg] = q.receive()
    assert q.delete("nope") is False
    assert q.delete(msg.receipt_handle) is True
    assert q.delete(msg.receipt_handle) is False


def test_unacknowledged_message_is_redelivered_after_visibility_timeout(q, clock):
    q.enqueue("m")
    [first] = q.receive()
    clock.advance(31)
    [second] = q.receive()
    assert second.message_id == first.message_id
    assert second.receive_count == 2
    assert second.receipt_handle != first.receipt_handle


def test_stale_receipt_handle_cannot_acknowledge_after_redelivery(q, clock):
    q.enqueue("m")
    [first] = q.receive()
    clock.advance(31)
    [second] = q.receive()
    assert q.delete(first.receipt_handle) is False
    assert q.delete(second.receipt_handle) is True


def test_custom_visibility_timeout_per_receive(q, clock):
    q.enqueue("m")
    q.receive(visibility_timeout=5)
    clock.advance(6)
    assert len(q.receive()) == 1


def test_change_visibility_can_release_or_delay(q, clock):
    q.enqueue("m")
    [msg] = q.receive()
    assert q.change_visibility(msg.receipt_handle, 0) is True
    [again] = q.receive()
    assert again.receive_count == 2
    assert q.change_visibility(again.receipt_handle, 100) is True
    clock.advance(99)
    assert q.receive() == []
    clock.advance(2)
    assert len(q.receive()) == 1
    assert q.change_visibility("unknown", 1) is False


def test_message_is_dead_lettered_after_max_receives(q, clock):
    q.enqueue("poison")
    for expected in (1, 2, 3):
        [msg] = q.receive()
        assert msg.receive_count == expected
        clock.advance(31)
    assert q.receive() == []
    assert q.stats().dead == 1
    assert q.stats().visible == 0


def test_exhausted_message_counts_as_dead_even_before_the_next_receive(q, clock):
    q.enqueue("poison")
    for _ in range(3):
        [msg] = q.receive()
        q.change_visibility(msg.receipt_handle, 0)
    assert q.stats().dead == 1 and q.stats().visible == 0


def test_stats_distinguish_visible_in_flight_and_dead(q):
    for i in range(3):
        q.enqueue(str(i))
    q.receive()
    s = q.stats()
    assert (s.visible, s.in_flight, s.dead) == (2, 1, 0)


def test_long_poll_returns_as_soon_as_a_message_arrives(tmp_path):
    q = LocalQueue(tmp_path / "q.sqlite3", poll_interval=0.02)
    threading.Timer(0.2, q.enqueue, args=("late",)).start()
    started = time.monotonic()
    [msg] = q.receive(wait_seconds=5)
    assert msg.body == "late"
    assert time.monotonic() - started < 2


def test_long_poll_times_out_with_empty_result(tmp_path):
    q = LocalQueue(tmp_path / "q.sqlite3", poll_interval=0.02)
    started = time.monotonic()
    assert q.receive(wait_seconds=0.25) == []
    assert 0.2 <= time.monotonic() - started < 2


def test_long_poll_is_interrupted_by_stop_event(tmp_path):
    q = LocalQueue(tmp_path / "q.sqlite3", poll_interval=0.02)
    stop = threading.Event()
    threading.Timer(0.15, stop.set).start()
    started = time.monotonic()
    assert q.receive(wait_seconds=30, stop_event=stop) == []
    assert time.monotonic() - started < 3


def test_two_handles_on_the_same_file_share_messages(tmp_path):
    a = LocalQueue(tmp_path / "shared.sqlite3")
    b = LocalQueue(tmp_path / "shared.sqlite3")
    a.enqueue("from-a")
    [msg] = b.receive()
    assert msg.body == "from-a"
    assert a.receive() == []  # already in flight for b


def test_concurrent_consumers_never_receive_the_same_message(tmp_path):
    q = LocalQueue(tmp_path / "q.sqlite3", visibility_timeout=60)
    total = 60
    for i in range(total):
        q.enqueue(str(i))
    got: list[str] = []
    lock = threading.Lock()

    def consume():
        consumer = LocalQueue(tmp_path / "q.sqlite3", visibility_timeout=60)
        while True:
            msgs = consumer.receive()
            if not msgs:
                return
            with lock:
                got.extend(m.body for m in msgs)

    threads = [threading.Thread(target=consume) for _ in range(6)]
    [t.start() for t in threads]
    [t.join(timeout=30) for t in threads]
    assert sorted(got, key=int) == [str(i) for i in range(total)]


def test_argument_validation(tmp_path, q):
    with pytest.raises(ValueError):
        LocalQueue(tmp_path / "x.sqlite3", visibility_timeout=0)
    with pytest.raises(ValueError):
        LocalQueue(tmp_path / "x.sqlite3", max_receive_count=0)
    with pytest.raises(ValueError):
        q.receive(max_messages=0)
    with pytest.raises(TypeError):
        q.enqueue({"not": "a str"})  # type: ignore[arg-type]
