"""Unit tests for the in-memory streams registry (no HTTP, no subprocess)."""

from __future__ import annotations

from src.api.streams import ConsoleFrame, StreamRegistry


def test_create_assigns_stream_id_and_running_status():
    reg = StreamRegistry()
    handle = reg.create(
        title="Running pytest…", session_id="supervisor-demo",
        project_id="demo", command=["pytest", "tests/"], cwd="/tmp",
    )
    assert handle.stream_id
    assert handle.status == "running"
    assert reg.get(handle.stream_id) is handle


def test_concurrent_count_tracks_per_session():
    reg = StreamRegistry()
    reg.create(title="a", session_id="s1", project_id=None, command=["echo"], cwd="/tmp")
    reg.create(title="b", session_id="s1", project_id=None, command=["echo"], cwd="/tmp")
    reg.create(title="c", session_id="s2", project_id=None, command=["echo"], cwd="/tmp")
    assert reg.concurrent_count("s1") == 2
    assert reg.concurrent_count("s2") == 1
    assert reg.concurrent_count("s3") == 0


def test_finish_decrements_concurrent_count():
    reg = StreamRegistry()
    handle = reg.create(title="a", session_id="s1", project_id=None, command=["echo"], cwd="/tmp")
    reg.finish(handle)
    assert reg.concurrent_count("s1") == 0


def test_append_fans_out_to_subscribers_and_caps_buffer():
    reg = StreamRegistry(buffer_max_lines=2)
    handle = reg.create(title="a", session_id="s1", project_id=None, command=["echo"], cwd="/tmp")
    q = handle.subscribe()
    for i in range(3):
        handle.append(ConsoleFrame(seq=i, type="line", stream="stdout", text=str(i)))
    assert len(handle.buffer) == 2
    assert handle.truncated is True
    assert [f.seq for f in handle.buffer] == [1, 2]
    assert q.qsize() == 3  # subscriber queue is not capped by the ring buffer


def test_replay_from_returns_frames_after_seq():
    reg = StreamRegistry()
    handle = reg.create(title="a", session_id="s1", project_id=None, command=["echo"], cwd="/tmp")
    for i in range(5):
        handle.append(ConsoleFrame(seq=i, type="line", stream="stdout", text=str(i)))
    replayed = handle.replay_from(2)
    assert [f.seq for f in replayed] == [3, 4]


def test_evict_removes_from_registry():
    reg = StreamRegistry()
    handle = reg.create(title="a", session_id="s1", project_id=None, command=["echo"], cwd="/tmp")
    reg.evict(handle.stream_id)
    assert reg.get(handle.stream_id) is None


def test_all_finished_before_cutoff():
    reg = StreamRegistry()
    handle = reg.create(title="a", session_id="s1", project_id=None, command=["echo"], cwd="/tmp")
    handle.status = "exited"
    handle.ended_at = 100.0
    assert reg.all_finished_before(200.0) == [handle.stream_id]
    assert reg.all_finished_before(50.0) == []


def test_console_frame_to_dict_omits_none_fields():
    frame = ConsoleFrame(seq=1, type="line", stream="stdout", text="hi", ts=1.0)
    d = frame.to_dict()
    assert d == {"type": "line", "seq": 1, "ts": 1.0, "stream": "stdout", "text": "hi"}
    exit_frame = ConsoleFrame(seq=2, type="exit", rc=0, ts=2.0)
    assert exit_frame.to_dict() == {"type": "exit", "seq": 2, "ts": 2.0, "rc": 0}


def test_byte_cap_evicts_oldest_frames_and_marks_truncated():
    """``streams.buffer_max_bytes`` bounds memory independently of
    ``buffer_max_lines``: a few very long lines must not pin
    ``buffer_max_lines`` x line-length bytes."""
    reg = StreamRegistry(buffer_max_lines=5000, buffer_max_bytes=300)
    handle = reg.create(title="a", session_id="s1", project_id=None, command=["echo"], cwd="/tmp")
    for i in range(10):
        handle.append(ConsoleFrame(seq=i, type="line", stream="stdout", text="x" * 100))

    assert handle.buffer_bytes <= 300
    assert len(handle.buffer) == 3
    assert [f.seq for f in handle.buffer] == [7, 8, 9]
    assert handle.truncated is True
    # The line cap alone would have kept everything.
    assert handle.buffer.maxlen == 5000


def test_byte_cap_counts_utf8_bytes_not_characters():
    # 4 bytes per char in UTF-8: 20 chars = 80 bytes, so a 100-byte cap
    # holds one frame, not two.
    reg = StreamRegistry(buffer_max_lines=5000, buffer_max_bytes=100)
    handle = reg.create(title="a", session_id="s1", project_id=None, command=["echo"], cwd="/tmp")
    handle.append(ConsoleFrame(seq=0, type="line", stream="stdout", text="\U0001f600" * 20))
    assert handle.buffer_bytes == 80
    assert len(handle.buffer) == 1
    handle.append(ConsoleFrame(seq=1, type="line", stream="stdout", text="\U0001f600" * 20))
    assert len(handle.buffer) == 1
    assert [f.seq for f in handle.buffer] == [1]


def test_byte_cap_keeps_the_newest_frame_even_when_it_alone_exceeds_the_cap():
    """Dropping the newest frame would lose a terminal exit/killed frame and
    leave subscribers replaying a stream that never ends."""
    reg = StreamRegistry(buffer_max_lines=5000, buffer_max_bytes=64)
    handle = reg.create(title="a", session_id="s1", project_id=None, command=["echo"], cwd="/tmp")
    handle.append(ConsoleFrame(seq=0, type="line", stream="stdout", text="y" * 10_000))
    assert [f.seq for f in handle.buffer] == [0]

    handle.append(ConsoleFrame(seq=1, type="exit", rc=0))
    assert [f.seq for f in handle.buffer] == [1]
    assert handle.buffer[-1].type == "exit"


def test_byte_accounting_stays_correct_when_the_line_cap_also_evicts():
    """Both caps can fire on the same append; the byte counter must not drift."""
    reg = StreamRegistry(buffer_max_lines=3, buffer_max_bytes=10_000)
    handle = reg.create(title="a", session_id="s1", project_id=None, command=["echo"], cwd="/tmp")
    for i in range(10):
        handle.append(ConsoleFrame(seq=i, type="line", stream="stdout", text="z" * 50))
    assert len(handle.buffer) == 3
    assert handle.buffer_bytes == 150
    assert handle.truncated is True


def test_frames_without_text_cost_nothing_against_the_byte_cap():
    reg = StreamRegistry(buffer_max_lines=5000, buffer_max_bytes=10)
    handle = reg.create(title="a", session_id="s1", project_id=None, command=["echo"], cwd="/tmp")
    for i in range(5):
        handle.append(ConsoleFrame(seq=i, type="killed"))
    assert handle.buffer_bytes == 0
    assert len(handle.buffer) == 5
    assert handle.truncated is False


def test_create_stamps_the_registry_byte_cap_onto_the_handle():
    reg = StreamRegistry(buffer_max_lines=7, buffer_max_bytes=1234)
    handle = reg.create(title="a", session_id="s1", project_id=None, command=["echo"], cwd="/tmp")
    assert handle.buffer.maxlen == 7
    assert handle.buffer_max_bytes == 1234
