"""Tests for PlaybookManager cooldown tracking (roadmap 5.3.9).

Covers the seven mandatory test cases from roadmap 5.3.9 for playbook
cooldown per the playbooks spec Section 6 (Execution Model, Concurrency):

  (a) Playbook with 60s cooldown ignores trigger within 60s of completion
  (b) Same playbook triggers normally after cooldown expires
  (c) Cooldown is per-playbook — different playbooks are independent
  (d) Cooldown is tracked per scope — project cooldown doesn't block system
  (e) Failed runs still apply cooldown (prevents error loops)
  (f) Cooldown of 0 means no cooldown (every event triggers)
  (g) Concurrent events during cooldown are dropped (not queued)

Additionally tests the public API surface: is_on_cooldown, get_cooldown_remaining,
record_execution, clear_cooldown, get_triggerable_playbooks.  Edge cases,
boundary conditions, and lifecycle scenarios round out the coverage.
"""

from __future__ import annotations

import time

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


def _manager_with_playbooks(*playbooks: CompiledPlaybook) -> PlaybookManager:
    """Create a PlaybookManager with pre-loaded playbooks (no disk/store)."""
    manager = PlaybookManager()
    for pb in playbooks:
        manager._active[pb.id] = pb
        manager._index_triggers(pb)
    return manager


# ---------------------------------------------------------------------------
# Test: Core cooldown behaviour (roadmap 5.3.9 cases a-g)
# ---------------------------------------------------------------------------


