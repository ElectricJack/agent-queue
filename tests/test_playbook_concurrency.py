"""Tests for PlaybookManager concurrency limits (roadmap 5.3.5).

Covers the ``max_concurrent_playbook_runs`` feature per the playbooks spec
Section 6 (Execution Model, Concurrency):

  - Global concurrency cap limits total in-flight playbook runs
  - Multiple instances of the same playbook can run concurrently
  - Runs are rejected (not queued) when at capacity
  - Completed runs free concurrency slots (reaping)
  - A cap of 0 means unlimited
  - Concurrency is orthogonal to cooldown (both gates must pass)
  - Shutdown cancels all running tasks

Additionally tests the public API surface: can_start_run, register_run,
unregister_run, reap_completed_runs, running_count, running_runs,
get_runs_for_playbook, shutdown_runs, max_concurrent_runs property.
"""

from __future__ import annotations

import asyncio

import pytest

from src.playbook_manager import PlaybookManager
from src.playbook_models import CompiledPlaybook, PlaybookNode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_playbook(
    *,
    playbook_id: str = "test-playbook",
    version: int = 1,
    source_hash: str = "abc123def456",
    triggers: list[str] | None = None,
    scope: str = "system",
    cooldown_seconds: int | None = None,
) -> CompiledPlaybook:
    """Create a minimal valid CompiledPlaybook for testing."""
    return CompiledPlaybook(
        id=playbook_id,
        version=version,
        source_hash=source_hash,
        triggers=triggers or ["git.commit"],
        scope=scope,
        cooldown_seconds=cooldown_seconds,
        nodes={
            "start": PlaybookNode(
                entry=True,
                prompt="Do something.",
                goto="end",
            ),
            "end": PlaybookNode(terminal=True),
        },
    )


def _manager_with_playbooks(
    *playbooks: CompiledPlaybook,
    max_concurrent_runs: int = 2,
) -> PlaybookManager:
    """Create a PlaybookManager with pre-loaded playbooks (no disk/store)."""
    manager = PlaybookManager(max_concurrent_runs=max_concurrent_runs)
    for pb in playbooks:
        manager._active[pb.id] = pb
        manager._index_triggers(pb)
    return manager


def _make_long_running_task() -> asyncio.Task:
    """Create an asyncio task that runs indefinitely (until cancelled)."""

    async def _run_forever():
        await asyncio.sleep(3600)

    return asyncio.get_event_loop().create_task(_run_forever())


def _make_completed_task(result: str = "done") -> asyncio.Task:
    """Create an asyncio task that is already completed."""
    future = asyncio.get_event_loop().create_future()
    future.set_result(result)
    task = asyncio.ensure_future(future)
    return task


def _make_failed_task(error: str = "boom") -> asyncio.Task:
    """Create an asyncio task that has failed with an exception."""
    future = asyncio.get_event_loop().create_future()
    future.set_exception(RuntimeError(error))
    task = asyncio.ensure_future(future)
    return task


# ---------------------------------------------------------------------------
# Test: Constructor defaults
# ---------------------------------------------------------------------------


class TestConcurrencyDefaults:
    """Verify that the manager initialises concurrency state correctly."""

    def test_default_max_concurrent_runs(self) -> None:
        """Default cap is 2 (matches spec's hook engine default)."""
        manager = PlaybookManager()
        assert manager.max_concurrent_runs == 2

    def test_custom_max_concurrent_runs(self) -> None:
        """Cap can be set via constructor."""
        manager = PlaybookManager(max_concurrent_runs=5)
        assert manager.max_concurrent_runs == 5

    def test_unlimited_with_zero(self) -> None:
        """A cap of 0 means unlimited."""
        manager = PlaybookManager(max_concurrent_runs=0)
        assert manager.max_concurrent_runs == 0
        assert manager.can_start_run() is True

    def test_running_count_initially_zero(self) -> None:
        """No runs are in-flight at startup."""
        manager = PlaybookManager()
        assert manager.running_count == 0

    def test_running_runs_initially_empty(self) -> None:
        """Running runs dict is empty at startup."""
        manager = PlaybookManager()
        assert manager.running_runs == {}


