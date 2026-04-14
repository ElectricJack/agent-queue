"""Tests for PlaybookManager EventBus subscription with payload filtering.

Tests cover roadmap 5.3.2 requirements:
  - PlaybookManager subscribes to EventBus for all trigger event types
  - Payload filters from structured triggers passed to EventBus subscribe()
  - Trigger handler checks cooldown and concurrency before dispatching
  - on_trigger callback invoked with matching playbook + event data
  - Subscriptions refreshed on compile/add/remove
  - Subscription lifecycle: subscribe, unsubscribe, shutdown cleanup
  - Mixed string and structured triggers coexist correctly
  - PlaybookTrigger model: string shorthand, structured dict, equality
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.event_bus import EventBus
from src.playbooks.manager import PlaybookManager
from src.playbooks.models import CompiledPlaybook, PlaybookNode, PlaybookTrigger


# ---------------------------------------------------------------------------
# Fixtures
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


def _make_playbook_md(
    *,
    playbook_id: str = "test-playbook",
    triggers: str = "- git.commit",
    scope: str = "system",
    body: str = "# Test\n\nDo something then finish.",
) -> str:
    """Create a minimal playbook markdown string."""
    return f"""\
---
id: {playbook_id}
triggers:
  {triggers}
scope: {scope}
---