class TestCooldownBasic:
    """Core cooldown behaviour — the seven test cases from roadmap 5.3.9."""

    def test_a_playbook_on_cooldown_ignores_trigger(self) -> None:
        """(a) Playbook with 60s cooldown ignores trigger event within 60s."""
        pb = _make_playbook(cooldown_seconds=60)
        manager = _manager_with_playbooks(pb)

        # Record an execution "just now"
        now = time.monotonic()
        manager.record_execution("test-playbook", "system", _clock=now)

        # Within 60s window — should be on cooldown
        assert manager.is_on_cooldown("test-playbook", "system") is True
        assert manager.get_cooldown_remaining("test-playbook", "system") > 0.0

        # Trigger event within cooldown window is ignored
        result = manager.get_triggerable_playbooks("git.commit", "system")
        assert result == [], "Trigger event within cooldown window must be ignored"

    def test_b_playbook_triggers_after_cooldown_expires(self) -> None:
        """(b) Same playbook triggers normally after cooldown expires."""
        pb = _make_playbook(cooldown_seconds=60)
        manager = _manager_with_playbooks(pb)

        # Record execution 61 seconds ago (simulated)
        now = time.monotonic()
        manager.record_execution("test-playbook", "system", _clock=now - 61)

        # Cooldown expired — should NOT be on cooldown
        assert manager.is_on_cooldown("test-playbook", "system") is False
        assert manager.get_cooldown_remaining("test-playbook", "system") == 0.0

        # Trigger event after cooldown fires normally
        result = manager.get_triggerable_playbooks("git.commit", "system")
        assert len(result) == 1
        assert result[0].id == "test-playbook"

    def test_c_cooldown_per_playbook_independent(self) -> None:
        """(c) Cooldown is per-playbook — different playbooks are independent."""
        pb1 = _make_playbook(
            playbook_id="pb-alpha",
            triggers=["git.commit"],
            cooldown_seconds=60,
        )
        pb2 = _make_playbook(
            playbook_id="pb-beta",
            triggers=["git.commit"],
            cooldown_seconds=60,
        )
        manager = _manager_with_playbooks(pb1, pb2)

        # Only pb-alpha has run
        now = time.monotonic()
        manager.record_execution("pb-alpha", "system", _clock=now)

        # pb-alpha on cooldown, pb-beta is NOT
        assert manager.is_on_cooldown("pb-alpha", "system") is True
        assert manager.is_on_cooldown("pb-beta", "system") is False

        # Same trigger event → only pb-beta fires (pb-alpha is independent, blocked)
        result = manager.get_triggerable_playbooks("git.commit", "system")
        assert len(result) == 1
        assert result[0].id == "pb-beta"

    def test_d_cooldown_tracked_per_scope(self) -> None:
        """(d) Cooldown is per-scope — project cooldown doesn't block system."""
        pb = _make_playbook(cooldown_seconds=60)
        manager = _manager_with_playbooks(pb)

        now = time.monotonic()
        # Record execution in project scope only
        manager.record_execution("test-playbook", "project", _clock=now)

        # Project scope is on cooldown
        assert manager.is_on_cooldown("test-playbook", "project") is True
        # System scope is NOT on cooldown
        assert manager.is_on_cooldown("test-playbook", "system") is False

        # Project-scoped trigger blocked, system-scoped trigger allowed
        assert manager.get_triggerable_playbooks("git.commit", "project") == []
        result = manager.get_triggerable_playbooks("git.commit", "system")
        assert len(result) == 1
        assert result[0].id == "test-playbook"

    def test_e_failed_run_applies_cooldown(self) -> None:
        """(e) Failed runs still apply cooldown (prevents error loops).

        record_execution does not distinguish between success and failure —
        any completed execution applies cooldown.  The caller is responsible
        for calling record_execution for both outcomes.
        """
        pb = _make_playbook(cooldown_seconds=60)
        manager = _manager_with_playbooks(pb)

        # Simulate failed run — caller still records execution
        now = time.monotonic()
        manager.record_execution("test-playbook", "system", _clock=now)

        # On cooldown regardless of success/failure
        assert manager.is_on_cooldown("test-playbook", "system") is True

    def test_f_cooldown_of_zero_means_no_cooldown(self) -> None:
        """(f) Cooldown of 0 means no cooldown — every event triggers."""
        pb = _make_playbook(cooldown_seconds=0)
        manager = _manager_with_playbooks(pb)

        # Record execution
        now = time.monotonic()
        manager.record_execution("test-playbook", "system", _clock=now)

        # Should NOT be on cooldown despite recent execution
        assert manager.is_on_cooldown("test-playbook", "system") is False
        assert manager.get_cooldown_remaining("test-playbook", "system") == 0.0

        # Trigger fires immediately despite just-completed execution
        result = manager.get_triggerable_playbooks("git.commit", "system")
        assert len(result) == 1, "cooldown_seconds=0 must allow every event to trigger"

    def test_g_concurrent_events_during_cooldown_dropped(self) -> None:
        """(g) Concurrent events during cooldown are dropped (not queued).

        get_triggerable_playbooks filters out playbooks on cooldown,
        effectively dropping the event for that playbook.
        """
        pb = _make_playbook(cooldown_seconds=60)
        manager = _manager_with_playbooks(pb)

        # Record execution
        now = time.monotonic()
        manager.record_execution("test-playbook", "system", _clock=now)

        # get_triggerable_playbooks returns empty — event is dropped
        result = manager.get_triggerable_playbooks("git.commit", "system")
        assert result == []

        # Directly matching playbooks still exist (not removed, just filtered)
        all_matching = manager.get_playbooks_by_trigger("git.commit")
        assert len(all_matching) == 1
        assert all_matching[0].id == "test-playbook"


# ---------------------------------------------------------------------------
# Test: is_on_cooldown edge cases
# ---------------------------------------------------------------------------


class TestIsOnCooldown:
    """Edge cases for the is_on_cooldown method."""

    def test_no_cooldown_configured(self) -> None:
        """Playbook with cooldown_seconds=None is never on cooldown."""
        pb = _make_playbook(cooldown_seconds=None)
        manager = _manager_with_playbooks(pb)

        manager.record_execution("test-playbook", "system")
        assert manager.is_on_cooldown("test-playbook", "system") is False

    def test_unknown_playbook(self) -> None:
        """Querying a non-existent playbook returns not on cooldown."""
        manager = PlaybookManager()
        assert manager.is_on_cooldown("nonexistent", "system") is False

    def test_no_prior_execution(self) -> None:
        """Playbook with cooldown but no prior execution is not on cooldown."""
        pb = _make_playbook(cooldown_seconds=60)
        manager = _manager_with_playbooks(pb)

        assert manager.is_on_cooldown("test-playbook", "system") is False


