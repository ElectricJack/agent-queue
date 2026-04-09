"""Tests for PlaybookManager event-to-scope matching (roadmap 5.3.3).

Tests cover the spec §7 "Event-to-Scope Matching" requirements:
  - System-scoped playbooks match all events (with or without project_id)
  - Project-scoped playbooks match only events with matching project_id
  - Project-scoped playbooks skip events without project_id
  - Agent-type-scoped playbooks match events with project_id + matching agent_type
  - Agent-type-scoped playbooks skip events without project_id
  - Agent-type-scoped playbooks skip events with wrong agent_type
  - Scope identifiers are tracked through load_from_store, compile, and remove
  - Cooldown scope keys are derived from playbook scope, not event data
"""

from __future__ import annotations

import pytest

from src.event_bus import EventBus
from src.playbook_manager import PlaybookManager
from src.playbook_models import CompiledPlaybook, PlaybookNode, PlaybookTrigger


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_playbook(
    *,
    playbook_id: str = "test-playbook",
    version: int = 1,
    source_hash: str = "abc123def456",
    triggers: list[str | dict | PlaybookTrigger] | None = None,
    scope: str = "system",
    cooldown_seconds: int | None = None,
) -> CompiledPlaybook:
    """Create a minimal valid CompiledPlaybook for testing."""
    return CompiledPlaybook(
        id=playbook_id,
        version=version,
        source_hash=source_hash,
        triggers=triggers or ["task.completed"],
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


def _make_manager(
    *,
    event_bus: EventBus | None = None,
    on_trigger: object | None = None,
) -> PlaybookManager:
    """Create a PlaybookManager with an EventBus."""
    bus = event_bus or EventBus(validate_events=False)
    return PlaybookManager(
        event_bus=bus,
        on_trigger=on_trigger,
    )


# ---------------------------------------------------------------------------
# Tests: _matches_scope (unit)
# ---------------------------------------------------------------------------


class TestMatchesScope:
    """Unit tests for PlaybookManager._matches_scope."""

    def test_system_scope_matches_event_with_project_id(self) -> None:
        """System-scoped playbooks match events with project_id."""
        mgr = _make_manager()
        pb = _make_playbook(scope="system")
        assert mgr._matches_scope(pb, {"project_id": "myapp"}) is True

    def test_system_scope_matches_event_without_project_id(self) -> None:
        """System-scoped playbooks match events without project_id."""
        mgr = _make_manager()
        pb = _make_playbook(scope="system")
        assert mgr._matches_scope(pb, {}) is True

    def test_system_scope_matches_event_with_null_project_id(self) -> None:
        """System-scoped playbooks match events with project_id=None."""
        mgr = _make_manager()
        pb = _make_playbook(scope="system")
        assert mgr._matches_scope(pb, {"project_id": None}) is True

    def test_project_scope_matches_event_with_matching_project(self) -> None:
        """Project-scoped playbooks match events with the same project_id."""
        mgr = _make_manager()
        pb = _make_playbook(playbook_id="proj-pb", scope="project")
        mgr._active[pb.id] = pb
        mgr._scope_identifiers[pb.id] = "myapp"
        assert mgr._matches_scope(pb, {"project_id": "myapp"}) is True

    def test_project_scope_skips_event_with_different_project(self) -> None:
        """Project-scoped playbooks skip events for a different project."""
        mgr = _make_manager()
        pb = _make_playbook(playbook_id="proj-pb", scope="project")
        mgr._active[pb.id] = pb
        mgr._scope_identifiers[pb.id] = "myapp"
        assert mgr._matches_scope(pb, {"project_id": "other-app"}) is False

    def test_project_scope_skips_event_without_project_id(self) -> None:
        """Project-scoped playbooks skip events without project_id."""
        mgr = _make_manager()
        pb = _make_playbook(playbook_id="proj-pb", scope="project")
        mgr._active[pb.id] = pb
        mgr._scope_identifiers[pb.id] = "myapp"
        assert mgr._matches_scope(pb, {}) is False

    def test_project_scope_skips_event_with_null_project_id(self) -> None:
        """Project-scoped playbooks skip events where project_id is None."""
        mgr = _make_manager()
        pb = _make_playbook(playbook_id="proj-pb", scope="project")
        mgr._active[pb.id] = pb
        mgr._scope_identifiers[pb.id] = "myapp"
        assert mgr._matches_scope(pb, {"project_id": None}) is False

    def test_project_scope_without_identifier_matches_any_project(self) -> None:
        """Project-scoped playbook with no identifier matches any project event.

        This can happen when loaded from legacy flat-dir storage where
        the project_id is not available.
        """
        mgr = _make_manager()
        pb = _make_playbook(playbook_id="proj-pb", scope="project")
        mgr._active[pb.id] = pb
        # No identifier set — should match any event with project_id
        assert mgr._matches_scope(pb, {"project_id": "any-project"}) is True

    def test_project_scope_without_identifier_skips_no_project(self) -> None:
        """Project-scoped playbook with no identifier still skips no-project events."""
        mgr = _make_manager()
        pb = _make_playbook(playbook_id="proj-pb", scope="project")
        mgr._active[pb.id] = pb
        assert mgr._matches_scope(pb, {}) is False

    def test_agent_type_scope_matches_event_with_matching_type(self) -> None:
        """Agent-type playbooks match events with matching agent_type."""
        mgr = _make_manager()
        pb = _make_playbook(playbook_id="coding-pb", scope="agent-type:coding")
        mgr._active[pb.id] = pb
        assert mgr._matches_scope(
            pb, {"project_id": "myapp", "agent_type": "coding"}
        ) is True

    def test_agent_type_scope_skips_event_with_wrong_type(self) -> None:
        """Agent-type playbooks skip events with a different agent_type."""
        mgr = _make_manager()
        pb = _make_playbook(playbook_id="coding-pb", scope="agent-type:coding")
        mgr._active[pb.id] = pb
        assert mgr._matches_scope(
            pb, {"project_id": "myapp", "agent_type": "review"}
        ) is False

    def test_agent_type_scope_skips_event_without_agent_type(self) -> None:
        """Agent-type playbooks skip events that have no agent_type field."""
        mgr = _make_manager()
        pb = _make_playbook(playbook_id="coding-pb", scope="agent-type:coding")
        mgr._active[pb.id] = pb
        assert mgr._matches_scope(pb, {"project_id": "myapp"}) is False

    def test_agent_type_scope_skips_event_without_project_id(self) -> None:
        """Agent-type playbooks skip events without project_id.

        Per spec §7: events without project_id match system-scoped only.
        """
        mgr = _make_manager()
        pb = _make_playbook(playbook_id="coding-pb", scope="agent-type:coding")
        mgr._active[pb.id] = pb
        assert mgr._matches_scope(
            pb, {"agent_type": "coding"}
        ) is False

    def test_agent_type_scope_with_null_project_id(self) -> None:
        """Agent-type playbooks skip events where project_id is explicitly None."""
        mgr = _make_manager()
        pb = _make_playbook(playbook_id="coding-pb", scope="agent-type:coding")
        mgr._active[pb.id] = pb
        assert mgr._matches_scope(
            pb, {"project_id": None, "agent_type": "coding"}
        ) is False


# ---------------------------------------------------------------------------
# Tests: _cooldown_scope_key
# ---------------------------------------------------------------------------


class TestCooldownScopeKey:
    """Tests for PlaybookManager._cooldown_scope_key."""

    def test_system_scope_key(self) -> None:
        mgr = _make_manager()
        pb = _make_playbook(scope="system")
        assert mgr._cooldown_scope_key(pb) == "system"

    def test_project_scope_key_with_identifier(self) -> None:
        mgr = _make_manager()
        pb = _make_playbook(playbook_id="proj-pb", scope="project")
        mgr._scope_identifiers[pb.id] = "myapp"
        assert mgr._cooldown_scope_key(pb) == "project:myapp"

    def test_project_scope_key_without_identifier(self) -> None:
        mgr = _make_manager()
        pb = _make_playbook(playbook_id="proj-pb", scope="project")
        assert mgr._cooldown_scope_key(pb) == "project"

    def test_agent_type_scope_key(self) -> None:
        mgr = _make_manager()
        pb = _make_playbook(scope="agent-type:coding")
        assert mgr._cooldown_scope_key(pb) == "agent-type:coding"


# ---------------------------------------------------------------------------
# Tests: scope identifier tracking
# ---------------------------------------------------------------------------


class TestScopeIdentifierTracking:
    """Tests for scope identifier lifecycle management."""

    def test_set_and_get_scope_identifier(self) -> None:
        mgr = _make_manager()
        mgr.set_scope_identifier("proj-pb", "myapp")
        assert mgr.get_scope_identifier("proj-pb") == "myapp"

    def test_get_unknown_identifier_returns_none(self) -> None:
        mgr = _make_manager()
        assert mgr.get_scope_identifier("nonexistent") is None

    async def test_remove_playbook_clears_identifier(self) -> None:
        mgr = _make_manager()
        pb = _make_playbook(playbook_id="proj-pb", scope="project")
        mgr._active[pb.id] = pb
        mgr._scope_identifiers[pb.id] = "myapp"
        await mgr.remove_playbook("proj-pb")
        assert mgr.get_scope_identifier("proj-pb") is None

    async def test_load_from_disk_extracts_agent_type(self) -> None:
        """load_from_disk extracts agent-type identifier from scope string."""
        import json
        import tempfile
        from pathlib import Path

        pb = _make_playbook(playbook_id="coding-pb", scope="agent-type:coding")
        with tempfile.TemporaryDirectory() as tmpdir:
            compiled_dir = Path(tmpdir) / "playbooks" / "compiled"
            compiled_dir.mkdir(parents=True)
            path = compiled_dir / "coding-pb.json"
            path.write_text(json.dumps(pb.to_dict()))

            mgr = PlaybookManager(data_dir=tmpdir)
            count = await mgr.load_from_disk()
            assert count == 1
            assert mgr.get_scope_identifier("coding-pb") == "coding"


# ---------------------------------------------------------------------------
# Tests: end-to-end EventBus integration
# ---------------------------------------------------------------------------


class TestScopeMatchingEventBusIntegration:
    """Integration tests: scope matching with EventBus emit/subscribe."""

    @pytest.fixture
    def event_bus(self) -> EventBus:
        return EventBus(validate_events=False)

    @pytest.fixture
    def trigger_log(self) -> list:
        return []

    @pytest.fixture
    def on_trigger(self, trigger_log: list):
        async def callback(playbook: CompiledPlaybook, data: dict) -> None:
            trigger_log.append((playbook.id, data))
        return callback

    async def test_system_playbook_fires_for_project_event(
        self, event_bus: EventBus, trigger_log: list, on_trigger
    ) -> None:
        """System-scoped playbook fires when event has project_id."""
        mgr = PlaybookManager(event_bus=event_bus, on_trigger=on_trigger)
        pb = _make_playbook(scope="system", triggers=["task.completed"])
        mgr._active[pb.id] = pb
        mgr._index_triggers(pb)
        mgr.subscribe_to_events()

        await event_bus.emit("task.completed", {"project_id": "myapp"})

        assert len(trigger_log) == 1
        assert trigger_log[0][0] == "test-playbook"

    async def test_system_playbook_fires_for_system_event(
        self, event_bus: EventBus, trigger_log: list, on_trigger
    ) -> None:
        """System-scoped playbook fires for events without project_id."""
        mgr = PlaybookManager(event_bus=event_bus, on_trigger=on_trigger)
        pb = _make_playbook(scope="system", triggers=["config.reloaded"])
        mgr._active[pb.id] = pb
        mgr._index_triggers(pb)
        mgr.subscribe_to_events()

        await event_bus.emit("config.reloaded", {})

        assert len(trigger_log) == 1

    async def test_project_playbook_fires_for_matching_project(
        self, event_bus: EventBus, trigger_log: list, on_trigger
    ) -> None:
        """Project-scoped playbook fires for event with matching project_id."""
        mgr = PlaybookManager(event_bus=event_bus, on_trigger=on_trigger)
        pb = _make_playbook(
            playbook_id="quality-gate",
            scope="project",
            triggers=["task.completed"],
        )
        mgr._active[pb.id] = pb
        mgr._index_triggers(pb)
        mgr._scope_identifiers[pb.id] = "myapp"
        mgr.subscribe_to_events()

        await event_bus.emit("task.completed", {"project_id": "myapp"})

        assert len(trigger_log) == 1
        assert trigger_log[0][0] == "quality-gate"

    async def test_project_playbook_skips_different_project(
        self, event_bus: EventBus, trigger_log: list, on_trigger
    ) -> None:
        """Project-scoped playbook does NOT fire for a different project."""
        mgr = PlaybookManager(event_bus=event_bus, on_trigger=on_trigger)
        pb = _make_playbook(
            playbook_id="quality-gate",
            scope="project",
            triggers=["task.completed"],
        )
        mgr._active[pb.id] = pb
        mgr._index_triggers(pb)
        mgr._scope_identifiers[pb.id] = "myapp"
        mgr.subscribe_to_events()

        await event_bus.emit("task.completed", {"project_id": "other-app"})

        assert len(trigger_log) == 0

    async def test_project_playbook_skips_no_project(
        self, event_bus: EventBus, trigger_log: list, on_trigger
    ) -> None:
        """Project-scoped playbook does NOT fire for events without project_id."""
        mgr = PlaybookManager(event_bus=event_bus, on_trigger=on_trigger)
        pb = _make_playbook(
            playbook_id="quality-gate",
            scope="project",
            triggers=["timer.30m"],
        )
        mgr._active[pb.id] = pb
        mgr._index_triggers(pb)
        mgr._scope_identifiers[pb.id] = "myapp"
        mgr.subscribe_to_events()

        await event_bus.emit("timer.30m", {"tick_time": "2026-01-01T00:00:00Z"})

        assert len(trigger_log) == 0

    async def test_agent_type_playbook_fires_for_matching_event(
        self, event_bus: EventBus, trigger_log: list, on_trigger
    ) -> None:
        """Agent-type playbook fires for event with matching agent_type."""
        mgr = PlaybookManager(event_bus=event_bus, on_trigger=on_trigger)
        pb = _make_playbook(
            playbook_id="coding-reflection",
            scope="agent-type:coding",
            triggers=["task.completed"],
        )
        mgr._active[pb.id] = pb
        mgr._index_triggers(pb)
        mgr.subscribe_to_events()

        await event_bus.emit(
            "task.completed",
            {"project_id": "myapp", "agent_type": "coding"},
        )

        assert len(trigger_log) == 1
        assert trigger_log[0][0] == "coding-reflection"

    async def test_agent_type_playbook_skips_wrong_type(
        self, event_bus: EventBus, trigger_log: list, on_trigger
    ) -> None:
        """Agent-type playbook does NOT fire for events with different type."""
        mgr = PlaybookManager(event_bus=event_bus, on_trigger=on_trigger)
        pb = _make_playbook(
            playbook_id="coding-reflection",
            scope="agent-type:coding",
            triggers=["task.completed"],
        )
        mgr._active[pb.id] = pb
        mgr._index_triggers(pb)
        mgr.subscribe_to_events()

        await event_bus.emit(
            "task.completed",
            {"project_id": "myapp", "agent_type": "review"},
        )

        assert len(trigger_log) == 0

    async def test_agent_type_playbook_skips_system_event(
        self, event_bus: EventBus, trigger_log: list, on_trigger
    ) -> None:
        """Agent-type playbook does NOT fire for events without project_id."""
        mgr = PlaybookManager(event_bus=event_bus, on_trigger=on_trigger)
        pb = _make_playbook(
            playbook_id="coding-reflection",
            scope="agent-type:coding",
            triggers=["config.reloaded"],
        )
        mgr._active[pb.id] = pb
        mgr._index_triggers(pb)
        mgr.subscribe_to_events()

        await event_bus.emit("config.reloaded", {})

        assert len(trigger_log) == 0

    async def test_mixed_scopes_fire_correctly(
        self, event_bus: EventBus, trigger_log: list, on_trigger
    ) -> None:
        """Multiple playbooks with different scopes: only matching ones fire.

        Scenario: system + project(myapp) + project(other) + agent-type:coding
        Event: task.completed with project_id=myapp, agent_type=coding
        Expected: system + project(myapp) + agent-type:coding fire; project(other) doesn't.
        """
        mgr = PlaybookManager(event_bus=event_bus, on_trigger=on_trigger)

        system_pb = _make_playbook(
            playbook_id="sys-pb",
            scope="system",
            triggers=["task.completed"],
        )
        project_pb = _make_playbook(
            playbook_id="proj-myapp",
            scope="project",
            triggers=["task.completed"],
        )
        other_project_pb = _make_playbook(
            playbook_id="proj-other",
            scope="project",
            triggers=["task.completed"],
        )
        agent_pb = _make_playbook(
            playbook_id="coding-pb",
            scope="agent-type:coding",
            triggers=["task.completed"],
        )

        for pb in [system_pb, project_pb, other_project_pb, agent_pb]:
            mgr._active[pb.id] = pb
            mgr._index_triggers(pb)

        mgr._scope_identifiers["proj-myapp"] = "myapp"
        mgr._scope_identifiers["proj-other"] = "other-app"
        mgr.subscribe_to_events()

        await event_bus.emit(
            "task.completed",
            {"project_id": "myapp", "agent_type": "coding"},
        )

        triggered_ids = {entry[0] for entry in trigger_log}
        assert "sys-pb" in triggered_ids
        assert "proj-myapp" in triggered_ids
        assert "proj-other" not in triggered_ids
        assert "coding-pb" in triggered_ids
        assert len(trigger_log) == 3

    async def test_system_event_only_triggers_system_playbooks(
        self, event_bus: EventBus, trigger_log: list, on_trigger
    ) -> None:
        """System event (no project_id) triggers only system-scoped playbooks.

        All three scope types subscribe to the same event; only system fires.
        """
        mgr = PlaybookManager(event_bus=event_bus, on_trigger=on_trigger)

        system_pb = _make_playbook(
            playbook_id="sys-pb",
            scope="system",
            triggers=["config.reloaded"],
        )
        project_pb = _make_playbook(
            playbook_id="proj-pb",
            scope="project",
            triggers=["config.reloaded"],
        )
        agent_pb = _make_playbook(
            playbook_id="agent-pb",
            scope="agent-type:coding",
            triggers=["config.reloaded"],
        )

        for pb in [system_pb, project_pb, agent_pb]:
            mgr._active[pb.id] = pb
            mgr._index_triggers(pb)

        mgr._scope_identifiers["proj-pb"] = "myapp"
        mgr.subscribe_to_events()

        await event_bus.emit("config.reloaded", {})

        assert len(trigger_log) == 1
        assert trigger_log[0][0] == "sys-pb"

    async def test_timer_event_is_system_only(
        self, event_bus: EventBus, trigger_log: list, on_trigger
    ) -> None:
        """Timer events (no project_id) only trigger system-scoped playbooks.

        Per spec: timer events carry project_id=null — inherently system-scoped.
        """
        mgr = PlaybookManager(event_bus=event_bus, on_trigger=on_trigger)

        system_pb = _make_playbook(
            playbook_id="sys-timer",
            scope="system",
            triggers=["timer.30m"],
        )
        project_pb = _make_playbook(
            playbook_id="proj-timer",
            scope="project",
            triggers=["timer.30m"],
        )

        for pb in [system_pb, project_pb]:
            mgr._active[pb.id] = pb
            mgr._index_triggers(pb)

        mgr._scope_identifiers["proj-timer"] = "myapp"
        mgr.subscribe_to_events()

        await event_bus.emit("timer.30m", {"tick_time": "2026-01-01", "interval": "30m"})

        assert len(trigger_log) == 1
        assert trigger_log[0][0] == "sys-timer"