{body}
"""


VALID_COMPILED_NODES = {
    "nodes": {
        "start": {
            "entry": True,
            "prompt": "Do something.",
            "goto": "end",
        },
        "end": {"terminal": True},
    }
}


def _make_mock_provider(responses: list[str] | None = None) -> AsyncMock:
    """Create a mock ChatProvider returning fenced JSON."""
    from src.chat_providers.types import ChatResponse, TextBlock

    provider = AsyncMock()
    provider.model_name = "test-model"

    if responses is None:
        json_str = json.dumps(VALID_COMPILED_NODES, indent=2)
        responses = [f"```json\n{json_str}\n```"]

    side_effects = []
    for text in responses:
        resp = ChatResponse(content=[TextBlock(text=text)])
        side_effects.append(resp)

    provider.create_message = AsyncMock(side_effect=side_effects)
    return provider


# ---------------------------------------------------------------------------
# Test: PlaybookTrigger model
# ---------------------------------------------------------------------------


class TestPlaybookTrigger:
    """Test the PlaybookTrigger dataclass."""

    def test_from_string(self) -> None:
        """String creates trigger with no filter."""
        t = PlaybookTrigger.from_value("git.commit")
        assert t.event_type == "git.commit"
        assert t.filter is None

    def test_from_dict_with_filter(self) -> None:
        """Dict with filter creates structured trigger."""
        t = PlaybookTrigger.from_value(
            {
                "event_type": "playbook.run.completed",
                "filter": {"playbook_id": "code-quality-gate"},
            }
        )
        assert t.event_type == "playbook.run.completed"
        assert t.filter == {"playbook_id": "code-quality-gate"}

    def test_from_dict_without_filter(self) -> None:
        """Dict without filter creates trigger with no filter."""
        t = PlaybookTrigger.from_value({"event_type": "task.completed"})
        assert t.event_type == "task.completed"
        assert t.filter is None

    def test_from_playbook_trigger(self) -> None:
        """Existing PlaybookTrigger passed through unchanged."""
        original = PlaybookTrigger(event_type="git.push", filter={"branch": "main"})
        t = PlaybookTrigger.from_value(original)
        assert t is original

    def test_from_invalid_type_raises(self) -> None:
        """Non-string/dict/PlaybookTrigger raises TypeError."""
        with pytest.raises(TypeError, match="Cannot create PlaybookTrigger"):
            PlaybookTrigger.from_value(42)

    def test_from_dict_missing_event_type_raises(self) -> None:
        """Dict without event_type raises ValueError."""
        with pytest.raises(ValueError, match="event_type"):
            PlaybookTrigger.from_value({"filter": {"x": "y"}})

    def test_to_value_string_shorthand(self) -> None:
        """Trigger without filter serializes to string."""
        t = PlaybookTrigger(event_type="git.commit")
        assert t.to_value() == "git.commit"

    def test_to_value_structured(self) -> None:
        """Trigger with filter serializes to dict."""
        t = PlaybookTrigger(
            event_type="playbook.run.completed",
            filter={"playbook_id": "code-quality-gate"},
        )
        assert t.to_value() == {
            "event_type": "playbook.run.completed",
            "filter": {"playbook_id": "code-quality-gate"},
        }

    def test_equality_with_string(self) -> None:
        """PlaybookTrigger without filter equals its event_type string."""
        t = PlaybookTrigger(event_type="git.commit")
        assert t == "git.commit"
        assert "git.commit" == t  # noqa: SIM300

    def test_inequality_with_string_when_filter(self) -> None:
        """PlaybookTrigger with filter does not equal a string."""
        t = PlaybookTrigger(event_type="git.commit", filter={"branch": "main"})
        assert t != "git.commit"

    def test_equality_between_triggers(self) -> None:
        """Two PlaybookTriggers are equal if event_type and filter match."""
        t1 = PlaybookTrigger(event_type="git.commit", filter={"x": 1})
        t2 = PlaybookTrigger(event_type="git.commit", filter={"x": 1})
        assert t1 == t2

    def test_hash_consistency(self) -> None:
        """Equal PlaybookTriggers have the same hash."""
        t1 = PlaybookTrigger(event_type="git.commit", filter={"a": 1, "b": 2})
        t2 = PlaybookTrigger(event_type="git.commit", filter={"b": 2, "a": 1})
        assert hash(t1) == hash(t2)

    def test_str_returns_event_type(self) -> None:
        t = PlaybookTrigger(event_type="git.commit", filter={"x": 1})
        assert str(t) == "git.commit"

    def test_repr_without_filter(self) -> None:
        t = PlaybookTrigger(event_type="git.commit")
        assert repr(t) == "PlaybookTrigger('git.commit')"

    def test_repr_with_filter(self) -> None:
        t = PlaybookTrigger(event_type="git.commit", filter={"branch": "main"})
        assert "filter=" in repr(t)


# ---------------------------------------------------------------------------
# Test: CompiledPlaybook trigger normalization
# ---------------------------------------------------------------------------


class TestCompiledPlaybookTriggers:
    """Test that CompiledPlaybook normalizes triggers to PlaybookTrigger."""

    def test_string_triggers_normalized(self) -> None:
        """String triggers auto-convert to PlaybookTrigger in __post_init__."""
        pb = _make_playbook(triggers=["git.commit", "task.completed"])
        assert all(isinstance(t, PlaybookTrigger) for t in pb.triggers)
        assert pb.triggers[0].event_type == "git.commit"
        assert pb.triggers[1].event_type == "task.completed"

    def test_dict_triggers_normalized(self) -> None:
        """Dict triggers auto-convert to PlaybookTrigger."""
        pb = _make_playbook(
            triggers=[
                {"event_type": "playbook.run.completed", "filter": {"playbook_id": "qg"}},
            ]
        )
        assert len(pb.triggers) == 1
        assert pb.triggers[0].event_type == "playbook.run.completed"
        assert pb.triggers[0].filter == {"playbook_id": "qg"}

    def test_mixed_triggers_normalized(self) -> None:
        """Mixed string and dict triggers both normalize correctly."""
        pb = _make_playbook(
            triggers=[
                "task.completed",
                {"event_type": "playbook.run.completed", "filter": {"playbook_id": "qg"}},
            ]
        )
        assert len(pb.triggers) == 2
        assert pb.triggers[0] == "task.completed"
        assert pb.triggers[1].event_type == "playbook.run.completed"
        assert pb.triggers[1].filter == {"playbook_id": "qg"}

    def test_trigger_event_types_property(self) -> None:
        """trigger_event_types returns sorted unique event types."""
        pb = _make_playbook(
            triggers=[
                "task.completed",
                {"event_type": "playbook.run.completed", "filter": {"playbook_id": "qg"}},
                "task.completed",  # duplicate
            ]
        )
        assert pb.trigger_event_types == ["playbook.run.completed", "task.completed"]

    def test_to_dict_round_trip_string_triggers(self) -> None:
        """String triggers round-trip through to_dict/from_dict."""
        pb = _make_playbook(triggers=["git.commit", "task.completed"])
        data = pb.to_dict()
        assert data["triggers"] == ["git.commit", "task.completed"]
        restored = CompiledPlaybook.from_dict(data)
        assert restored.triggers == pb.triggers

    def test_to_dict_round_trip_structured_triggers(self) -> None:
        """Structured triggers round-trip through to_dict/from_dict."""
        pb = _make_playbook(
            triggers=[
                {"event_type": "playbook.run.completed", "filter": {"playbook_id": "qg"}},
            ]
        )
        data = pb.to_dict()
        assert data["triggers"] == [
            {"event_type": "playbook.run.completed", "filter": {"playbook_id": "qg"}}
        ]
        restored = CompiledPlaybook.from_dict(data)
        assert restored.triggers[0].event_type == "playbook.run.completed"
        assert restored.triggers[0].filter == {"playbook_id": "qg"}

    def test_to_dict_round_trip_mixed_triggers(self) -> None:
        """Mixed triggers round-trip correctly."""
        pb = _make_playbook(
            triggers=[
                "task.completed",
                {"event_type": "playbook.run.completed", "filter": {"playbook_id": "qg"}},
            ]
        )
        data = pb.to_dict()
        assert data["triggers"] == [
            "task.completed",
            {"event_type": "playbook.run.completed", "filter": {"playbook_id": "qg"}},
        ]
        restored = CompiledPlaybook.from_dict(data)
        assert len(restored.triggers) == 2


# ---------------------------------------------------------------------------
# Test: EventBus subscription basics
# ---------------------------------------------------------------------------


class TestSubscribeToEvents:
    """Test PlaybookManager.subscribe_to_events() with real EventBus."""

    def test_subscribe_with_no_event_bus(self) -> None:
        """subscribe_to_events with no EventBus returns 0."""
        manager = PlaybookManager()
        count = manager.subscribe_to_events()
        assert count == 0
        assert manager.subscription_count == 0

    @pytest.mark.asyncio
    async def test_subscribe_with_no_playbooks(self) -> None:
        """subscribe_to_events with no active playbooks returns 0."""
        bus = EventBus(validate_events=False)
        manager = PlaybookManager(event_bus=bus)
        count = manager.subscribe_to_events()
        assert count == 0

    @pytest.mark.asyncio
    async def test_subscribe_creates_subscriptions(self, tmp_path: Path) -> None:
        """subscribe_to_events creates one subscription per trigger."""
        bus = EventBus(validate_events=False)
        manager = PlaybookManager(event_bus=bus, data_dir=str(tmp_path))

        # Pre-load playbooks
        compiled_dir = tmp_path / "playbooks" / "compiled"
        compiled_dir.mkdir(parents=True)
        pb = _make_playbook(triggers=["git.commit", "task.completed"])
        (compiled_dir / "test-playbook.json").write_text(json.dumps(pb.to_dict()))
        await manager.load_from_disk()

        count = manager.subscribe_to_events()
        assert count == 2  # Two triggers
        assert manager.subscription_count == 2

    @pytest.mark.asyncio
    async def test_subscribe_multiple_playbooks(self, tmp_path: Path) -> None:
        """subscribe_to_events handles multiple playbooks correctly."""
        bus = EventBus(validate_events=False)
        manager = PlaybookManager(event_bus=bus, data_dir=str(tmp_path))

        compiled_dir = tmp_path / "playbooks" / "compiled"
        compiled_dir.mkdir(parents=True)
        pb1 = _make_playbook(playbook_id="pb-1", triggers=["git.commit"])
        pb2 = _make_playbook(playbook_id="pb-2", triggers=["task.completed", "git.push"])
        (compiled_dir / "pb-1.json").write_text(json.dumps(pb1.to_dict()))
        (compiled_dir / "pb-2.json").write_text(json.dumps(pb2.to_dict()))
        await manager.load_from_disk()

        count = manager.subscribe_to_events()
        assert count == 3  # 1 + 2 triggers

    @pytest.mark.asyncio
    async def test_resubscribe_clears_old(self, tmp_path: Path) -> None:
        """Calling subscribe_to_events again removes old subscriptions first."""
        bus = EventBus(validate_events=False)
        manager = PlaybookManager(event_bus=bus, data_dir=str(tmp_path))

        compiled_dir = tmp_path / "playbooks" / "compiled"
        compiled_dir.mkdir(parents=True)
        pb = _make_playbook(triggers=["git.commit"])
        (compiled_dir / "test-playbook.json").write_text(json.dumps(pb.to_dict()))
        await manager.load_from_disk()

        count1 = manager.subscribe_to_events()
        assert count1 == 1

        # Subscribe again — should replace, not accumulate
        count2 = manager.subscribe_to_events()
        assert count2 == 1
        assert manager.subscription_count == 1

    def test_unsubscribe_clears_all(self) -> None:
        """unsubscribe_from_events removes all subscriptions."""
        bus = EventBus(validate_events=False)
        manager = PlaybookManager(event_bus=bus)

        # Manually add a playbook
        pb = _make_playbook(triggers=["git.commit"])
        manager._active[pb.id] = pb
        manager._index_triggers(pb)

        manager.subscribe_to_events()
        assert manager.subscription_count == 1

        manager.unsubscribe_from_events()
        assert manager.subscription_count == 0

    @pytest.mark.asyncio
    async def test_shutdown_clears_subscriptions(self, tmp_path: Path) -> None:
        """shutdown_runs also removes EventBus subscriptions."""
        bus = EventBus(validate_events=False)
        manager = PlaybookManager(event_bus=bus, data_dir=str(tmp_path))

        compiled_dir = tmp_path / "playbooks" / "compiled"
        compiled_dir.mkdir(parents=True)
        pb = _make_playbook(triggers=["git.commit"])
        (compiled_dir / "test-playbook.json").write_text(json.dumps(pb.to_dict()))
        await manager.load_from_disk()

        manager.subscribe_to_events()
        assert manager.subscription_count == 1

        await manager.shutdown_runs()
        assert manager.subscription_count == 0


# ---------------------------------------------------------------------------
# Test: Trigger handler dispatching with real EventBus
# ---------------------------------------------------------------------------


class TestTriggerHandler:
    """Test that events dispatched through EventBus reach the on_trigger callback."""

    @pytest.mark.asyncio
    async def test_simple_trigger_fires_callback(self) -> None:
        """An event matching a string trigger invokes on_trigger."""
        bus = EventBus(validate_events=False)
        triggered: list[tuple[str, dict]] = []

        async def on_trigger(playbook: CompiledPlaybook, data: dict) -> None:
            triggered.append((playbook.id, data))

        manager = PlaybookManager(event_bus=bus, on_trigger=on_trigger)
        pb = _make_playbook(triggers=["task.completed"])
        manager._active[pb.id] = pb
        manager._index_triggers(pb)
        manager.subscribe_to_events()

        await bus.emit("task.completed", {"task_id": "t1", "project_id": "p1", "title": "T"})

        assert len(triggered) == 1
        assert triggered[0][0] == "test-playbook"
        assert triggered[0][1]["task_id"] == "t1"

    @pytest.mark.asyncio
    async def test_filtered_trigger_only_fires_on_match(self) -> None:
        """A trigger with a filter only fires when the payload matches."""
        bus = EventBus(validate_events=False)
        triggered: list[str] = []

        async def on_trigger(playbook: CompiledPlaybook, data: dict) -> None:
            triggered.append(playbook.id)

        manager = PlaybookManager(event_bus=bus, on_trigger=on_trigger)
        pb = _make_playbook(
            playbook_id="post-qg",
            triggers=[
                {
                    "event_type": "playbook.run.completed",
                    "filter": {"playbook_id": "code-quality-gate"},
                },
            ],
        )
        manager._active[pb.id] = pb
        manager._index_triggers(pb)
        manager.subscribe_to_events()

        # Non-matching event — different playbook_id
        await bus.emit("playbook.run.completed", {"playbook_id": "other-playbook"})
        assert len(triggered) == 0

        # Matching event
        await bus.emit("playbook.run.completed", {"playbook_id": "code-quality-gate"})
        assert len(triggered) == 1
        assert triggered[0] == "post-qg"

    @pytest.mark.asyncio
    async def test_multiple_filter_fields_all_must_match(self) -> None:
        """All filter key/value pairs must match (AND semantics)."""
        bus = EventBus(validate_events=False)
        triggered: list[str] = []

        async def on_trigger(playbook: CompiledPlaybook, data: dict) -> None:
            triggered.append(playbook.id)

        manager = PlaybookManager(event_bus=bus, on_trigger=on_trigger)
        pb = _make_playbook(
            playbook_id="specific",
            triggers=[
                {
                    "event_type": "playbook.run.completed",
                    "filter": {"playbook_id": "qg", "status": "success"},
                },
            ],
        )
        manager._active[pb.id] = pb
        manager._index_triggers(pb)
        manager.subscribe_to_events()

        # Only one field matches
        await bus.emit("playbook.run.completed", {"playbook_id": "qg", "status": "failed"})
        assert len(triggered) == 0

        # Both fields match
        await bus.emit("playbook.run.completed", {"playbook_id": "qg", "status": "success"})
        assert len(triggered) == 1

    @pytest.mark.asyncio
    async def test_mixed_triggers_both_fire(self) -> None:
        """A playbook with both string and filtered triggers fires on both."""
        bus = EventBus(validate_events=False)
        triggered: list[dict] = []

        async def on_trigger(playbook: CompiledPlaybook, data: dict) -> None:
            triggered.append(data)

        manager = PlaybookManager(event_bus=bus, on_trigger=on_trigger)
        pb = _make_playbook(
            playbook_id="mixed",
            triggers=[
                "task.completed",
                {"event_type": "playbook.run.completed", "filter": {"playbook_id": "qg"}},
            ],
        )
        manager._active[pb.id] = pb
        manager._index_triggers(pb)
        manager.subscribe_to_events()

        # Fire the string trigger
        await bus.emit("task.completed", {"task_id": "t1", "project_id": "p", "title": "T"})
        assert len(triggered) == 1

        # Fire the filtered trigger
        await bus.emit("playbook.run.completed", {"playbook_id": "qg"})
        assert len(triggered) == 2

    @pytest.mark.asyncio
    async def test_no_callback_no_error(self) -> None:
        """When no on_trigger callback is set, matched events are a no-op."""
        bus = EventBus(validate_events=False)
        manager = PlaybookManager(event_bus=bus)  # no on_trigger
        pb = _make_playbook(triggers=["task.completed"])
        manager._active[pb.id] = pb
        manager._index_triggers(pb)
        manager.subscribe_to_events()

        # Should not raise
        await bus.emit("task.completed", {"task_id": "t1", "project_id": "p", "title": "T"})

    @pytest.mark.asyncio
    async def test_unrelated_event_does_not_fire(self) -> None:
        """Events not matching any trigger are ignored."""
        bus = EventBus(validate_events=False)
        triggered: list[str] = []

        async def on_trigger(playbook: CompiledPlaybook, data: dict) -> None:
            triggered.append(playbook.id)

        manager = PlaybookManager(event_bus=bus, on_trigger=on_trigger)
        pb = _make_playbook(triggers=["git.commit"])
        manager._active[pb.id] = pb
        manager._index_triggers(pb)
        manager.subscribe_to_events()

        await bus.emit("task.completed", {"task_id": "t1", "project_id": "p", "title": "T"})
        assert len(triggered) == 0

    @pytest.mark.asyncio
    async def test_removed_playbook_skipped(self) -> None:
        """If a playbook is removed between subscribe and event, handler skips it."""
        bus = EventBus(validate_events=False)
        triggered: list[str] = []

        async def on_trigger(playbook: CompiledPlaybook, data: dict) -> None:
            triggered.append(playbook.id)

        manager = PlaybookManager(event_bus=bus, on_trigger=on_trigger)
        pb = _make_playbook(triggers=["git.commit"])
        manager._active[pb.id] = pb
        manager._index_triggers(pb)
        manager.subscribe_to_events()

        # Remove the playbook without resubscribing (simulates race)
        del manager._active[pb.id]

        await bus.emit("git.commit", {})
        assert len(triggered) == 0

    @pytest.mark.asyncio
    async def test_sync_callback_works(self) -> None:
        """A synchronous on_trigger callback is also supported."""
        bus = EventBus(validate_events=False)
        triggered: list[str] = []

        def on_trigger(playbook: CompiledPlaybook, data: dict) -> None:
            triggered.append(playbook.id)

        manager = PlaybookManager(event_bus=bus, on_trigger=on_trigger)
        pb = _make_playbook(triggers=["git.commit"])
        manager._active[pb.id] = pb
        manager._index_triggers(pb)
        manager.subscribe_to_events()

        await bus.emit("git.commit", {})
        assert len(triggered) == 1

    @pytest.mark.asyncio
    async def test_callback_error_logged_not_raised(self) -> None:
        """If the on_trigger callback raises, it's logged but doesn't crash."""
        bus = EventBus(validate_events=False)

        async def bad_callback(playbook: CompiledPlaybook, data: dict) -> None:
            raise RuntimeError("Boom!")

        manager = PlaybookManager(event_bus=bus, on_trigger=bad_callback)
        pb = _make_playbook(triggers=["git.commit"])
        manager._active[pb.id] = pb
        manager._index_triggers(pb)
        manager.subscribe_to_events()

        # Should not raise
        await bus.emit("git.commit", {})


# ---------------------------------------------------------------------------
# Test: Cooldown integration in trigger handler
# ---------------------------------------------------------------------------


class TestTriggerCooldownIntegration:
    """Test that the trigger handler respects cooldown."""

    @pytest.mark.asyncio
    async def test_cooldown_prevents_trigger(self) -> None:
        """A playbook on cooldown is skipped when triggered."""
        bus = EventBus(validate_events=False)
        triggered: list[str] = []

        async def on_trigger(playbook: CompiledPlaybook, data: dict) -> None:
            triggered.append(playbook.id)

        manager = PlaybookManager(event_bus=bus, on_trigger=on_trigger)
        pb = _make_playbook(triggers=["git.commit"], cooldown_seconds=300)
        manager._active[pb.id] = pb
        manager._index_triggers(pb)
        manager.subscribe_to_events()

        # Record a recent execution — puts playbook on cooldown
        manager.record_execution(pb.id, "system")

        await bus.emit("git.commit", {})
        assert len(triggered) == 0

    @pytest.mark.asyncio
    async def test_no_cooldown_allows_trigger(self) -> None:
        """A playbook not on cooldown is triggered normally."""
        bus = EventBus(validate_events=False)
        triggered: list[str] = []

        async def on_trigger(playbook: CompiledPlaybook, data: dict) -> None:
            triggered.append(playbook.id)

        manager = PlaybookManager(event_bus=bus, on_trigger=on_trigger)
        pb = _make_playbook(triggers=["git.commit"], cooldown_seconds=300)
        manager._active[pb.id] = pb
        manager._index_triggers(pb)
        manager.subscribe_to_events()

        # No execution recorded — not on cooldown
        await bus.emit("git.commit", {})
        assert len(triggered) == 1

    @pytest.mark.asyncio
    async def test_cleared_cooldown_allows_trigger(self) -> None:
        """After clearing cooldown, the playbook can trigger again."""
        bus = EventBus(validate_events=False)
        triggered: list[str] = []

        async def on_trigger(playbook: CompiledPlaybook, data: dict) -> None:
            triggered.append(playbook.id)

        manager = PlaybookManager(event_bus=bus, on_trigger=on_trigger)
        pb = _make_playbook(triggers=["git.commit"], cooldown_seconds=300)
        manager._active[pb.id] = pb
        manager._index_triggers(pb)
        manager.subscribe_to_events()

        manager.record_execution(pb.id, "system")
        await bus.emit("git.commit", {})
        assert len(triggered) == 0

        manager.clear_cooldown(pb.id)
        await bus.emit("git.commit", {})
        assert len(triggered) == 1


# ---------------------------------------------------------------------------
# Test: Concurrency integration in trigger handler
# ---------------------------------------------------------------------------


class TestTriggerConcurrencyIntegration:
    """Test that the trigger handler respects concurrency limits."""

    @pytest.mark.asyncio
    async def test_at_concurrency_cap_skips_trigger(self) -> None:
        """When at the concurrency cap, triggers are skipped."""
        bus = EventBus(validate_events=False)
        triggered: list[str] = []

        async def on_trigger(playbook: CompiledPlaybook, data: dict) -> None:
            triggered.append(playbook.id)

        manager = PlaybookManager(
            event_bus=bus,
            on_trigger=on_trigger,
            max_concurrent_runs=1,
        )
        pb = _make_playbook(triggers=["git.commit"])
        manager._active[pb.id] = pb
        manager._index_triggers(pb)
        manager.subscribe_to_events()

        # Register a running task to fill the cap
        dummy_task = asyncio.ensure_future(asyncio.sleep(100))
        try:
            manager.register_run("run-1", pb.id, dummy_task)

            await bus.emit("git.commit", {})
            assert len(triggered) == 0
        finally:
            dummy_task.cancel()
            try:
                await dummy_task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_unlimited_concurrency_allows_trigger(self) -> None:
        """With max_concurrent_runs=0 (unlimited), triggers always fire."""
        bus = EventBus(validate_events=False)
        triggered: list[str] = []

        async def on_trigger(playbook: CompiledPlaybook, data: dict) -> None:
            triggered.append(playbook.id)

        manager = PlaybookManager(
            event_bus=bus,
            on_trigger=on_trigger,
            max_concurrent_runs=0,  # unlimited
        )
        pb = _make_playbook(triggers=["git.commit"])
        manager._active[pb.id] = pb
        manager._index_triggers(pb)
        manager.subscribe_to_events()

        await bus.emit("git.commit", {})
        assert len(triggered) == 1


# ---------------------------------------------------------------------------
# Test: Subscription lifecycle on mutations
# ---------------------------------------------------------------------------


class TestSubscriptionLifecycle:
    """Test that subscriptions refresh when playbooks are mutated."""

    @pytest.mark.asyncio
    async def test_load_from_disk_refreshes(self, tmp_path: Path) -> None:
        """load_from_disk refreshes subscriptions when previously subscribed."""
        bus = EventBus(validate_events=False)
        triggered: list[str] = []

        async def on_trigger(playbook: CompiledPlaybook, data: dict) -> None:
            triggered.append(playbook.id)

        manager = PlaybookManager(
            event_bus=bus,
            on_trigger=on_trigger,
            data_dir=str(tmp_path),
        )

        # Subscribe with no playbooks
        manager.subscribe_to_events()
        assert manager.subscription_count == 0

        # Now add a playbook on disk and load
        compiled_dir = tmp_path / "playbooks" / "compiled"
        compiled_dir.mkdir(parents=True)
        pb = _make_playbook(triggers=["git.commit"])
        (compiled_dir / "test-playbook.json").write_text(json.dumps(pb.to_dict()))
        await manager.load_from_disk()

        # Subscriptions should have been refreshed
        assert manager.subscription_count == 1

        await bus.emit("git.commit", {})
        assert len(triggered) == 1

    @pytest.mark.asyncio
    async def test_compile_refreshes(self, tmp_path: Path) -> None:
        """Successful compilation refreshes subscriptions."""
        bus = EventBus(validate_events=False)
        triggered: list[str] = []

        async def on_trigger(playbook: CompiledPlaybook, data: dict) -> None:
            triggered.append(playbook.id)

        provider = _make_mock_provider()
        manager = PlaybookManager(
            chat_provider=provider,
            event_bus=bus,
            on_trigger=on_trigger,
            data_dir=str(tmp_path),
        )
        manager.subscribe_to_events()

        md = _make_playbook_md()
        result = await manager.compile_playbook(md)
        assert result.success

        # Subscriptions should have refreshed
        assert manager.subscription_count >= 1

        await bus.emit("git.commit", {})
        assert len(triggered) == 1

    @pytest.mark.asyncio
    async def test_remove_refreshes(self, tmp_path: Path) -> None:
        """Removing a playbook refreshes subscriptions (removes old ones)."""
        bus = EventBus(validate_events=False)
        triggered: list[str] = []

        async def on_trigger(playbook: CompiledPlaybook, data: dict) -> None:
            triggered.append(playbook.id)

        manager = PlaybookManager(
            event_bus=bus,
            on_trigger=on_trigger,
            data_dir=str(tmp_path),
        )

        pb = _make_playbook(triggers=["git.commit"])
        manager._active[pb.id] = pb
        manager._index_triggers(pb)
        manager.subscribe_to_events()
        assert manager.subscription_count == 1

        await manager.remove_playbook(pb.id)
        assert manager.subscription_count == 0

        # Events should no longer fire
        await bus.emit("git.commit", {})
        assert len(triggered) == 0

    @pytest.mark.asyncio
    async def test_no_refresh_before_first_subscribe(self) -> None:
        """Mutations don't create subscriptions if subscribe_to_events never called."""
        bus = EventBus(validate_events=False)
        manager = PlaybookManager(event_bus=bus)

        # Manually add a playbook (simulates load without subscribe)
        pb = _make_playbook(triggers=["git.commit"])
        manager._active[pb.id] = pb
        manager._index_triggers(pb)

        # _subscribed is False, so _refresh should do nothing
        manager._refresh_subscriptions()
        assert manager.subscription_count == 0

    @pytest.mark.asyncio
    async def test_on_trigger_setter(self) -> None:
        """on_trigger property can be updated at runtime."""
        bus = EventBus(validate_events=False)
        triggered: list[str] = []

        manager = PlaybookManager(event_bus=bus)
        pb = _make_playbook(triggers=["git.commit"])
        manager._active[pb.id] = pb
        manager._index_triggers(pb)
        manager.subscribe_to_events()

        # Initially no callback
        await bus.emit("git.commit", {})
        assert len(triggered) == 0

        # Set callback
        async def on_trigger(playbook: CompiledPlaybook, data: dict) -> None:
            triggered.append(playbook.id)

        manager.on_trigger = on_trigger
        await bus.emit("git.commit", {})
        assert len(triggered) == 1