# ---------------------------------------------------------------------------
# Test: get_cooldown_remaining
# ---------------------------------------------------------------------------


class TestGetCooldownRemaining:
    """Tests for get_cooldown_remaining precision and edge cases."""

    def test_returns_zero_for_no_cooldown(self) -> None:
        """Returns 0.0 when playbook has no cooldown configured."""
        pb = _make_playbook(cooldown_seconds=None)
        manager = _manager_with_playbooks(pb)
        assert manager.get_cooldown_remaining("test-playbook", "system") == 0.0

    def test_returns_zero_for_expired(self) -> None:
        """Returns 0.0 when cooldown has expired."""
        pb = _make_playbook(cooldown_seconds=10)
        manager = _manager_with_playbooks(pb)

        now = time.monotonic()
        manager.record_execution("test-playbook", "system", _clock=now - 15)

        assert manager.get_cooldown_remaining("test-playbook", "system") == 0.0

    def test_returns_positive_during_cooldown(self) -> None:
        """Returns positive seconds remaining during active cooldown."""
        pb = _make_playbook(cooldown_seconds=60)
        manager = _manager_with_playbooks(pb)

        now = time.monotonic()
        manager.record_execution("test-playbook", "system", _clock=now - 10)

        remaining = manager.get_cooldown_remaining("test-playbook", "system")
        # Should be approximately 50 seconds remaining (60 - 10)
        assert 45.0 < remaining <= 50.0

    def test_returns_zero_for_unknown_playbook(self) -> None:
        """Returns 0.0 for a playbook not in the registry."""
        manager = PlaybookManager()
        assert manager.get_cooldown_remaining("nonexistent", "system") == 0.0

    def test_never_returns_negative(self) -> None:
        """Remaining time is clamped to 0.0, never negative."""
        pb = _make_playbook(cooldown_seconds=5)
        manager = _manager_with_playbooks(pb)

        # Far in the past
        now = time.monotonic()
        manager.record_execution("test-playbook", "system", _clock=now - 1000)

        assert manager.get_cooldown_remaining("test-playbook", "system") == 0.0


# ---------------------------------------------------------------------------
# Test: record_execution
# ---------------------------------------------------------------------------


class TestRecordExecution:
    """Tests for recording execution timestamps."""

    def test_basic_recording(self) -> None:
        """record_execution stores the timestamp for cooldown checks."""
        pb = _make_playbook(cooldown_seconds=60)
        manager = _manager_with_playbooks(pb)

        assert manager.is_on_cooldown("test-playbook", "system") is False
        manager.record_execution("test-playbook", "system")
        assert manager.is_on_cooldown("test-playbook", "system") is True

    def test_recording_updates_timestamp(self) -> None:
        """Subsequent executions update the cooldown start time."""
        pb = _make_playbook(cooldown_seconds=60)
        manager = _manager_with_playbooks(pb)

        now = time.monotonic()
        # First execution 50 seconds ago
        manager.record_execution("test-playbook", "system", _clock=now - 50)

        remaining_before = manager.get_cooldown_remaining("test-playbook", "system")
        assert 5.0 < remaining_before <= 10.0  # ~10s remaining

        # New execution resets cooldown
        manager.record_execution("test-playbook", "system", _clock=now)
        remaining_after = manager.get_cooldown_remaining("test-playbook", "system")
        assert remaining_after > 55.0  # Full ~60s cooldown again

    def test_different_scopes_tracked_independently(self) -> None:
        """Executions in different scopes create independent cooldown entries."""
        pb = _make_playbook(cooldown_seconds=60)
        manager = _manager_with_playbooks(pb)

        now = time.monotonic()
        manager.record_execution("test-playbook", "system", _clock=now)
        manager.record_execution("test-playbook", "project", _clock=now - 55)

        # System: full cooldown
        assert manager.get_cooldown_remaining("test-playbook", "system") > 55.0
        # Project: almost expired (~5s left)
        assert manager.get_cooldown_remaining("test-playbook", "project") <= 5.0


