"""Unit tests for the session-runtime observation helpers.

Covers the pitfall-table promises that do not need a tmux server
(session-runtime.md §9): the shared dialog budget, "unknown is not dead"
cache semantics, and the fenced-kill primitives in proctable.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys

import pytest

from src.sessions import proctable
from src.sessions.dialogs import DialogBudget, run_dialog_dismissal
from src.sessions.provider import DialogRule
from src.sessions.state_cache import TmuxStateCache, TmuxUnavailable

# ---------------------------------------------------------------------------
# dialogs
# ---------------------------------------------------------------------------


def _rule(name, pattern, keys=("Enter",), **kw):
    return DialogRule(name=name, pattern=pattern, keys=keys, **kw)


class _Screen:
    """Scripted pane: each capture pops the next frame; sends are recorded."""

    def __init__(self, frames: list[str]):
        self.frames = frames
        self.sent: list[tuple[str, ...]] = []

    async def capture(self) -> str:
        return self.frames.pop(0) if len(self.frames) > 1 else self.frames[0]

    async def send(self, keys: tuple[str, ...]) -> None:
        self.sent.append(keys)


class TestDialogDismissal:
    async def test_matches_and_answers_until_quiet(self):
        screen = _Screen(["Do you trust this folder?", "❯ "])
        outcome = await run_dialog_dismissal(
            capture=screen.capture,
            send_keys=screen.send,
            dialogs=(_rule("trust", "Do you trust"),),
            budget=DialogBudget(5.0),
            fired=set(),
        )
        assert outcome.fired == ["trust"]
        assert screen.sent == [("Enter",)]
        assert not outcome.budget_exhausted

    async def test_quarantine_rule_terminates_the_pass(self):
        screen = _Screen(["You have hit your usage limit", "still there"])
        outcome = await run_dialog_dismissal(
            capture=screen.capture,
            send_keys=screen.send,
            dialogs=(_rule("rate-limit", "usage limit", keys=("Escape",), quarantine=True),),
            budget=DialogBudget(5.0),
            fired=set(),
        )
        assert outcome.quarantined is not None
        assert outcome.quarantined.name == "rate-limit"

    async def test_once_rules_fire_at_most_once_across_passes(self):
        fired: set[str] = set()
        for _ in range(2):
            screen = _Screen(["Welcome to the harness!", "❯ "])
            await run_dialog_dismissal(
                capture=screen.capture,
                send_keys=screen.send,
                dialogs=(_rule("welcome", "Welcome", once=True),),
                budget=DialogBudget(5.0),
                fired=fired,
            )
        assert fired == {"welcome"}

    async def test_shared_budget_stops_a_looping_dialog(self):
        # A dialog that never goes away must not loop past the *shared*
        # budget — per-dialog budgets were the Gas City failure mode.
        screen = _Screen(["stuck dialog"])
        outcome = await run_dialog_dismissal(
            capture=screen.capture,
            send_keys=screen.send,
            dialogs=(_rule("stuck", "stuck", once=False),),
            budget=DialogBudget(0.5),
            fired=set(),
        )
        assert outcome.budget_exhausted

    async def test_invalid_regex_is_skipped_not_fatal(self):
        screen = _Screen(["anything"])
        outcome = await run_dialog_dismissal(
            capture=screen.capture,
            send_keys=screen.send,
            dialogs=(_rule("bad", "([", is_regex=True),),
            budget=DialogBudget(1.0),
            fired=set(),
        )
        assert outcome.fired == []


# ---------------------------------------------------------------------------
# state cache
# ---------------------------------------------------------------------------


class _Runner:
    def __init__(self):
        self.calls = 0
        self.output = "s-a\t0\t100\n"
        self.error: Exception | None = None

    async def __call__(self, *args, **kwargs) -> str:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.output


class TestStateCache:
    async def test_one_list_panes_per_ttl_window(self):
        runner = _Runner()
        cache = TmuxStateCache(runner, ttl=60.0)
        await cache.panes()
        await cache.panes()
        assert runner.calls == 1
        cache.invalidate()
        await cache.panes()
        assert runner.calls == 2

    async def test_fresh_boot_no_server_is_empty(self):
        runner = _Runner()
        runner.error = RuntimeError("no server running on /tmp/x")
        cache = TmuxStateCache(runner, ttl=60.0)
        assert await cache.panes() == {}

    async def test_server_loss_raises_with_last_known_good(self):
        runner = _Runner()
        cache = TmuxStateCache(runner, ttl=60.0)
        first = await cache.panes()
        assert set(first) == {"s-a"}
        cache.invalidate()
        runner.error = RuntimeError("no server running on /tmp/x")
        with pytest.raises(TmuxUnavailable) as excinfo:
            await cache.panes()
        assert set(excinfo.value.last_good) == {"s-a"}

    async def test_forget_then_no_server_is_authoritatively_empty(self):
        # exit-empty: we killed the last session ourselves, so the server
        # exiting is the expected end state, not an anomaly to defer on.
        runner = _Runner()
        cache = TmuxStateCache(runner, ttl=60.0)
        await cache.panes()
        cache.forget("s-a")
        runner.error = RuntimeError("no server running on /tmp/x")
        assert await cache.panes() == {}

    async def test_hang_with_no_last_good_still_defers(self):
        runner = _Runner()
        cache = TmuxStateCache(runner, ttl=60.0)
        await cache.panes()
        cache.forget("s-a")
        runner.error = RuntimeError("timed out after 30s")
        with pytest.raises(TmuxUnavailable):
            await cache.panes()

    async def test_dead_pane_is_reported_dead(self):
        runner = _Runner()
        runner.output = "s-a\t1\t100\n"
        cache = TmuxStateCache(runner, ttl=60.0)
        panes = await cache.panes()
        assert panes["s-a"].dead is True

    async def test_any_live_pane_keeps_a_split_session_alive(self):
        runner = _Runner()
        runner.output = "s-a\t1\t100\ns-a\t0\t101\n"
        cache = TmuxStateCache(runner, ttl=60.0)
        panes = await cache.panes()
        assert panes["s-a"].dead is False

    async def test_procs_returns_real_processes(self):
        cache = TmuxStateCache(_Runner(), ttl=60.0)
        procs = await cache.procs()
        assert procs is not None
        assert os.getpid() in procs

    async def test_descendants_walk_ppid_links(self):
        cache = TmuxStateCache(_Runner(), ttl=60.0)
        procs = await cache.procs()
        assert procs is not None
        me = procs[os.getpid()]
        subtree = cache.descendants(procs, me.ppid)
        assert any(p.pid == os.getpid() for p in subtree)


# ---------------------------------------------------------------------------
# proctable
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not os.path.isdir("/proc"), reason="requires /proc")
class TestProctable:
    async def test_read_start_ticks_of_a_live_process(self):
        ticks = await proctable.read_start_ticks(os.getpid())
        assert isinstance(ticks, int)

    async def test_read_start_ticks_of_a_gone_process(self):
        assert await proctable.read_start_ticks(-1) is None

    async def test_scan_by_env_marker_finds_a_marked_child(self):
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(20)"],
            env={**os.environ, "AQ_SESSION_ID": "sess-scan-test"},
        )
        try:
            await asyncio.sleep(0.2)
            entries = await proctable.scan_by_env_marker("AQ_SESSION_ID")
            mine = [e for e in entries if e.pid == child.pid]
            assert mine and mine[0].marker == "sess-scan-test"
        finally:
            child.kill()
            child.wait()

    async def test_kill_tree_terminates_parent_and_child(self):
        script = (
            "import subprocess, sys, time, os;"
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
            "print(p.pid, flush=True); time.sleep(60)"
        )
        parent = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            env={**os.environ, "AQ_INSTANCE_TOKEN": "tok-kill"},
        )
        child_pid = int(parent.stdout.readline())
        try:
            await proctable.kill_tree(parent.pid, instance_token="tok-kill", grace=1.0)
            parent.wait(timeout=5)
            await asyncio.sleep(0.2)
            assert await proctable.read_start_ticks(child_pid) is None
        finally:
            for pid in (parent.pid, child_pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            parent.wait()

    async def test_kill_tree_refuses_a_different_instance_token(self):
        victim = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(20)"],
            env={**os.environ, "AQ_INSTANCE_TOKEN": "tok-real"},
        )
        try:
            await proctable.kill_tree(victim.pid, instance_token="tok-stale", grace=0.2)
            assert victim.poll() is None  # still alive — fence held
        finally:
            victim.kill()
            victim.wait()

    async def test_fence_refuses_recycled_start_ticks(self):
        stat = proctable._read_stat_sync(os.getpid())
        assert stat is not None
        _, comm, start_ticks = stat
        recycled = proctable.ProcEntry(
            pid=os.getpid(), ppid=0, comm=comm, start_ticks=start_ticks + 999
        )
        assert proctable._fence_ok_sync(recycled, None) is False