# ---------------------------------------------------------------------------
# Test: Composition chain scenario (spec §10 example)
# ---------------------------------------------------------------------------


class TestCompositionChain:
    """Test the spec §10 composition chain example end-to-end."""

    @pytest.mark.asyncio
    async def test_composition_chain(self) -> None:
        """git.commit -> code-quality-gate -> post-commit-summary chain.

        Simulates the spec §10 example where:
        1. git.commit triggers code-quality-gate
        2. code-quality-gate completion triggers post-commit-summary
           (filtered on playbook_id match)
        """
        bus = EventBus(validate_events=False)
        triggered: list[tuple[str, dict]] = []

        async def on_trigger(playbook: CompiledPlaybook, data: dict) -> None:
            triggered.append((playbook.id, dict(data)))

        manager = PlaybookManager(event_bus=bus, on_trigger=on_trigger)

        # Playbook 1: code-quality-gate triggers on git.commit
        qg = _make_playbook(
            playbook_id="code-quality-gate",
            triggers=["git.commit"],
        )
        manager._active[qg.id] = qg
        manager._index_triggers(qg)

        # Playbook 2: post-commit-summary triggers on code-quality-gate completion
        summary = _make_playbook(
            playbook_id="post-commit-summary",
            triggers=[
                {
                    "event_type": "playbook.run.completed",
                    "filter": {"playbook_id": "code-quality-gate"},
                }
            ],
        )
        manager._active[summary.id] = summary
        manager._index_triggers(summary)

        manager.subscribe_to_events()

        # Step 1: git.commit fires → code-quality-gate triggered
        await bus.emit("git.commit", {"commit_hash": "abc123"})
        assert len(triggered) == 1
        assert triggered[0][0] == "code-quality-gate"

        # Step 2: code-quality-gate completes → post-commit-summary triggered
        await bus.emit(
            "playbook.run.completed",
            {
                "playbook_id": "code-quality-gate",
                "run_id": "run-1",
                "final_context": {"quality_score": 95},
            },
        )
        assert len(triggered) == 2
        assert triggered[1][0] == "post-commit-summary"
        assert triggered[1][1]["playbook_id"] == "code-quality-gate"

        # Verify non-matching completions don't trigger
        await bus.emit(
            "playbook.run.completed",
            {
                "playbook_id": "some-other-playbook",
                "run_id": "run-2",
            },
        )
        assert len(triggered) == 2  # No new trigger

    @pytest.mark.asyncio
    async def test_multiple_downstream_playbooks(self) -> None:
        """Multiple playbooks can trigger on the same upstream completion."""
        bus = EventBus(validate_events=False)
        triggered: list[str] = []

        async def on_trigger(playbook: CompiledPlaybook, data: dict) -> None:
            triggered.append(playbook.id)

        manager = PlaybookManager(event_bus=bus, on_trigger=on_trigger)

        # Upstream playbook
        upstream = _make_playbook(playbook_id="upstream", triggers=["git.commit"])
        manager._active[upstream.id] = upstream
        manager._index_triggers(upstream)

        # Two downstream playbooks
        ds1 = _make_playbook(
            playbook_id="downstream-1",
            triggers=[
                {
                    "event_type": "playbook.run.completed",
                    "filter": {"playbook_id": "upstream"},
                }
            ],
        )
        ds2 = _make_playbook(
            playbook_id="downstream-2",
            triggers=[
                {
                    "event_type": "playbook.run.completed",
                    "filter": {"playbook_id": "upstream"},
                }
            ],
        )
        for pb in [ds1, ds2]:
            manager._active[pb.id] = pb
            manager._index_triggers(pb)

        manager.subscribe_to_events()

        # Upstream completes
        await bus.emit("playbook.run.completed", {"playbook_id": "upstream"})

        # Both downstream should have triggered
        assert sorted(triggered) == ["downstream-1", "downstream-2"]


