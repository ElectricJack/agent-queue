"""Tests for PlaybookManager event-to-scope matching (roadmap 5.3.3, 5.3.8).

Tests cover the spec §7 "Event-to-Scope Matching" requirements:
  - System-scoped playbooks match all events (with or without project_id)
  - Project-scoped playbooks match only events with matching project_id
  - Project-scoped playbooks skip events without project_id
  - Agent-type-scoped playbooks match events with project_id + matching agent_type
  - Agent-type-scoped playbooks skip events without project_id
  - Agent-type-scoped playbooks skip events with wrong agent_type
  - Scope identifiers are tracked through load_from_store, compile, and remove
  - Cooldown scope keys are derived from playbook scope, not event data

Roadmap 5.3.8 test cases:
  (a) task.completed with project_id triggers both project-scoped AND system-scoped
  (b) task.completed with project_id does NOT trigger project-scoped for different project
  (c) event without project_id triggers only system-scoped playbooks
  (d) agent-type-scoped triggers only when event's agent matches
  (e) multiple playbooks subscribed to same event type all trigger
  (f) playbook with trigger that never fires does not interfere with others
  (g) unrecognized event type does not cause errors
"""

from __future__ import annotations

import pytest

from src.event_bus import EventBus
from src.playbooks.manager import PlaybookManager
from src.playbooks.models import CompiledPlaybook, PlaybookNode, PlaybookTrigger


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
        assert mgr._matches_scope(pb, {"project_id": "myapp", "agent_type": "coding"}) is True

    def test_agent_type_scope_skips_event_with_wrong_type(self) -> None:
        """Agent-type playbooks skip events with a different agent_type."""
        mgr = _make_manager()
        pb = _make_playbook(playbook_id="coding-pb", scope="agent-type:coding")
        mgr._active[pb.id] = pb
        assert mgr._matches_scope(pb, {"project_id": "myapp", "agent_type": "review"}) is False

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
        assert mgr._matches_scope(pb, {"agent_type": "coding"}) is False

    def test_agent_type_scope_with_null_project_id(self) -> None:
        """Agent-type playbooks skip events where project_id is explicitly None."""
        mgr = _make_manager()
        pb = _make_playbook(playbook_id="coding-pb", scope="agent-type:coding")
        mgr._active[pb.id] = pb
        assert mgr._matches_scope(pb, {"project_id": None, "agent_type": "coding"}) is False


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
        """Project-scoped playbook does NOT fire for non-timer events without project_id."""
        mgr = PlaybookManager(event_bus=event_bus, on_trigger=on_trigger)
        pb = _make_playbook(
            playbook_id="quality-gate",
            scope="project",
            triggers=["config.reloaded"],
        )
        mgr._active[pb.id] = pb
        mgr._index_triggers(pb)
        mgr._scope_identifiers[pb.id] = "myapp"
        mgr.subscribe_to_events()

        await event_bus.emit("config.reloaded", {})

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

    async def test_timer_event_fires_project_scoped_playbook(
        self, event_bus: EventBus, trigger_log: list, on_trigger
    ) -> None:
        """Timer events fire both system- and project-scoped playbooks.

        Per spec §7 timer/cron events are system-level (project_id=null),
        but project-scoped playbooks still fire as if the tick had been
        scoped to the playbook's own project.
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

        triggered = {entry[0]: entry[1] for entry in trigger_log}
        assert "sys-timer" in triggered
        assert "proj-timer" in triggered
        assert triggered["proj-timer"]["project_id"] == "myapp"
        assert len(trigger_log) == 2

    async def test_cron_event_fires_project_scoped_playbook(
        self, event_bus: EventBus, trigger_log: list, on_trigger
    ) -> None:
        """Cron events fire project-scoped playbooks with injected project_id.

        Regression: a project-scoped playbook with `cron.08:00` trigger was
        silently dropped because the timer service emits cron events with
        project_id=null.
        """
        mgr = PlaybookManager(event_bus=event_bus, on_trigger=on_trigger)

        pb = _make_playbook(
            playbook_id="morning-outfit",
            scope="project",
            triggers=["cron.08:00"],
        )
        mgr._active[pb.id] = pb
        mgr._index_triggers(pb)
        mgr._scope_identifiers[pb.id] = "my-playbooks"
        mgr.subscribe_to_events()

        await event_bus.emit(
            "cron.08:00", {"tick_time": "2026-04-21T08:00:00", "interval": "08:00"}
        )

        assert len(trigger_log) == 1
        assert trigger_log[0][0] == "morning-outfit"
        assert trigger_log[0][1]["project_id"] == "my-playbooks"

    async def test_timer_event_skips_project_scoped_without_identifier(
        self, event_bus: EventBus, trigger_log: list, on_trigger
    ) -> None:
        """Project-scoped playbook with no scope_identifier falls back to
        the old behavior: timer events without project_id are skipped.
        """
        mgr = PlaybookManager(event_bus=event_bus, on_trigger=on_trigger)

        pb = _make_playbook(
            playbook_id="orphan-pb",
            scope="project",
            triggers=["timer.30m"],
        )
        mgr._active[pb.id] = pb
        mgr._index_triggers(pb)
        # No scope_identifier set — no project to inject
        mgr.subscribe_to_events()

        await event_bus.emit("timer.30m", {"tick_time": "2026-01-01", "interval": "30m"})

        assert len(trigger_log) == 0

    # -------------------------------------------------------------------
    # Roadmap 5.3.8 — dedicated test cases (a)-(g)
    # -------------------------------------------------------------------

    async def test_538a_project_event_triggers_both_project_and_system(
        self, event_bus: EventBus, trigger_log: list, on_trigger
    ) -> None:
        """5.3.8(a): task.completed with project_id triggers BOTH
        project-scoped playbook for that project AND system-scoped playbooks.
        """
        mgr = PlaybookManager(event_bus=event_bus, on_trigger=on_trigger)

        system_pb = _make_playbook(
            playbook_id="sys-monitor",
            scope="system",
            triggers=["task.completed"],
        )
        project_pb = _make_playbook(
            playbook_id="myapp-gate",
            scope="project",
            triggers=["task.completed"],
        )

        for pb in [system_pb, project_pb]:
            mgr._active[pb.id] = pb
            mgr._index_triggers(pb)

        mgr._scope_identifiers["myapp-gate"] = "myapp"
        mgr.subscribe_to_events()

        await event_bus.emit("task.completed", {"project_id": "myapp"})

        triggered_ids = {entry[0] for entry in trigger_log}
        assert "sys-monitor" in triggered_ids, "System-scoped playbook should fire"
        assert "myapp-gate" in triggered_ids, "Project-scoped playbook should fire"
        assert len(trigger_log) == 2

    async def test_538b_project_event_skips_different_project_playbook(
        self, event_bus: EventBus, trigger_log: list, on_trigger
    ) -> None:
        """5.3.8(b): task.completed with project_id="myapp" does NOT trigger
        project-scoped playbook for a different project.
        """
        mgr = PlaybookManager(event_bus=event_bus, on_trigger=on_trigger)

        other_pb = _make_playbook(
            playbook_id="other-gate",
            scope="project",
            triggers=["task.completed"],
        )
        mgr._active[other_pb.id] = other_pb
        mgr._index_triggers(other_pb)
        mgr._scope_identifiers["other-gate"] = "other-project"
        mgr.subscribe_to_events()

        await event_bus.emit("task.completed", {"project_id": "myapp"})

        assert len(trigger_log) == 0

    async def test_538c_no_project_id_triggers_only_system(
        self, event_bus: EventBus, trigger_log: list, on_trigger
    ) -> None:
        """5.3.8(c): event without project_id triggers only system-scoped playbooks."""
        mgr = PlaybookManager(event_bus=event_bus, on_trigger=on_trigger)

        system_pb = _make_playbook(
            playbook_id="sys-pb",
            scope="system",
            triggers=["task.completed"],
        )
        project_pb = _make_playbook(
            playbook_id="proj-pb",
            scope="project",
            triggers=["task.completed"],
        )
        agent_pb = _make_playbook(
            playbook_id="agent-pb",
            scope="agent-type:coding",
            triggers=["task.completed"],
        )

        for pb in [system_pb, project_pb, agent_pb]:
            mgr._active[pb.id] = pb
            mgr._index_triggers(pb)

        mgr._scope_identifiers["proj-pb"] = "myapp"
        mgr.subscribe_to_events()

        await event_bus.emit("task.completed", {})

        assert len(trigger_log) == 1
        assert trigger_log[0][0] == "sys-pb"

    async def test_538d_agent_type_triggers_only_matching_type(
        self, event_bus: EventBus, trigger_log: list, on_trigger
    ) -> None:
        """5.3.8(d): agent-type-scoped playbook triggers only when event's
        agent_type matches, not for other agent types.
        """
        mgr = PlaybookManager(event_bus=event_bus, on_trigger=on_trigger)

        coding_pb = _make_playbook(
            playbook_id="coding-reflection",
            scope="agent-type:coding",
            triggers=["task.completed"],
        )
        review_pb = _make_playbook(
            playbook_id="review-reflection",
            scope="agent-type:review",
            triggers=["task.completed"],
        )

        for pb in [coding_pb, review_pb]:
            mgr._active[pb.id] = pb
            mgr._index_triggers(pb)

        mgr.subscribe_to_events()

        # Emit event with agent_type=coding
        await event_bus.emit(
            "task.completed",
            {"project_id": "myapp", "agent_type": "coding"},
        )

        assert len(trigger_log) == 1
        assert trigger_log[0][0] == "coding-reflection"

    async def test_538e_multiple_playbooks_same_event_all_trigger(
        self, event_bus: EventBus, trigger_log: list, on_trigger
    ) -> None:
        """5.3.8(e): multiple playbooks subscribed to same event type all
        trigger (not just the first one).
        """
        mgr = PlaybookManager(event_bus=event_bus, on_trigger=on_trigger)

        # Three system-scoped playbooks all subscribing to the same event
        pb_a = _make_playbook(
            playbook_id="monitor-a",
            scope="system",
            triggers=["task.completed"],
        )
        pb_b = _make_playbook(
            playbook_id="monitor-b",
            scope="system",
            triggers=["task.completed"],
        )
        pb_c = _make_playbook(
            playbook_id="monitor-c",
            scope="system",
            triggers=["task.completed"],
        )

        for pb in [pb_a, pb_b, pb_c]:
            mgr._active[pb.id] = pb
            mgr._index_triggers(pb)

        mgr.subscribe_to_events()

        await event_bus.emit("task.completed", {"project_id": "myapp"})

        triggered_ids = {entry[0] for entry in trigger_log}
        assert "monitor-a" in triggered_ids
        assert "monitor-b" in triggered_ids
        assert "monitor-c" in triggered_ids
        assert len(trigger_log) == 3

    async def test_538f_unused_trigger_does_not_interfere(
        self, event_bus: EventBus, trigger_log: list, on_trigger
    ) -> None:
        """5.3.8(f): playbook with trigger event type that never fires does
        not interfere with other playbooks that do fire.
        """
        mgr = PlaybookManager(event_bus=event_bus, on_trigger=on_trigger)

        # This playbook subscribes to an event that is never emitted
        idle_pb = _make_playbook(
            playbook_id="idle-pb",
            scope="system",
            triggers=["some.rare.event"],
        )
        # This playbook subscribes to an event that IS emitted
        active_pb = _make_playbook(
            playbook_id="active-pb",
            scope="system",
            triggers=["task.completed"],
        )

        for pb in [idle_pb, active_pb]:
            mgr._active[pb.id] = pb
            mgr._index_triggers(pb)

        mgr.subscribe_to_events()

        # Emit only task.completed — the idle playbook's trigger never fires
        await event_bus.emit("task.completed", {"project_id": "myapp"})

        assert len(trigger_log) == 1
        assert trigger_log[0][0] == "active-pb"

        # Verify idle playbook is still registered and functional
        assert "idle-pb" in mgr._active

    async def test_538g_unrecognized_event_type_no_errors(
        self, event_bus: EventBus, trigger_log: list, on_trigger
    ) -> None:
        """5.3.8(g): unrecognized event type does not cause errors in
        PlaybookManager — the manager should handle unknown events gracefully.
        """
        mgr = PlaybookManager(event_bus=event_bus, on_trigger=on_trigger)

        pb = _make_playbook(
            playbook_id="normal-pb",
            scope="system",
            triggers=["task.completed"],
        )
        mgr._active[pb.id] = pb
        mgr._index_triggers(pb)
        mgr.subscribe_to_events()

        # Emit an event type that no playbook subscribes to — should not raise
        await event_bus.emit("totally.unknown.event.type", {"foo": "bar"})

        # No playbook triggered
        assert len(trigger_log) == 0

        # Manager is still functional — emitting the real event still works
        await event_bus.emit("task.completed", {"project_id": "myapp"})
        assert len(trigger_log) == 1
        assert trigger_log[0][0] == "normal-pb"