# ---------------------------------------------------------------------------
# Test: can_start_run
# ---------------------------------------------------------------------------


class TestCanStartRun:
    """Tests for the concurrency gate check."""

    def test_can_start_when_empty(self) -> None:
        """Can start when no runs are in-flight."""
        manager = PlaybookManager(max_concurrent_runs=2)
        assert manager.can_start_run() is True

    @pytest.mark.asyncio
    async def test_can_start_when_below_cap(self) -> None:
        """Can start when below the concurrency cap."""
        manager = PlaybookManager(max_concurrent_runs=3)
        task = _make_long_running_task()
        try:
            manager.register_run("run-1", "playbook-a", task)
            assert manager.can_start_run() is True
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_cannot_start_at_cap(self) -> None:
        """Cannot start when at the concurrency cap."""
        manager = PlaybookManager(max_concurrent_runs=2)
        tasks = []
        try:
            for i in range(2):
                t = _make_long_running_task()
                tasks.append(t)
                manager.register_run(f"run-{i}", "playbook-a", t)
            assert manager.can_start_run() is False
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_unlimited_always_allows(self) -> None:
        """With cap=0 (unlimited), can always start."""
        manager = PlaybookManager(max_concurrent_runs=0)
        tasks = []
        try:
            for i in range(10):
                t = _make_long_running_task()
                tasks.append(t)
                assert manager.register_run(f"run-{i}", "pb", t) is True
            assert manager.can_start_run() is True
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


# ---------------------------------------------------------------------------
# Test: register_run
# ---------------------------------------------------------------------------