# ---------------------------------------------------------------------------
# Test: clear_cooldown
# ---------------------------------------------------------------------------


class TestClearCooldown:
    """Tests for clearing cooldown state."""

    def test_clear_specific_scope(self) -> None:
        """clear_cooldown with scope clears only that scope."""
        pb = _make_playbook(cooldown_seconds=60)
        manager = _manager_with_playbooks(pb)

        now = time.monotonic()
        manager.record_execution("test-playbook", "system", _clock=now)
        manager.record_execution("test-playbook", "project", _clock=now)

        # Clear only system scope
        manager.clear_cooldown("test-playbook", scope="system")

        assert manager.is_on_cooldown("test-playbook", "system") is False
        assert manager.is_on_cooldown("test-playbook", "project") is True

    def test_clear_all_scopes(self) -> None:
        """clear_cooldown without scope clears all scopes for that playbook."""
        pb = _make_playbook(cooldown_seconds=60)
        manager = _manager_with_playbooks(pb)

        now = time.monotonic()
        manager.record_execution("test-playbook", "system", _clock=now)
        manager.record_execution("test-playbook", "project", _clock=now)
        manager.record_execution("test-playbook", "agent-type:coding", _clock=now)

        # Clear all scopes
        manager.clear_cooldown("test-playbook")

        assert manager.is_on_cooldown("test-playbook", "system") is False
        assert manager.is_on_cooldown("test-playbook", "project") is False
        assert manager.is_on_cooldown("test-playbook", "agent-type:coding") is False

    def test_clear_does_not_affect_other_playbooks(self) -> None:
        """Clearing one playbook's cooldown doesn't affect others."""
        pb1 = _make_playbook(playbook_id="pb-1", cooldown_seconds=60)
        pb2 = _make_playbook(playbook_id="pb-2", cooldown_seconds=60)
        manager = _manager_with_playbooks(pb1, pb2)

        now = time.monotonic()
        manager.record_execution("pb-1", "system", _clock=now)
        manager.record_execution("pb-2", "system", _clock=now)

        manager.clear_cooldown("pb-1")

        assert manager.is_on_cooldown("pb-1", "system") is False
        assert manager.is_on_cooldown("pb-2", "system") is True

    def test_clear_nonexistent_is_noop(self) -> None:
        """Clearing cooldown for a non-existent playbook doesn't raise."""
        manager = PlaybookManager()
        # Should not raise
        manager.clear_cooldown("nonexistent")
        manager.clear_cooldown("nonexistent", scope="system")


# ---------------------------------------------------------------------------
# Test: get_triggerable_playbooks
# ---------------------------------------------------------------------------


