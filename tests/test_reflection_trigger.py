"""Tests for reflection playbook trigger on task.completed/failed (roadmap 6.1.3).

Verifies that:
  - task.completed events include agent_type from the resolved profile
  - task.failed events include agent_type when available
  - Agent-type-scoped reflection playbooks trigger on matching events
  - Agent-type-scoped playbooks do NOT trigger when agent_type is missing/wrong
  - Event schemas accept agent_id and agent_type as optional fields
"""

from __future__ import annotations

import pytest

from src.event_bus import EventBus
from src.event_schemas import get_schema, validate_payload
from src.playbooks.manager import PlaybookManager
from src.playbooks.models import CompiledPlaybook, PlaybookNode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_playbook(
    *,
    playbook_id: str = "coding-reflection",
    triggers: list[str] | None = None,
    scope: str = "agent-type:coding",
    cooldown_seconds: int | None = None,
) -> CompiledPlaybook:
    """Create a minimal agent-type-scoped playbook for testing."""
    return CompiledPlaybook(
        id=playbook_id,
        version=1,
        source_hash="abc123",
        triggers=triggers or ["task.completed", "task.failed"],
        scope=scope,
        cooldown_seconds=cooldown_seconds,
        nodes={
            "start": PlaybookNode(entry=True, prompt="Reflect on task.", goto="end"),
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
    return PlaybookManager(config=None, event_bus=bus, on_trigger=on_trigger)


# ---------------------------------------------------------------------------
# Tests: Event schema includes agent_type
# ---------------------------------------------------------------------------


class TestEventSchemaAgentType:
    """Verify event schemas include agent_type and agent_id as optional fields."""

    def test_task_completed_schema_has_agent_type(self) -> None:
        """task.completed schema lists agent_type as optional."""
        schema = get_schema("task.completed")
        assert schema is not None
        assert "agent_type" in schema["optional"]

    def test_task_completed_schema_has_agent_id(self) -> None:
        """task.completed schema lists agent_id as optional."""
        schema = get_schema("task.completed")
        assert schema is not None
        assert "agent_id" in schema["optional"]

    def test_task_failed_schema_has_agent_type(self) -> None:
        """task.failed schema lists agent_type as optional."""
        schema = get_schema("task.failed")
        assert schema is not None
        assert "agent_type" in schema["optional"]

    def test_task_failed_schema_has_agent_id(self) -> None:
        """task.failed schema lists agent_id as optional."""
        schema = get_schema("task.failed")
        assert schema is not None
        assert "agent_id" in schema["optional"]


class TestEventSchemaValidation:
    """Verify event payloads with agent_type pass schema validation."""

    def test_task_completed_with_agent_type_is_valid(self) -> None:
        """task.completed payload with agent_type passes validation."""
        errors = validate_payload("task.completed", {
            "task_id": "t-123",
            "project_id": "myapp",
            "title": "Fix the bug",
            "agent_id": "agent-1",
            "agent_type": "coding",
        })
        assert errors == []

    def test_task_completed_without_agent_type_is_still_valid(self) -> None:
        """task.completed payload without agent_type still passes (it's optional)."""
        errors = validate_payload("task.completed", {
            "task_id": "t-123",
            "project_id": "myapp",
            "title": "Fix the bug",
        })
        assert errors == []

    def test_task_failed_with_agent_type_is_valid(self) -> None:
        """task.failed payload with agent_type passes validation."""
        errors = validate_payload("task.failed", {
            "task_id": "t-123",
            "project_id": "myapp",
            "title": "Fix the bug",
            "status": "BLOCKED",
            "context": "max_retries",
            "error": "Max retries exhausted",
            "agent_id": "agent-1",
            "agent_type": "coding",
        })
        assert errors == []


# ---------------------------------------------------------------------------
# Tests: Reflection playbook triggers for matching agent type
# ---------------------------------------------------------------------------


class TestReflectionPlaybookTrigger:
    """Integration tests: agent-type-scoped reflection playbook triggering."""

    @pytest.fixture
    def event_bus(self) -> EventBus:
        return EventBus(validate_events=False)

    @pytest.fixture
    def trigger_log(self) -> list:
        return []

    @pytest.fixture
    def on_trigger(self, trigger_log: list):
        async def callback(playbook: CompiledPlaybook, data: dict) -> None:
            trigger_log.append({"playbook_id": playbook.id, "data": data})
        return callback

    async def test_coding_reflection_fires_on_task_completed_with_matching_type(
        self, event_bus: EventBus, trigger_log: list, on_trigger
    ) -> None:
        """Reflection playbook fires when task.completed has agent_type=coding."""
        mgr = _make_manager(event_bus=event_bus, on_trigger=on_trigger)
        pb = _make_playbook()
        mgr._active[pb.id] = pb
        mgr._index_triggers(pb)
        mgr.subscribe_to_events()

        await event_bus.emit("task.completed", {
            "task_id": "t-123",
            "project_id": "myapp",
            "title": "Implement feature",
            "agent_id": "agent-1",
            "agent_type": "coding",
        })

        assert len(trigger_log) == 1
        assert trigger_log[0]["playbook_id"] == "coding-reflection"
        assert trigger_log[0]["data"]["agent_type"] == "coding"

    async def test_coding_reflection_fires_on_task_failed_with_matching_type(
        self, event_bus: EventBus, trigger_log: list, on_trigger
    ) -> None:
        """Reflection playbook fires when task.failed has agent_type=coding."""
        mgr = _make_manager(event_bus=event_bus, on_trigger=on_trigger)
        pb = _make_playbook()
        mgr._active[pb.id] = pb
        mgr._index_triggers(pb)
        mgr.subscribe_to_events()

        await event_bus.emit("task.failed", {
            "task_id": "t-456",
            "project_id": "myapp",
            "title": "Fix bug",
            "status": "BLOCKED",
            "context": "max_retries",
            "agent_type": "coding",
        })

        assert len(trigger_log) == 1
        assert trigger_log[0]["playbook_id"] == "coding-reflection"

    async def test_reflection_does_not_fire_for_wrong_agent_type(
        self, event_bus: EventBus, trigger_log: list, on_trigger
    ) -> None:
        """Coding reflection playbook does NOT fire for agent_type=review."""
        mgr = _make_manager(event_bus=event_bus, on_trigger=on_trigger)
        pb = _make_playbook()
        mgr._active[pb.id] = pb
        mgr._index_triggers(pb)
        mgr.subscribe_to_events()

        await event_bus.emit("task.completed", {
            "task_id": "t-789",
            "project_id": "myapp",
            "title": "Review PR",
            "agent_type": "review",
        })

        assert len(trigger_log) == 0

    async def test_reflection_does_not_fire_without_agent_type(
        self, event_bus: EventBus, trigger_log: list, on_trigger
    ) -> None:
        """Coding reflection playbook does NOT fire when agent_type is missing.

        This represents the legacy case where events don't include agent_type.
        """
        mgr = _make_manager(event_bus=event_bus, on_trigger=on_trigger)
        pb = _make_playbook()
        mgr._active[pb.id] = pb
        mgr._index_triggers(pb)
        mgr.subscribe_to_events()

        await event_bus.emit("task.completed", {
            "task_id": "t-000",
            "project_id": "myapp",
            "title": "Some task",
        })

        assert len(trigger_log) == 0

    async def test_reflection_does_not_fire_with_agent_type_none(
        self, event_bus: EventBus, trigger_log: list, on_trigger
    ) -> None:
        """Coding reflection playbook does NOT fire when agent_type is None.

        This represents the case where no profile was resolved for the task.
        """
        mgr = _make_manager(event_bus=event_bus, on_trigger=on_trigger)
        pb = _make_playbook()
        mgr._active[pb.id] = pb
        mgr._index_triggers(pb)
        mgr.subscribe_to_events()

        await event_bus.emit("task.completed", {
            "task_id": "t-111",
            "project_id": "myapp",
            "title": "Some task",
            "agent_type": None,
        })

        assert len(trigger_log) == 0

    async def test_multiple_agent_type_playbooks_only_matching_fires(
        self, event_bus: EventBus, trigger_log: list, on_trigger
    ) -> None:
        """When multiple agent-type playbooks exist, only the matching one fires."""
        mgr = _make_manager(event_bus=event_bus, on_trigger=on_trigger)

        coding_pb = _make_playbook(
            playbook_id="coding-reflection",
            scope="agent-type:coding",
        )
        review_pb = _make_playbook(
            playbook_id="review-reflection",
            scope="agent-type:review",
        )

        for pb in [coding_pb, review_pb]:
            mgr._active[pb.id] = pb
            mgr._index_triggers(pb)

        mgr.subscribe_to_events()

        await event_bus.emit("task.completed", {
            "task_id": "t-222",
            "project_id": "myapp",
            "title": "Implement X",
            "agent_type": "coding",
        })

        assert len(trigger_log) == 1
        assert trigger_log[0]["playbook_id"] == "coding-reflection"

    async def test_system_and_agent_type_playbooks_both_fire(
        self, event_bus: EventBus, trigger_log: list, on_trigger
    ) -> None:
        """System-scoped playbook AND matching agent-type playbook both fire.

        This validates that the system task-outcome playbook and the
        coding reflection playbook can coexist: a task.completed event
        with agent_type=coding triggers both.
        """
        mgr = _make_manager(event_bus=event_bus, on_trigger=on_trigger)

        system_pb = CompiledPlaybook(
            id="task-outcome",
            version=1,
            source_hash="sys123",
            triggers=["task.completed"],
            scope="system",
            nodes={
                "start": PlaybookNode(entry=True, prompt="Check outcome.", goto="end"),
                "end": PlaybookNode(terminal=True),
            },
        )
        coding_pb = _make_playbook(
            playbook_id="coding-reflection",
            scope="agent-type:coding",
        )

        for pb in [system_pb, coding_pb]:
            mgr._active[pb.id] = pb
            mgr._index_triggers(pb)

        mgr.subscribe_to_events()

        await event_bus.emit("task.completed", {
            "task_id": "t-333",
            "project_id": "myapp",
            "title": "Build feature",
            "agent_id": "agent-1",
            "agent_type": "coding",
        })

        triggered_ids = {entry["playbook_id"] for entry in trigger_log}
        assert "task-outcome" in triggered_ids
        assert "coding-reflection" in triggered_ids
        assert len(trigger_log) == 2

    async def test_agent_type_from_profile_id_matches_vault_path(
        self, event_bus: EventBus, trigger_log: list, on_trigger
    ) -> None:
        """Verify that profile.id ("coding") matches vault agent-type scope.

        The mapping is: task → resolved profile → profile.id → agent_type
        in the event payload.  The playbook scope "agent-type:coding"
        extracts "coding" as the type identifier.  This test verifies
        that the two align.
        """
        mgr = _make_manager(event_bus=event_bus, on_trigger=on_trigger)
        pb = _make_playbook(
            playbook_id="coding-reflection",
            scope="agent-type:coding",
        )
        mgr._active[pb.id] = pb
        mgr._index_triggers(pb)
        mgr.subscribe_to_events()

        # Simulate the orchestrator's emit: profile.id = "coding"
        await event_bus.emit("task.completed", {
            "task_id": "t-444",
            "project_id": "myapp",
            "title": "Refactor module",
            "agent_id": "agent-2",
            "agent_type": "coding",  # This comes from profile.id
        })

        assert len(trigger_log) == 1
        assert trigger_log[0]["data"]["agent_type"] == "coding"


# ---------------------------------------------------------------------------
# Tests: Cooldown respects agent-type scope
# ---------------------------------------------------------------------------


class TestReflectionCooldown:
    """Verify cooldown works correctly for agent-type-scoped reflection."""

    @pytest.fixture
    def event_bus(self) -> EventBus:
        return EventBus(validate_events=False)

    @pytest.fixture
    def trigger_log(self) -> list:
        return []

    @pytest.fixture
    def on_trigger(self, trigger_log: list):
        async def callback(playbook: CompiledPlaybook, data: dict) -> None:
            trigger_log.append({"playbook_id": playbook.id, "data": data})
        return callback

    async def test_cooldown_blocks_rapid_reflection(
        self, event_bus: EventBus, trigger_log: list, on_trigger
    ) -> None:
        """Second task.completed within cooldown window does not re-trigger."""
        mgr = _make_manager(event_bus=event_bus, on_trigger=on_trigger)
        pb = _make_playbook(cooldown_seconds=30)
        mgr._active[pb.id] = pb
        mgr._index_triggers(pb)
        mgr.subscribe_to_events()

        event_data = {
            "task_id": "t-555",
            "project_id": "myapp",
            "title": "First task",
            "agent_type": "coding",
        }

        # First trigger — should fire
        await event_bus.emit("task.completed", event_data)
        assert len(trigger_log) == 1

        # Record execution (simulating completion callback)
        mgr.record_execution(pb.id, "agent-type:coding")

        # Second trigger — should be blocked by cooldown
        event_data["task_id"] = "t-556"
        event_data["title"] = "Second task"
        await event_bus.emit("task.completed", event_data)
        assert len(trigger_log) == 1  # Still 1, second was blocked
