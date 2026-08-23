"""Unit tests for the in-memory streams registry (no HTTP, no subprocess)."""

from __future__ import annotations

import asyncio

import pytest

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