class TestGetTriggerablePlaybooks:
    """Tests for the combined trigger + cooldown filtering method."""

    def test_returns_all_when_no_cooldown(self) -> None:
        """All matching playbooks returned when none are on cooldown."""
        pb1 = _make_playbook(playbook_id="pb-1", triggers=["git.commit"], cooldown_seconds=60)
        pb2 = _make_playbook(playbook_id="pb-2", triggers=["git.commit"], cooldown_seconds=60)
        manager = _manager_with_playbooks(pb1, pb2)

        result = manager.get_triggerable_playbooks("git.commit", "system")
        assert len(result) == 2
        ids = {pb.id for pb in result}
        assert ids == {"pb-1", "pb-2"}

    def test_filters_out_cooldown_playbooks(self) -> None:
        """Only playbooks not on cooldown are returned."""
        pb1 = _make_playbook(playbook_id="pb-1", triggers=["git.commit"], cooldown_seconds=60)
        pb2 = _make_playbook(playbook_id="pb-2", triggers=["git.commit"], cooldown_seconds=60)
        manager = _manager_with_playbooks(pb1, pb2)

        # Only pb-1 is on cooldown
        now = time.monotonic()
        manager.record_execution("pb-1", "system", _clock=now)

        result = manager.get_triggerable_playbooks("git.commit", "system")
        assert len(result) == 1
        assert result[0].id == "pb-2"

    def test_no_trigger_match_returns_empty(self) -> None:
        """Non-matching trigger returns empty list."""
        pb = _make_playbook(triggers=["git.commit"], cooldown_seconds=60)
        manager = _manager_with_playbooks(pb)

        result = manager.get_triggerable_playbooks("task.completed", "system")
        assert result == []

    def test_all_on_cooldown_returns_empty(self) -> None:
        """When all matching playbooks are on cooldown, returns empty."""
        pb1 = _make_playbook(playbook_id="pb-1", triggers=["git.commit"], cooldown_seconds=60)
        pb2 = _make_playbook(playbook_id="pb-2", triggers=["git.commit"], cooldown_seconds=60)
        manager = _manager_with_playbooks(pb1, pb2)

        now = time.monotonic()
        manager.record_execution("pb-1", "system", _clock=now)
        manager.record_execution("pb-2", "system", _clock=now)

        result = manager.get_triggerable_playbooks("git.commit", "system")
        assert result == []

    def test_scope_specific_filtering(self) -> None:
        """Cooldown check uses the scope parameter."""
        pb = _make_playbook(
            triggers=["git.commit"],
            cooldown_seconds=60,
        )
        manager = _manager_with_playbooks(pb)

        now = time.monotonic()
        manager.record_execution("test-playbook", "project", _clock=now)

        # Project scope — on cooldown, filtered out
        assert manager.get_triggerable_playbooks("git.commit", "project") == []
        # System scope — not on cooldown, returned
        result = manager.get_triggerable_playbooks("git.commit", "system")
        assert len(result) == 1
        assert result[0].id == "test-playbook"

    def test_no_cooldown_configured_always_triggers(self) -> None:
        """Playbooks without cooldown_seconds are always returned."""
        pb = _make_playbook(
            triggers=["git.commit"],
            cooldown_seconds=None,
        )
        manager = _manager_with_playbooks(pb)

        # Even after recording an execution, no cooldown blocks it
        manager.record_execution("test-playbook", "system")

        result = manager.get_triggerable_playbooks("git.commit", "system")
        assert len(result) == 1

    def test_mixed_cooldown_and_no_cooldown(self) -> None:
        """Mix of playbooks with and without cooldown configured."""
        pb_with = _make_playbook(
            playbook_id="pb-with-cd",
            triggers=["git.commit"],
            cooldown_seconds=60,
        )
        pb_without = _make_playbook(
            playbook_id="pb-no-cd",
            triggers=["git.commit"],
            cooldown_seconds=None,
        )
        manager = _manager_with_playbooks(pb_with, pb_without)

        now = time.monotonic()
        manager.record_execution("pb-with-cd", "system", _clock=now)
        manager.record_execution("pb-no-cd", "system", _clock=now)

        result = manager.get_triggerable_playbooks("git.commit", "system")
        assert len(result) == 1
        assert result[0].id == "pb-no-cd"


# ---------------------------------------------------------------------------
# Test: Cooldown cleanup on playbook removal
# ---------------------------------------------------------------------------


class TestCooldownCleanupOnRemoval:
    """Test that cooldown state is cleaned up when a playbook is removed."""

    @pytest.mark.asyncio
    async def test_remove_playbook_clears_cooldown(self) -> None:
        """Removing a playbook clears its cooldown across all scopes."""
        pb = _make_playbook(cooldown_seconds=60)
        manager = _manager_with_playbooks(pb)

        now = time.monotonic()
        manager.record_execution("test-playbook", "system", _clock=now)
        manager.record_execution("test-playbook", "project", _clock=now)

        assert ("test-playbook", "system") in manager._last_execution
        assert ("test-playbook", "project") in manager._last_execution

        await manager.remove_playbook("test-playbook")

        assert ("test-playbook", "system") not in manager._last_execution
        assert ("test-playbook", "project") not in manager._last_execution