class TestRegisterRun:
    """Tests for registering playbook runs."""

    @pytest.mark.asyncio
    async def test_register_success(self) -> None:
        """register_run returns True and tracks the run."""
        manager = PlaybookManager(max_concurrent_runs=2)
        task = _make_long_running_task()
        try:
            result = manager.register_run("run-1", "playbook-a", task)
            assert result is True
            assert manager.running_count == 1
            assert "run-1" in manager.running_runs
            assert manager.running_runs["run-1"] == "playbook-a"
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_register_rejected_at_cap(self) -> None:
        """register_run returns False when at capacity."""
        manager = PlaybookManager(max_concurrent_runs=1)
        t1 = _make_long_running_task()
        t2 = _make_long_running_task()
        try:
            assert manager.register_run("run-1", "pb-a", t1) is True
            assert manager.register_run("run-2", "pb-b", t2) is False
            assert manager.running_count == 1
            assert "run-2" not in manager.running_runs
        finally:
            t1.cancel()
            t2.cancel()
            await asyncio.gather(t1, t2, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_multiple_instances_same_playbook(self) -> None:
        """Multiple instances of the same playbook can run concurrently."""
        manager = PlaybookManager(max_concurrent_runs=3)
        tasks = []
        try:
            for i in range(3):
                t = _make_long_running_task()
                tasks.append(t)
                assert manager.register_run(f"run-{i}", "same-playbook", t) is True
            assert manager.running_count == 3
            assert manager.get_runs_for_playbook("same-playbook") == [
                "run-0",
                "run-1",
                "run-2",
            ]
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


# ---------------------------------------------------------------------------
# Test: unregister_run
# ---------------------------------------------------------------------------


class TestUnregisterRun:
    """Tests for manual run unregistration."""

    @pytest.mark.asyncio
    async def test_unregister_frees_slot(self) -> None:
        """Unregistering a run frees a concurrency slot."""
        manager = PlaybookManager(max_concurrent_runs=1)
        task = _make_long_running_task()
        try:
            manager.register_run("run-1", "pb-a", task)
            assert manager.can_start_run() is False
            manager.unregister_run("run-1")
            assert manager.can_start_run() is True
            assert manager.running_count == 0
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def test_unregister_nonexistent_is_noop(self) -> None:
        """Unregistering a run that doesn't exist is a no-op."""
        manager = PlaybookManager()
        manager.unregister_run("nonexistent")  # Should not raise
        assert manager.running_count == 0


# ---------------------------------------------------------------------------
# Test: reap_completed_runs
# ---------------------------------------------------------------------------


class TestReapCompletedRuns:
    """Tests for the periodic reaping of completed asyncio tasks."""

    @pytest.mark.asyncio
    async def test_reap_completed_task(self) -> None:
        """Completed tasks are reaped and their slots freed."""
        manager = PlaybookManager(max_concurrent_runs=1)
        task = _make_completed_task()
        # Allow the task to complete
        await asyncio.sleep(0)
        manager._running["run-1"] = task
        manager._running_playbook_ids["run-1"] = "pb-a"

        reaped = manager.reap_completed_runs()
        assert reaped == ["run-1"]
        assert manager.running_count == 0
        assert manager.can_start_run() is True

    @pytest.mark.asyncio
    async def test_reap_failed_task(self) -> None:
        """Failed tasks are reaped and exceptions are logged (not raised)."""
        manager = PlaybookManager(max_concurrent_runs=1)
        task = _make_failed_task("something broke")
        await asyncio.sleep(0)
        manager._running["run-1"] = task
        manager._running_playbook_ids["run-1"] = "pb-a"

        # Should not raise — exceptions are logged
        reaped = manager.reap_completed_runs()
        assert reaped == ["run-1"]
        assert manager.running_count == 0

    @pytest.mark.asyncio
    async def test_reap_does_not_touch_running_tasks(self) -> None:
        """Running (not-done) tasks are not reaped."""
        manager = PlaybookManager(max_concurrent_runs=3)
        running_task = _make_long_running_task()
        completed_task = _make_completed_task()
        await asyncio.sleep(0)
        try:
            manager._running["run-1"] = running_task
            manager._running_playbook_ids["run-1"] = "pb-a"
            manager._running["run-2"] = completed_task
            manager._running_playbook_ids["run-2"] = "pb-b"

            reaped = manager.reap_completed_runs()
            assert reaped == ["run-2"]
            assert manager.running_count == 1
            assert "run-1" in manager.running_runs
        finally:
            running_task.cancel()
            try:
                await running_task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_reap_returns_empty_when_nothing_done(self) -> None:
        """Reap returns empty list when all tasks are still running."""
        manager = PlaybookManager(max_concurrent_runs=2)
        task = _make_long_running_task()
        try:
            manager.register_run("run-1", "pb-a", task)
            reaped = manager.reap_completed_runs()
            assert reaped == []
            assert manager.running_count == 1
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_reap_multiple_completed(self) -> None:
        """Multiple completed tasks are all reaped in one call."""
        manager = PlaybookManager(max_concurrent_runs=5)
        for i in range(3):
            task = _make_completed_task()
            await asyncio.sleep(0)
            manager._running[f"run-{i}"] = task
            manager._running_playbook_ids[f"run-{i}"] = "pb"

        reaped = manager.reap_completed_runs()
        assert len(reaped) == 3
        assert manager.running_count == 0


# ---------------------------------------------------------------------------
# Test: get_runs_for_playbook
# ---------------------------------------------------------------------------


class TestGetRunsForPlaybook:
    """Tests for querying in-flight runs by playbook ID."""

    @pytest.mark.asyncio
    async def test_returns_runs_for_specific_playbook(self) -> None:
        """Only returns runs belonging to the queried playbook."""
        manager = PlaybookManager(max_concurrent_runs=5)
        tasks = []
        try:
            for i in range(2):
                t = _make_long_running_task()
                tasks.append(t)
                manager.register_run(f"run-a-{i}", "playbook-alpha", t)
            t = _make_long_running_task()
            tasks.append(t)
            manager.register_run("run-b-0", "playbook-beta", t)

            assert manager.get_runs_for_playbook("playbook-alpha") == ["run-a-0", "run-a-1"]
            assert manager.get_runs_for_playbook("playbook-beta") == ["run-b-0"]
            assert manager.get_runs_for_playbook("nonexistent") == []
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


# ---------------------------------------------------------------------------
# Test: max_concurrent_runs property (getter/setter)
# ---------------------------------------------------------------------------


class TestMaxConcurrentRunsProperty:
    """Tests for runtime adjustment of the concurrency cap."""

    def test_setter_updates_cap(self) -> None:
        """Setting the property updates the internal cap."""
        manager = PlaybookManager(max_concurrent_runs=2)
        assert manager.max_concurrent_runs == 2
        manager.max_concurrent_runs = 5
        assert manager.max_concurrent_runs == 5

    @pytest.mark.asyncio
    async def test_raising_cap_allows_more_runs(self) -> None:
        """Raising the cap while at capacity allows new runs."""
        manager = PlaybookManager(max_concurrent_runs=1)
        t1 = _make_long_running_task()
        try:
            manager.register_run("run-1", "pb-a", t1)
            assert manager.can_start_run() is False

            manager.max_concurrent_runs = 2
            assert manager.can_start_run() is True
        finally:
            t1.cancel()
            try:
                await t1
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_lowering_cap_does_not_cancel_existing(self) -> None:
        """Lowering the cap does not cancel already-running tasks."""
        manager = PlaybookManager(max_concurrent_runs=3)
        tasks = []
        try:
            for i in range(3):
                t = _make_long_running_task()
                tasks.append(t)
                manager.register_run(f"run-{i}", "pb", t)

            # Lower cap below current running count
            manager.max_concurrent_runs = 1
            assert manager.running_count == 3  # Existing runs continue
            assert manager.can_start_run() is False  # But no new ones allowed
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


# ---------------------------------------------------------------------------
# Test: shutdown_runs
# ---------------------------------------------------------------------------


class TestShutdownRuns:
    """Tests for graceful shutdown of all running tasks."""

    @pytest.mark.asyncio
    async def test_shutdown_cancels_all_tasks(self) -> None:
        """shutdown_runs cancels all running tasks and clears tracking."""
        manager = PlaybookManager(max_concurrent_runs=5)
        tasks = []
        for i in range(3):
            t = _make_long_running_task()
            tasks.append(t)
            manager.register_run(f"run-{i}", "pb", t)

        assert manager.running_count == 3
        await manager.shutdown_runs()
        assert manager.running_count == 0
        assert manager.running_runs == {}
        # All tasks should be cancelled
        for t in tasks:
            assert t.done()

    @pytest.mark.asyncio
    async def test_shutdown_with_no_running_tasks(self) -> None:
        """shutdown_runs is safe to call with no running tasks."""
        manager = PlaybookManager()
        await manager.shutdown_runs()  # Should not raise
        assert manager.running_count == 0


# ---------------------------------------------------------------------------
# Test: Concurrency + cooldown interaction
# ---------------------------------------------------------------------------


class TestConcurrencyCooldownInteraction:
    """Verify that concurrency and cooldown are orthogonal gates."""

    @pytest.mark.asyncio
    async def test_cooldown_independent_of_concurrency(self) -> None:
        """A playbook on cooldown is skipped regardless of concurrency."""
        pb = _make_playbook(cooldown_seconds=60)
        manager = _manager_with_playbooks(pb, max_concurrent_runs=5)

        # Record execution to start cooldown
        manager.record_execution("test-playbook", "system")

        # Even with plenty of concurrency room, cooldown blocks
        assert manager.can_start_run() is True
        triggerable = manager.get_triggerable_playbooks("git.commit")
        assert len(triggerable) == 0

    @pytest.mark.asyncio
    async def test_concurrency_independent_of_cooldown(self) -> None:
        """Concurrency cap blocks even when no cooldown is active."""
        pb = _make_playbook(cooldown_seconds=None)
        manager = _manager_with_playbooks(pb, max_concurrent_runs=1)
        task = _make_long_running_task()
        try:
            manager.register_run("run-1", "test-playbook", task)

            # No cooldown active, but concurrency blocks
            assert manager.can_start_run() is False
            # get_triggerable_playbooks still returns playbooks (it doesn't check concurrency)
            triggerable = manager.get_triggerable_playbooks("git.commit")
            assert len(triggerable) == 1
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_both_gates_pass(self) -> None:
        """When both concurrency and cooldown pass, playbook is eligible."""
        pb = _make_playbook(cooldown_seconds=None)
        manager = _manager_with_playbooks(pb, max_concurrent_runs=2)

        assert manager.can_start_run() is True
        triggerable = manager.get_triggerable_playbooks("git.commit")
        assert len(triggerable) == 1

    @pytest.mark.asyncio
    async def test_both_gates_fail(self) -> None:
        """When both concurrency and cooldown fail, playbook is blocked."""
        pb = _make_playbook(cooldown_seconds=60)
        manager = _manager_with_playbooks(pb, max_concurrent_runs=1)

        # Start cooldown
        manager.record_execution("test-playbook", "system")
        # Fill concurrency
        task = _make_long_running_task()
        try:
            manager.register_run("run-1", "other-playbook", task)

            assert manager.can_start_run() is False
            triggerable = manager.get_triggerable_playbooks("git.commit")
            assert len(triggerable) == 0
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


# ---------------------------------------------------------------------------
# Test: Edge cases
# ---------------------------------------------------------------------------


class TestConcurrencyEdgeCases:
    """Edge cases and boundary conditions."""

    @pytest.mark.asyncio
    async def test_cap_of_one(self) -> None:
        """With cap=1, only one run at a time."""
        manager = PlaybookManager(max_concurrent_runs=1)
        t1 = _make_long_running_task()
        t2 = _make_long_running_task()
        try:
            assert manager.register_run("run-1", "pb", t1) is True
            assert manager.register_run("run-2", "pb", t2) is False
            assert manager.running_count == 1
        finally:
            t1.cancel()
            t2.cancel()
            await asyncio.gather(t1, t2, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_register_after_reap_succeeds(self) -> None:
        """After reaping, new runs can be registered."""
        manager = PlaybookManager(max_concurrent_runs=1)

        # Fill slot with a task that completes immediately
        task = _make_completed_task()
        await asyncio.sleep(0)
        manager._running["run-1"] = task
        manager._running_playbook_ids["run-1"] = "pb-a"

        # At cap before reaping
        assert manager.can_start_run() is False

        # Reap frees the slot
        manager.reap_completed_runs()
        assert manager.can_start_run() is True

        # Now we can register a new run
        t2 = _make_long_running_task()
        try:
            assert manager.register_run("run-2", "pb-b", t2) is True
            assert manager.running_count == 1
        finally:
            t2.cancel()
            try:
                await t2
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_running_runs_is_snapshot(self) -> None:
        """running_runs property returns a copy, not a live reference."""
        manager = PlaybookManager(max_concurrent_runs=5)
        task = _make_long_running_task()
        try:
            manager.register_run("run-1", "pb-a", task)
            snapshot = manager.running_runs
            manager.unregister_run("run-1")
            # Snapshot should still have the old data
            assert "run-1" in snapshot
            assert manager.running_count == 0
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_cancelled_task_reaped(self) -> None:
        """Cancelled tasks are reaped without raising."""
        manager = PlaybookManager(max_concurrent_runs=2)
        task = _make_long_running_task()
        manager._running["run-1"] = task
        manager._running_playbook_ids["run-1"] = "pb"
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        reaped = manager.reap_completed_runs()
        assert reaped == ["run-1"]
        assert manager.running_count == 0

    def test_negative_cap_treated_as_unlimited(self) -> None:
        """A negative max_concurrent_runs is treated as unlimited (same as 0)."""
        manager = PlaybookManager(max_concurrent_runs=-1)
        assert manager.can_start_run() is True