# ---------------------------------------------------------------------------
# Test: Trigger map with structured triggers
# ---------------------------------------------------------------------------


class TestTriggerMapWithFilters:
    """Test that trigger map indexing works correctly with structured triggers."""

    def test_trigger_map_uses_event_type(self) -> None:
        """Structured triggers are indexed by event_type in the trigger map."""
        manager = PlaybookManager()
        pb = _make_playbook(
            playbook_id="filtered",
            triggers=[
                {
                    "event_type": "playbook.run.completed",
                    "filter": {"playbook_id": "qg"},
                }
            ],
        )
        manager._active[pb.id] = pb
        manager._index_triggers(pb)

        assert "playbook.run.completed" in manager.trigger_map
        assert manager.trigger_map["playbook.run.completed"] == ["filtered"]

    def test_get_all_triggers_includes_filtered(self) -> None:
        """get_all_triggers includes event types from filtered triggers."""
        manager = PlaybookManager()
        pb = _make_playbook(
            triggers=[
                "git.commit",
                {"event_type": "playbook.run.completed", "filter": {"playbook_id": "qg"}},
            ],
        )
        manager._active[pb.id] = pb
        manager._index_triggers(pb)

        triggers = manager.get_all_triggers()
        assert "git.commit" in triggers
        assert "playbook.run.completed" in triggers

    def test_get_playbooks_by_trigger_with_structured(self) -> None:
        """get_playbooks_by_trigger works for event types from structured triggers."""
        manager = PlaybookManager()
        pb = _make_playbook(
            playbook_id="filtered",
            triggers=[
                {
                    "event_type": "playbook.run.completed",
                    "filter": {"playbook_id": "qg"},
                }
            ],
        )
        manager._active[pb.id] = pb
        manager._index_triggers(pb)

        results = manager.get_playbooks_by_trigger("playbook.run.completed")
        assert len(results) == 1
        assert results[0].id == "filtered"

    @pytest.mark.asyncio
    async def test_load_structured_triggers_from_disk(self, tmp_path: Path) -> None:
        """Playbooks with structured triggers load correctly from disk."""
        compiled_dir = tmp_path / "playbooks" / "compiled"
        compiled_dir.mkdir(parents=True)

        pb = _make_playbook(
            playbook_id="filtered",
            triggers=[
                {
                    "event_type": "playbook.run.completed",
                    "filter": {"playbook_id": "code-quality-gate"},
                }
            ],
        )
        (compiled_dir / "filtered.json").write_text(json.dumps(pb.to_dict()))

        manager = PlaybookManager(data_dir=str(tmp_path))
        loaded = await manager.load_from_disk()
        assert loaded == 1

        active = manager.get_playbook("filtered")
        assert active is not None
        assert active.triggers[0].event_type == "playbook.run.completed"
        assert active.triggers[0].filter == {"playbook_id": "code-quality-gate"}
        assert "playbook.run.completed" in manager.trigger_map
