"""PaneBroadcaster — fan-out, dedupe, linger, cap, error handling."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from src.sessions.fake import FakeProvider
from src.sessions.pane_broadcaster import PaneBroadcaster, PaneStreamRefused
from src.sessions.provider import SessionSpec


@dataclass
class Row:
    """Minimal stand-in for a SessionRecord."""

    name: str
    provider: str = "fake"
    instance_token: str = "tok"


class OneProvider:
    """Registry stub: always hands back the same provider instance."""

    def __init__(self, provider):
        self.provider = provider

    def create(self, name, config=None):
        return self.provider


@dataclass
class Sessions:
    pane_stream_interval_seconds: float = 0.01
    pane_stream_max_sessions: int = 2
    pane_stream_lines: int = 60


@dataclass
class Config:
    sessions: Sessions


async def _fake_with(name: str) -> FakeProvider:
    provider = FakeProvider(config=None)
    await provider.start(
        SessionSpec(session_name=name, work_dir="/w", command=("x",),
                     instance_token="tok")
    )
    return provider


def _bcast(provider, **overrides) -> PaneBroadcaster:
    return PaneBroadcaster(
        OneProvider(provider), Config(sessions=Sessions(**overrides))
    )


async def _next(queue: asyncio.Queue, timeout: float = 2.0) -> dict:
    return await asyncio.wait_for(queue.get(), timeout=timeout)


@pytest.mark.asyncio
async def test_first_frame_is_immediate():
    provider = await _fake_with("s1")
    provider.sessions["s1"].output.append("hello")
    b = _bcast(provider)
    q = await b.subscribe(Row("s1"))
    frame = await _next(q)
    assert frame["source"] == "pane"
    assert frame["type"] == "screen"
    assert "hello" in frame["screen"]
    await b.shutdown()


@pytest.mark.asyncio
async def test_two_subscribers_share_one_loop():
    provider = await _fake_with("s1")
    b = _bcast(provider)
    q1 = await b.subscribe(Row("s1"))
    q2 = await b.subscribe(Row("s1"))
    assert b.watched_count() == 1
    await _next(q1)
    await _next(q2)
    provider.sessions["s1"].output.append("later")
    f1 = await _next(q1)
    f2 = await _next(q2)
    assert "later" in f1["screen"]
    assert "later" in f2["screen"]
    await b.shutdown()


@pytest.mark.asyncio
async def test_unchanged_screen_emits_no_frame():
    provider = await _fake_with("s1")
    provider.sessions["s1"].output.append("static")
    b = _bcast(provider)
    q = await b.subscribe(Row("s1"))
    await _next(q)  # the immediate first frame
    with pytest.raises(asyncio.TimeoutError):
        await _next(q, timeout=0.2)
    await b.shutdown()


@pytest.mark.asyncio
async def test_seq_increases_per_frame():
    provider = await _fake_with("s1")
    b = _bcast(provider)
    q = await b.subscribe(Row("s1"))
    first = await _next(q)
    provider.sessions["s1"].output.append("change")
    second = await _next(q)
    assert second["seq"] > first["seq"]
    await b.shutdown()


@pytest.mark.asyncio
async def test_last_unsubscribe_stops_loop_after_linger():
    provider = await _fake_with("s1")
    b = _bcast(provider)
    b.linger_seconds = 0.05
    q = await b.subscribe(Row("s1"))
    await _next(q)
    await b.unsubscribe("s1", q)
    await asyncio.sleep(0.2)
    assert b.watched_count() == 0
    await b.shutdown()


@pytest.mark.asyncio
async def test_resubscribe_within_linger_reuses_loop():
    provider = await _fake_with("s1")
    b = _bcast(provider)
    b.linger_seconds = 1.0
    q1 = await b.subscribe(Row("s1"))
    await _next(q1)
    await b.unsubscribe("s1", q1)
    q2 = await b.subscribe(Row("s1"))
    assert b.watched_count() == 1
    await _next(q2)
    await b.shutdown()


@pytest.mark.asyncio
async def test_cap_refuses_extra_session():
    provider = await _fake_with("s1")
    await provider.start(
        SessionSpec(session_name="s2", work_dir="/w", command=("x",),
                     instance_token="tok")
    )
    await provider.start(
        SessionSpec(session_name="s3", work_dir="/w", command=("x",),
                     instance_token="tok")
    )
    b = _bcast(provider, pane_stream_max_sessions=2)
    await b.subscribe(Row("s1"))
    await b.subscribe(Row("s2"))
    with pytest.raises(PaneStreamRefused):
        await b.subscribe(Row("s3"))
    await b.shutdown()


@pytest.mark.asyncio
async def test_stopped_session_emits_stopped_frame_and_ends():
    provider = await _fake_with("s1")
    b = _bcast(provider)
    q = await b.subscribe(Row("s1"))
    await _next(q)
    await provider.stop(provider.sessions["s1"].handle)
    frame = await _next(q)
    while frame["type"] == "screen":
        frame = await _next(q)
    assert frame["type"] == "stopped"
    await asyncio.sleep(0.1)
    assert b.watched_count() == 0
    await b.shutdown()


@pytest.mark.asyncio
async def test_repeated_peek_errors_emit_one_error_frame_and_stop():
    provider = await _fake_with("s1")

    async def boom(*_a, **_k):
        raise RuntimeError("tmux is gone")

    b = _bcast(provider)
    q = await b.subscribe(Row("s1"))
    await _next(q)
    provider.peek = boom
    frame = await _next(q)
    assert frame["type"] == "error"
    assert "tmux is gone" in frame["message"]
    await asyncio.sleep(0.1)
    assert b.watched_count() == 0
    await b.shutdown()


@pytest.mark.asyncio
async def test_subscribe_after_shutdown_is_refused():
    provider = await _fake_with("s1")
    b = _bcast(provider)
    q = await b.subscribe(Row("s1"))
    await _next(q)
    await b.shutdown()
    assert b.watched_count() == 0
    with pytest.raises(PaneStreamRefused):
        await b.subscribe(Row("s1"))
    assert b.watched_count() == 0


@pytest.mark.asyncio
async def test_liveness_uses_process_alive_not_is_running():
    """The loop must ask "is the *process* alive", not "does a pane exist".

    Regression guard: ``TmuxProvider`` sets ``remain-on-exit on``, so
    ``is_running`` stays True for a finished agent forever.  Pinned here on
    a provider whose two answers disagree the way tmux's do.
    """
    provider = await _fake_with("s1")

    class RemainOnExit:
        """A pane that outlives its process, exactly like tmux's."""

        name = "remain"
        capabilities = provider.capabilities

        def supports(self, cap):
            return provider.supports(cap)

        async def peek(self, *a, **k):
            return "frozen screen"

        async def is_running(self, h):
            return True  # pane still listed — this is the trap

        async def process_alive(self, h, process_names=()):
            return False

    b = _bcast(RemainOnExit())
    q = await b.subscribe(Row("s1"))
    frame = await _next(q)
    while frame["type"] == "screen":
        frame = await _next(q)
    assert frame["type"] == "stopped"
    await b.shutdown()