# ---------------------------------------------------------------------------
# Test: Agent-type scope cooldowns
# ---------------------------------------------------------------------------


class TestAgentTypeScopeCooldown:
    """Cooldown tracking with agent-type scopes."""

    def test_different_agent_types_independent(self) -> None:
        """Cooldown for agent-type:coding doesn't block agent-type:review."""
        pb = _make_playbook(cooldown_seconds=60)
        manager = _manager_with_playbooks(pb)

        now = time.monotonic()
        manager.record_execution("test-playbook", "agent-type:coding", _clock=now)

        assert manager.is_on_cooldown("test-playbook", "agent-type:coding") is True
        assert manager.is_on_cooldown("test-playbook", "agent-type:review") is False

    def test_agent_type_independent_from_system(self) -> None:
        """Agent-type scope cooldown is independent from system scope."""
        pb = _make_playbook(cooldown_seconds=60)
        manager = _manager_with_playbooks(pb)

        now = time.monotonic()
        manager.record_execution("test-playbook", "agent-type:coding", _clock=now)

        assert manager.is_on_cooldown("test-playbook", "agent-type:coding") is True
        assert manager.is_on_cooldown("test-playbook", "system") is False


# ---------------------------------------------------------------------------
# Test: Boundary conditions
# ---------------------------------------------------------------------------


class TestCooldownBoundary:
    """Tests for edge/boundary conditions in cooldown timing."""

    def test_exact_cooldown_boundary_is_expired(self) -> None:
        """At exactly cooldown_seconds elapsed, cooldown has expired.

        The implementation uses strict ``> 0.0`` for ``is_on_cooldown``,
        so ``remaining == 0.0`` (at the boundary) means expired.
        """
        pb = _make_playbook(cooldown_seconds=60)
        manager = _manager_with_playbooks(pb)

        now = time.monotonic()
        manager.record_execution("test-playbook", "system", _clock=now - 60)

        assert manager.is_on_cooldown("test-playbook", "system") is False
        assert manager.get_cooldown_remaining("test-playbook", "system") == 0.0

        # Trigger fires at the exact boundary
        result = manager.get_triggerable_playbooks("git.commit", "system")
        assert len(result) == 1

    def test_one_second_before_expiry(self) -> None:
        """One second before cooldown expires, still blocked."""
        pb = _make_playbook(cooldown_seconds=60)
        manager = _manager_with_playbooks(pb)

        now = time.monotonic()
        manager.record_execution("test-playbook", "system", _clock=now - 59)

        assert manager.is_on_cooldown("test-playbook", "system") is True
        remaining = manager.get_cooldown_remaining("test-playbook", "system")
        assert 0.0 < remaining <= 1.0

    def test_very_small_cooldown(self) -> None:
        """Cooldown of 1 second works correctly."""
        pb = _make_playbook(cooldown_seconds=1)
        manager = _manager_with_playbooks(pb)

        now = time.monotonic()
        # Execution 0.5s ago — within cooldown
        manager.record_execution("test-playbook", "system", _clock=now - 0.5)
        assert manager.is_on_cooldown("test-playbook", "system") is True

        # Execution 2s ago — past cooldown
        manager.record_execution("test-playbook", "system", _clock=now - 2)
        assert manager.is_on_cooldown("test-playbook", "system") is False

    def test_large_cooldown(self) -> None:
        """Large cooldown values (e.g. 1 hour) work correctly."""
        pb = _make_playbook(cooldown_seconds=3600)
        manager = _manager_with_playbooks(pb)

        now = time.monotonic()
        manager.record_execution("test-playbook", "system", _clock=now - 1800)

        # 30 minutes in — still on cooldown
        assert manager.is_on_cooldown("test-playbook", "system") is True
        remaining = manager.get_cooldown_remaining("test-playbook", "system")
        assert 1795.0 < remaining <= 1800.0


# ---------------------------------------------------------------------------
# Test: Full lifecycle scenario
# ---------------------------------------------------------------------------