@pytest.mark.asyncio
async def test_failing_liveness_probe_keeps_session_alive_and_warns(caplog):
    """Unknown is not dead — but it must not be invisible either."""
    provider = await _fake_with("s1")

    async def boom(*_a, **_k):
        raise RuntimeError("ps exploded")

    provider.process_alive = boom
    b = _bcast(provider)
    with caplog.at_level("WARNING", logger="src.sessions.pane_broadcaster"):
        q = await b.subscribe(Row("s1"))
        await _next(q)
        await asyncio.sleep(0.1)
        assert b.watched_count() == 1  # never reported stopped
    assert any("liveness probe failing" in r.message for r in caplog.records)
    await b.shutdown()


@pytest.mark.asyncio
async def test_full_queue_drops_the_oldest_frame_not_the_newest():
    """Every frame is a full snapshot, so the stale end is the droppable end."""
    provider = await _fake_with("s1")
    b = _bcast(provider)
    q = await b.subscribe(Row("s1"))
    await _next(q)
    watch = b._watches["s1"]
    # Saturate this subscriber's queue with numbered filler.
    n = 0
    while True:
        try:
            q.put_nowait({"filler": n})
        except asyncio.QueueFull:
            break
        n += 1
    b._emit(watch, {"type": "stopped"})
    frames = []
    while not q.empty():
        frames.append(q.get_nowait())
    assert frames[-1].get("type") == "stopped"  # the newest frame survived
    assert frames[0] == {"filler": 1}  # ...at the cost of the oldest
    await b.shutdown()


@pytest.mark.asyncio
async def test_detach_is_synchronous_and_cannot_be_interrupted():
    """Teardown must not depend on an await completing (see finding 4)."""
    import inspect

    provider = await _fake_with("s1")
    b = _bcast(provider)
    b.linger_seconds = 0.05
    q = await b.subscribe(Row("s1"))
    await _next(q)
    assert not inspect.iscoroutinefunction(b.detach)
    b.detach("s1", q)
    assert b._watches["s1"].subscribers == set()
    await asyncio.sleep(0.2)
    assert b.watched_count() == 0
    await b.shutdown()


@pytest.mark.asyncio
async def test_detach_works_while_another_coroutine_holds_the_lock():
    """Teardown must not queue behind the lock — that is where it gets cancelled."""
    provider = await _fake_with("s1")
    b = _bcast(provider)
    q = await b.subscribe(Row("s1"))
    await _next(q)
    async with b._lock:  # contended, as two tiles unmounting at once would be
        b.detach("s1", q)
        assert b._watches["s1"].subscribers == set()
    await b.shutdown()