class TestCooldownLifecycle:
    """End-to-end lifecycle test exercising the full cooldown flow."""

    def test_full_trigger_execute_cooldown_retrigger_cycle(self) -> None:
        """Full lifecycle: trigger → execute → cooldown → drop → expire → trigger.

        Walks through the complete sequence described in the spec's
        Concurrency section and roadmap cases (a), (b), and (g) combined.
        """
        pb = _make_playbook(cooldown_seconds=60, triggers=["git.commit"])
        manager = _manager_with_playbooks(pb)

        now = time.monotonic()

        # 1. First event arrives — playbook is triggerable (no prior execution)
        result = manager.get_triggerable_playbooks("git.commit", "system")
        assert len(result) == 1, "First event should trigger the playbook"
        assert result[0].id == "test-playbook"

        # 2. Execution completes — record it
        manager.record_execution("test-playbook", "system", _clock=now)

        # 3. Second event arrives within cooldown — dropped (case g)
        result = manager.get_triggerable_playbooks("git.commit", "system")
        assert result == [], "Event during cooldown must be dropped"

        # 4. Third event also within cooldown — also dropped (case a)
        result = manager.get_triggerable_playbooks("git.commit", "system")
        assert result == [], "Subsequent event still within cooldown must be dropped"

        # 5. Simulate cooldown expiry (re-record execution 61s ago)
        manager.record_execution("test-playbook", "system", _clock=now - 61)

        # 6. Event after cooldown — triggers normally (case b)
        result = manager.get_triggerable_playbooks("git.commit", "system")
        assert len(result) == 1, "Event after cooldown should trigger the playbook"
        assert result[0].id == "test-playbook"

    def test_multiple_playbooks_mixed_cooldown_lifecycle(self) -> None:
        """Multiple playbooks with different cooldowns on the same trigger.

        Verifies that cooldown is truly per-playbook: one playbook can be
        on cooldown while another (with a shorter cooldown or no execution)
        continues to trigger from the same event type.
        """
        pb_short = _make_playbook(
            playbook_id="pb-short",
            triggers=["git.commit"],
            cooldown_seconds=10,
        )
        pb_long = _make_playbook(
            playbook_id="pb-long",
            triggers=["git.commit"],
            cooldown_seconds=120,
        )
        pb_none = _make_playbook(
            playbook_id="pb-none",
            triggers=["git.commit"],
            cooldown_seconds=None,
        )
        manager = _manager_with_playbooks(pb_short, pb_long, pb_none)

        now = time.monotonic()

        # All three trigger initially
        result = manager.get_triggerable_playbooks("git.commit", "system")
        assert len(result) == 3

        # Execute all three
        manager.record_execution("pb-short", "system", _clock=now - 15)  # 15s ago
        manager.record_execution("pb-long", "system", _clock=now - 15)  # 15s ago
        manager.record_execution("pb-none", "system", _clock=now)

        # pb-short: 10s cooldown expired (15s ago) → triggers
        # pb-long: 120s cooldown active (15s ago) → blocked
        # pb-none: no cooldown → triggers
        result = manager.get_triggerable_playbooks("git.commit", "system")
        ids = {pb.id for pb in result}
        assert ids == {"pb-short", "pb-none"}

    def test_rapid_successive_events_only_first_triggers(self) -> None:
        """Multiple rapid events: only the first one triggers, rest are dropped.

        Strengthens case (g): simulates N events arriving in quick
        succession — after the first execution, all subsequent events
        during the cooldown window are dropped (not queued).
        """
        pb = _make_playbook(cooldown_seconds=60, triggers=["git.commit"])
        manager = _manager_with_playbooks(pb)

        now = time.monotonic()

        # First event triggers
        result = manager.get_triggerable_playbooks("git.commit", "system")
        assert len(result) == 1

        # Playbook executes, recording completion
        manager.record_execution("test-playbook", "system", _clock=now)

        # Simulate 5 rapid successive events — all should be dropped
        for i in range(5):
            result = manager.get_triggerable_playbooks("git.commit", "system")
            assert result == [], f"Event {i + 1} during cooldown must be dropped"
