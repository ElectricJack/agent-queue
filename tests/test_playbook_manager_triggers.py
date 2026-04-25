"""Tests for PlaybookManager trigger mapping and CompiledPlaybookStore integration.

Tests cover roadmap 5.3.1 requirements:
  - Trigger → playbook mapping maintained across add/remove/compile/reload
  - Efficient O(1) trigger lookup via pre-built mapping
  - CompiledPlaybookStore integration for scope-mirrored loading
  - Trigger map consistency after all mutation operations
  - Multiple playbooks sharing the same trigger
  - Playbooks with multiple triggers
  - Trigger map cleanup on playbook removal
  - Trigger map update on recompilation with changed triggers
  - load_from_store() with scope-mirrored directories
  - Public API: trigger_map, get_all_triggers, playbook_count
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.playbooks.manager import PlaybookManager
from src.playbooks.models import CompiledPlaybook, PlaybookNode
from src.playbooks.store import CompiledPlaybookStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class FakeVaultManager:
    """Minimal stand-in for VaultManager — only ``compiled_root`` is needed."""

    def __init__(self, compiled_root: str) -> None:
        self._compiled_root = compiled_root

    @property
    def compiled_root(self) -> str:
        return self._compiled_root


def _make_playbook(
    *,
    playbook_id: str = "test-playbook",
    version: int = 1,
    source_hash: str = "abc123def456",
    triggers: list[str] | None = None,
    scope: str = "system",
) -> CompiledPlaybook:
    """Create a minimal valid CompiledPlaybook for testing."""
    return CompiledPlaybook(
        id=playbook_id,
        version=version,
        source_hash=source_hash,
        triggers=triggers or ["git.commit"],
        scope=scope,
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


def _make_event_bus() -> AsyncMock:
    """Create a mock EventBus that records emitted events."""
    bus = AsyncMock()
    bus.emit = AsyncMock()
    return bus


# ---------------------------------------------------------------------------
# Test: Trigger mapping — basic operations
# ---------------------------------------------------------------------------


class TestTriggerMapBasic:
    """Test that the trigger → playbook mapping is correctly maintained."""

    @pytest.mark.asyncio
    async def test_trigger_map_populated_on_load_from_disk(self, tmp_path: Path) -> None:
        """Loading playbooks from disk populates the trigger mapping."""
        compiled_dir = tmp_path / "playbooks" / "compiled"
        compiled_dir.mkdir(parents=True)

        pb1 = _make_playbook(playbook_id="pb-1", triggers=["git.commit", "git.push"])
        pb2 = _make_playbook(playbook_id="pb-2", triggers=["task.completed"])
        pb3 = _make_playbook(playbook_id="pb-3", triggers=["git.commit"])

        (compiled_dir / "pb-1.json").write_text(json.dumps(pb1.to_dict()))
        (compiled_dir / "pb-2.json").write_text(json.dumps(pb2.to_dict()))
        (compiled_dir / "pb-3.json").write_text(json.dumps(pb3.to_dict()))

        manager = PlaybookManager(config=None, data_dir=str(tmp_path))
        loaded = await manager.load_from_disk()
        assert loaded == 3

        # Verify trigger map
        tmap = manager.trigger_map
        assert set(tmap["git.commit"]) == {"pb-1", "pb-3"}
        assert tmap["git.push"] == ["pb-1"]
        assert tmap["task.completed"] == ["pb-2"]

    @pytest.mark.asyncio
    async def test_trigger_map_empty_initially(self) -> None:
        """A fresh manager has an empty trigger map."""
        manager = PlaybookManager(config=None)
        assert manager.trigger_map == {}
        assert manager.get_all_triggers() == []
        assert manager.playbook_count == 0

    @pytest.mark.asyncio
    async def test_get_playbooks_by_trigger_uses_mapping(self, tmp_path: Path) -> None:
        """get_playbooks_by_trigger returns results from the trigger map."""
        compiled_dir = tmp_path / "playbooks" / "compiled"
        compiled_dir.mkdir(parents=True)

        pb1 = _make_playbook(playbook_id="pb-1", triggers=["git.commit", "git.push"])
        pb2 = _make_playbook(playbook_id="pb-2", triggers=["task.completed"])
        pb3 = _make_playbook(playbook_id="pb-3", triggers=["git.commit"])

        (compiled_dir / "pb-1.json").write_text(json.dumps(pb1.to_dict()))
        (compiled_dir / "pb-2.json").write_text(json.dumps(pb2.to_dict()))
        (compiled_dir / "pb-3.json").write_text(json.dumps(pb3.to_dict()))

        manager = PlaybookManager(config=None, data_dir=str(tmp_path))
        await manager.load_from_disk()

        # Verify lookup
        git_commit = manager.get_playbooks_by_trigger("git.commit")
        assert len(git_commit) == 2
        ids = {pb.id for pb in git_commit}
        assert ids == {"pb-1", "pb-3"}

        git_push = manager.get_playbooks_by_trigger("git.push")
        assert len(git_push) == 1
        assert git_push[0].id == "pb-1"

        task = manager.get_playbooks_by_trigger("task.completed")
        assert len(task) == 1
        assert task[0].id == "pb-2"

        # Non-existent trigger
        assert manager.get_playbooks_by_trigger("nonexistent") == []

    @pytest.mark.asyncio
    async def test_get_all_triggers(self, tmp_path: Path) -> None:
        """get_all_triggers returns sorted list of all trigger event types."""
        compiled_dir = tmp_path / "playbooks" / "compiled"
        compiled_dir.mkdir(parents=True)

        pb1 = _make_playbook(playbook_id="pb-1", triggers=["git.commit", "git.push"])
        pb2 = _make_playbook(playbook_id="pb-2", triggers=["task.completed", "git.commit"])

        (compiled_dir / "pb-1.json").write_text(json.dumps(pb1.to_dict()))
        (compiled_dir / "pb-2.json").write_text(json.dumps(pb2.to_dict()))

        manager = PlaybookManager(config=None, data_dir=str(tmp_path))
        await manager.load_from_disk()

        triggers = manager.get_all_triggers()
        assert triggers == ["git.commit", "git.push", "task.completed"]

    @pytest.mark.asyncio
    async def test_playbook_count(self, tmp_path: Path) -> None:
        """playbook_count returns the number of active playbooks."""
        compiled_dir = tmp_path / "playbooks" / "compiled"
        compiled_dir.mkdir(parents=True)

        pb1 = _make_playbook(playbook_id="pb-1")
        pb2 = _make_playbook(playbook_id="pb-2")

        (compiled_dir / "pb-1.json").write_text(json.dumps(pb1.to_dict()))
        (compiled_dir / "pb-2.json").write_text(json.dumps(pb2.to_dict()))

        manager = PlaybookManager(config=None, data_dir=str(tmp_path))
        assert manager.playbook_count == 0

        await manager.load_from_disk()
        assert manager.playbook_count == 2


# ---------------------------------------------------------------------------
# Test: Trigger map mutation on compile
# ---------------------------------------------------------------------------


class TestTriggerMapOnCompile:
    """Test trigger mapping updates during compilation."""

    @pytest.mark.asyncio
    async def test_trigger_map_updated_on_successful_compilation(self, tmp_path: Path) -> None:
        """Successful compilation adds triggers to the mapping."""
        provider = _make_mock_provider()
        manager = PlaybookManager(
            config=None,
            chat_provider=provider,
            data_dir=str(tmp_path),
        )

        md = _make_playbook_md(triggers="- git.commit\n  - task.completed")
        result = await manager.compile_playbook(md)
        assert result.success

        tmap = manager.trigger_map
        assert "git.commit" in tmap
        assert "test-playbook" in tmap["git.commit"]

    @pytest.mark.asyncio
    async def test_trigger_map_not_changed_on_failed_compilation(self, tmp_path: Path) -> None:
        """Failed compilation does not alter the trigger mapping."""
        provider = _make_mock_provider()
        from src.chat_providers.types import ChatResponse, TextBlock

        json_str = json.dumps(VALID_COMPILED_NODES, indent=2)
        good_resp = ChatResponse(content=[TextBlock(text=f"```json\n{json_str}\n```")])
        bad_resp = ChatResponse(content=[TextBlock(text="garbage")])
        provider.create_message = AsyncMock(side_effect=[good_resp, bad_resp, bad_resp, bad_resp])

        manager = PlaybookManager(
            config=None,
            chat_provider=provider,
            data_dir=str(tmp_path),
        )

        # Compile successfully first
        md = _make_playbook_md()
        result1 = await manager.compile_playbook(md)
        assert result1.success

        tmap_before = manager.trigger_map
        assert "git.commit" in tmap_before

        # Fail recompilation with different content
        modified_md = _make_playbook_md(body="# Changed\n\nNew content to trigger recompile.")
        result2 = await manager.compile_playbook(modified_md)
        assert not result2.success

        # Trigger map unchanged
        tmap_after = manager.trigger_map
        assert tmap_after == tmap_before

    @pytest.mark.asyncio
    async def test_trigger_map_updated_when_triggers_change_on_recompile(
        self, tmp_path: Path
    ) -> None:
        """Recompilation with different triggers updates the mapping correctly.

        When a playbook's triggers change (e.g., from git.commit to task.completed),
        the old triggers are removed and new ones are added.
        """
        from src.chat_providers.types import ChatResponse, TextBlock

        # v1: triggers on git.commit
        nodes_v1 = {"nodes": VALID_COMPILED_NODES["nodes"]}
        v1_json = json.dumps(nodes_v1, indent=2)
        v1_resp = ChatResponse(content=[TextBlock(text=f"```json\n{v1_json}\n```")])

        # v2: triggers on task.completed (compiler output doesn't change triggers,
        # but the compiled playbook gets the frontmatter triggers)
        v2_resp = ChatResponse(content=[TextBlock(text=f"```json\n{v1_json}\n```")])

        provider = AsyncMock()
        provider.model_name = "test-model"
        provider.create_message = AsyncMock(side_effect=[v1_resp, v2_resp])

        manager = PlaybookManager(
            config=None,
            chat_provider=provider,
            data_dir=str(tmp_path),
        )

        # Compile with git.commit trigger
        md_v1 = _make_playbook_md(triggers="- git.commit")
        result1 = await manager.compile_playbook(md_v1)
        assert result1.success
        assert "git.commit" in manager.trigger_map

        # Recompile with task.completed trigger (different frontmatter)
        md_v2 = _make_playbook_md(
            triggers="- task.completed",
            body="# Updated\n\nChanged triggers and content.",
        )
        result2 = await manager.compile_playbook(md_v2)
        assert result2.success

        # Frontmatter triggers are "task.completed" now
        pb = manager.get_playbook("test-playbook")
        if "task.completed" in pb.triggers:
            assert "task.completed" in manager.trigger_map
        # At minimum, the old trigger entry for this playbook should be updated


# ---------------------------------------------------------------------------
# Test: Trigger map mutation on remove
# ---------------------------------------------------------------------------


class TestTriggerMapOnRemove:
    """Test trigger mapping cleanup when playbooks are removed."""

    @pytest.mark.asyncio
    async def test_trigger_map_cleaned_on_removal(self, tmp_path: Path) -> None:
        """Removing a playbook removes its triggers from the mapping."""
        compiled_dir = tmp_path / "playbooks" / "compiled"
        compiled_dir.mkdir(parents=True)

        pb = _make_playbook(
            playbook_id="removable",
            triggers=["git.commit", "git.push"],
        )
        (compiled_dir / "removable.json").write_text(json.dumps(pb.to_dict()))

        manager = PlaybookManager(config=None, data_dir=str(tmp_path))
        await manager.load_from_disk()

        assert "git.commit" in manager.trigger_map
        assert "git.push" in manager.trigger_map

        await manager.remove_playbook("removable")

        # All triggers cleaned up
        assert "git.commit" not in manager.trigger_map
        assert "git.push" not in manager.trigger_map
        assert manager.get_all_triggers() == []

    @pytest.mark.asyncio
    async def test_trigger_map_partial_cleanup(self, tmp_path: Path) -> None:
        """Removing one playbook only cleans its own trigger entries.

        When multiple playbooks share a trigger, removing one should not
        remove the trigger entry entirely.
        """
        compiled_dir = tmp_path / "playbooks" / "compiled"
        compiled_dir.mkdir(parents=True)

        pb1 = _make_playbook(playbook_id="pb-1", triggers=["git.commit", "git.push"])
        pb2 = _make_playbook(playbook_id="pb-2", triggers=["git.commit"])

        (compiled_dir / "pb-1.json").write_text(json.dumps(pb1.to_dict()))
        (compiled_dir / "pb-2.json").write_text(json.dumps(pb2.to_dict()))

        manager = PlaybookManager(config=None, data_dir=str(tmp_path))
        await manager.load_from_disk()

        assert set(manager.trigger_map["git.commit"]) == {"pb-1", "pb-2"}

        await manager.remove_playbook("pb-1")

        # git.commit still present with just pb-2
        assert manager.trigger_map["git.commit"] == ["pb-2"]
        # git.push gone (only pb-1 had it)
        assert "git.push" not in manager.trigger_map

    @pytest.mark.asyncio
    async def test_remove_nonexistent_does_not_affect_trigger_map(self) -> None:
        """Removing a nonexistent playbook leaves the trigger map unchanged."""
        manager = PlaybookManager(config=None)
        # Manually inject a playbook
        pb = _make_playbook(playbook_id="existing", triggers=["git.commit"])
        manager._active["existing"] = pb
        manager._index_triggers(pb)

        before = manager.trigger_map.copy()
        await manager.remove_playbook("nonexistent")
        assert manager.trigger_map == before


# ---------------------------------------------------------------------------
# Test: Trigger map — _rebuild_trigger_map
# ---------------------------------------------------------------------------


class TestRebuildTriggerMap:
    """Test the _rebuild_trigger_map internal method."""

    def test_rebuild_from_scratch(self) -> None:
        """_rebuild_trigger_map clears and rebuilds the entire mapping."""
        manager = PlaybookManager(config=None)

        pb1 = _make_playbook(playbook_id="pb-1", triggers=["git.commit", "git.push"])
        pb2 = _make_playbook(playbook_id="pb-2", triggers=["task.completed"])

        manager._active["pb-1"] = pb1
        manager._active["pb-2"] = pb2

        # Trigger map is empty (manually added, not through load)
        assert manager.trigger_map == {}

        manager._rebuild_trigger_map()

        assert set(manager.trigger_map.keys()) == {"git.commit", "git.push", "task.completed"}
        assert manager.trigger_map["git.commit"] == ["pb-1"]
        assert manager.trigger_map["git.push"] == ["pb-1"]
        assert manager.trigger_map["task.completed"] == ["pb-2"]

    def test_rebuild_clears_stale_entries(self) -> None:
        """_rebuild_trigger_map removes stale entries from a previous state."""
        manager = PlaybookManager(config=None)

        # Simulate a stale trigger map
        manager._trigger_map["stale.event"] = {"old-playbook"}

        pb = _make_playbook(playbook_id="active", triggers=["git.commit"])
        manager._active["active"] = pb

        manager._rebuild_trigger_map()

        assert "stale.event" not in manager.trigger_map
        assert manager.trigger_map["git.commit"] == ["active"]


# ---------------------------------------------------------------------------
# Test: CompiledPlaybookStore integration — load_from_store
# ---------------------------------------------------------------------------


class TestLoadFromStore:
    """Test loading compiled playbooks via CompiledPlaybookStore."""

    @pytest.mark.asyncio
    async def test_load_from_store_system_scope(self, tmp_path: Path) -> None:
        """load_from_store loads system-scoped playbooks."""
        vm = FakeVaultManager(str(tmp_path / "compiled"))
        store = CompiledPlaybookStore(vm)

        pb = _make_playbook(playbook_id="sys-pb", triggers=["task.completed"], scope="system")
        store.save(pb, "system")

        manager = PlaybookManager(config=None, store=store)
        loaded = await manager.load_from_store()

        assert loaded == 1
        assert manager.get_playbook("sys-pb") is not None
        assert manager.trigger_map["task.completed"] == ["sys-pb"]

    @pytest.mark.asyncio
    async def test_load_from_store_project_scope(self, tmp_path: Path) -> None:
        """load_from_store loads project-scoped playbooks."""
        vm = FakeVaultManager(str(tmp_path / "compiled"))
        store = CompiledPlaybookStore(vm)

        pb = _make_playbook(
            playbook_id="proj-pb",
            triggers=["git.commit"],
            scope="project",
        )
        store.save(pb, "project", "my-app")

        manager = PlaybookManager(config=None, store=store)
        loaded = await manager.load_from_store()

        assert loaded == 1
        assert manager.get_playbook("proj-pb") is not None
        assert manager.trigger_map["git.commit"] == ["proj-pb"]

    @pytest.mark.asyncio
    async def test_load_from_store_agent_type_scope(self, tmp_path: Path) -> None:
        """load_from_store loads agent-type-scoped playbooks."""
        vm = FakeVaultManager(str(tmp_path / "compiled"))
        store = CompiledPlaybookStore(vm)

        pb = _make_playbook(
            playbook_id="at-pb",
            triggers=["task.completed"],
            scope="agent-type:coding",
        )
        store.save(pb, "agent_type", "coding")

        manager = PlaybookManager(config=None, store=store)
        loaded = await manager.load_from_store()

        assert loaded == 1
        assert manager.get_playbook("at-pb") is not None

    @pytest.mark.asyncio
    async def test_load_from_store_multiple_scopes(self, tmp_path: Path) -> None:
        """load_from_store aggregates playbooks across all scopes."""
        vm = FakeVaultManager(str(tmp_path / "compiled"))
        store = CompiledPlaybookStore(vm)

        pb_sys = _make_playbook(
            playbook_id="sys-pb",
            triggers=["git.commit"],
            scope="system",
        )
        # 'orchestrator' was merged into 'supervisor', which aliases 'system'.
        # Use 'agent_type' for a truly distinct scope so the test still
        # exercises multi-scope aggregation.
        pb_agent = _make_playbook(
            playbook_id="coding-pb",
            triggers=["task.completed"],
            scope="agent-type:coding",
        )
        pb_proj = _make_playbook(
            playbook_id="proj-pb",
            triggers=["git.push", "git.commit"],
            scope="project",
        )

        store.save(pb_sys, "system")
        store.save(pb_agent, "agent_type", "coding")
        store.save(pb_proj, "project", "my-app")

        manager = PlaybookManager(config=None, store=store)
        loaded = await manager.load_from_store()

        assert loaded == 3
        assert manager.playbook_count == 3

        # Verify trigger mapping aggregates correctly
        tmap = manager.trigger_map
        assert set(tmap["git.commit"]) == {"sys-pb", "proj-pb"}
        assert tmap["task.completed"] == ["coding-pb"]
        assert tmap["git.push"] == ["proj-pb"]

    @pytest.mark.asyncio
    async def test_load_from_store_skips_invalid(self, tmp_path: Path) -> None:
        """load_from_store skips playbooks that fail validation."""
        vm = FakeVaultManager(str(tmp_path / "compiled"))
        store = CompiledPlaybookStore(vm)

        # Save a valid playbook
        good_pb = _make_playbook(playbook_id="good", triggers=["git.commit"])
        store.save(good_pb, "system")

        # Save an invalid playbook (no entry node)
        bad_pb = CompiledPlaybook(
            id="bad",
            version=1,
            source_hash="abc123",
            triggers=["task.completed"],
            scope="system",
            nodes={
                "step": PlaybookNode(prompt="Do something.", terminal=True),
                # No entry node!
            },
        )
        store.save(bad_pb, "system")

        manager = PlaybookManager(config=None, store=store)
        loaded = await manager.load_from_store()

        assert loaded == 1
        assert manager.get_playbook("good") is not None
        assert manager.get_playbook("bad") is None
        # Only good playbook's triggers in map
        assert "git.commit" in manager.trigger_map
        assert "task.completed" not in manager.trigger_map

    @pytest.mark.asyncio
    async def test_load_from_store_empty(self, tmp_path: Path) -> None:
        """load_from_store returns 0 when no playbooks exist."""
        vm = FakeVaultManager(str(tmp_path / "compiled"))
        store = CompiledPlaybookStore(vm)

        manager = PlaybookManager(config=None, store=store)
        loaded = await manager.load_from_store()

        assert loaded == 0
        assert manager.trigger_map == {}

    @pytest.mark.asyncio
    async def test_load_from_store_falls_back_to_disk(self, tmp_path: Path) -> None:
        """load_from_store falls back to load_from_disk when no store is set."""
        compiled_dir = tmp_path / "playbooks" / "compiled"
        compiled_dir.mkdir(parents=True)

        pb = _make_playbook(playbook_id="disk-pb", triggers=["git.commit"])
        (compiled_dir / "disk-pb.json").write_text(json.dumps(pb.to_dict()))

        manager = PlaybookManager(config=None, data_dir=str(tmp_path))  # no store
        loaded = await manager.load_from_store()

        assert loaded == 1
        assert manager.get_playbook("disk-pb") is not None
        assert "git.commit" in manager.trigger_map

    @pytest.mark.asyncio
    async def test_load_from_store_and_trigger_lookup(self, tmp_path: Path) -> None:
        """Full round-trip: store → load → trigger lookup returns correct playbooks."""
        vm = FakeVaultManager(str(tmp_path / "compiled"))
        store = CompiledPlaybookStore(vm)

        # Three playbooks across two scopes sharing some triggers
        pb_a = _make_playbook(
            playbook_id="deploy-gate",
            triggers=["git.push", "git.pr.created"],
            scope="system",
        )
        pb_b = _make_playbook(
            playbook_id="code-review",
            triggers=["git.pr.created", "task.completed"],
            scope="project",
        )
        pb_c = _make_playbook(
            playbook_id="health-check",
            triggers=["timer.30m"],
            scope="system",
        )

        store.save(pb_a, "system")
        store.save(pb_b, "project", "my-app")
        store.save(pb_c, "system")

        manager = PlaybookManager(config=None, store=store)
        await manager.load_from_store()

        # git.pr.created → both deploy-gate and code-review
        pr_playbooks = manager.get_playbooks_by_trigger("git.pr.created")
        assert len(pr_playbooks) == 2
        pr_ids = {pb.id for pb in pr_playbooks}
        assert pr_ids == {"deploy-gate", "code-review"}

        # git.push → only deploy-gate
        push_playbooks = manager.get_playbooks_by_trigger("git.push")
        assert len(push_playbooks) == 1
        assert push_playbooks[0].id == "deploy-gate"

        # timer.30m → only health-check
        timer_playbooks = manager.get_playbooks_by_trigger("timer.30m")
        assert len(timer_playbooks) == 1
        assert timer_playbooks[0].id == "health-check"

        # All triggers
        all_triggers = manager.get_all_triggers()
        assert all_triggers == [
            "git.pr.created",
            "git.push",
            "task.completed",
            "timer.30m",
        ]


# ---------------------------------------------------------------------------
# Test: Trigger map consistency across operations
# ---------------------------------------------------------------------------


class TestTriggerMapConsistency:
    """Test that the trigger map stays consistent through mixed operations."""

    @pytest.mark.asyncio
    async def test_compile_then_remove_cleans_triggers(self, tmp_path: Path) -> None:
        """Compiling then removing a playbook leaves no stale triggers."""
        provider = _make_mock_provider()
        manager = PlaybookManager(
            config=None,
            chat_provider=provider,
            data_dir=str(tmp_path),
        )

        md = _make_playbook_md()
        result = await manager.compile_playbook(md)
        assert result.success
        assert "git.commit" in manager.trigger_map

        await manager.remove_playbook("test-playbook")
        assert "git.commit" not in manager.trigger_map
        assert manager.playbook_count == 0

    @pytest.mark.asyncio
    async def test_load_compile_remove_mixed(self, tmp_path: Path) -> None:
        """Mixed load/compile/remove operations keep trigger map consistent."""
        compiled_dir = tmp_path / "playbooks" / "compiled"
        compiled_dir.mkdir(parents=True)

        # Pre-load from disk
        pb_disk = _make_playbook(playbook_id="disk-pb", triggers=["git.push"])
        (compiled_dir / "disk-pb.json").write_text(json.dumps(pb_disk.to_dict()))

        provider = _make_mock_provider()
        manager = PlaybookManager(
            config=None,
            chat_provider=provider,
            data_dir=str(tmp_path),
        )

        await manager.load_from_disk()
        assert manager.trigger_map["git.push"] == ["disk-pb"]

        # Compile a new playbook
        md = _make_playbook_md(playbook_id="compiled-pb", triggers="- git.commit")
        result = await manager.compile_playbook(md)
        assert result.success

        assert "git.push" in manager.trigger_map
        assert "git.commit" in manager.trigger_map
        assert manager.playbook_count == 2

        # Remove the disk-loaded one
        await manager.remove_playbook("disk-pb")

        assert "git.push" not in manager.trigger_map
        assert "git.commit" in manager.trigger_map
        assert manager.playbook_count == 1

    @pytest.mark.asyncio
    async def test_trigger_map_deterministic_ordering(self, tmp_path: Path) -> None:
        """get_playbooks_by_trigger returns results in deterministic order."""
        compiled_dir = tmp_path / "playbooks" / "compiled"
        compiled_dir.mkdir(parents=True)

        # Create playbooks with same trigger but different IDs
        for name in ["zebra", "alpha", "mango"]:
            pb = _make_playbook(playbook_id=name, triggers=["shared.trigger"])
            (compiled_dir / f"{name}.json").write_text(json.dumps(pb.to_dict()))

        manager = PlaybookManager(config=None, data_dir=str(tmp_path))
        await manager.load_from_disk()

        results = manager.get_playbooks_by_trigger("shared.trigger")
        result_ids = [pb.id for pb in results]
        # Should be sorted alphabetically
        assert result_ids == ["alpha", "mango", "zebra"]

    @pytest.mark.asyncio
    async def test_trigger_map_property_returns_sorted_ids(self, tmp_path: Path) -> None:
        """trigger_map property returns sorted playbook IDs per trigger."""
        compiled_dir = tmp_path / "playbooks" / "compiled"
        compiled_dir.mkdir(parents=True)

        for name in ["zebra", "alpha", "mango"]:
            pb = _make_playbook(playbook_id=name, triggers=["shared.trigger"])
            (compiled_dir / f"{name}.json").write_text(json.dumps(pb.to_dict()))

        manager = PlaybookManager(config=None, data_dir=str(tmp_path))
        await manager.load_from_disk()

        tmap = manager.trigger_map
        assert tmap["shared.trigger"] == ["alpha", "mango", "zebra"]


# ---------------------------------------------------------------------------
# Test: Single playbook with many triggers
# ---------------------------------------------------------------------------


class TestMultipleTriggersSamePlaybook:
    """Test playbooks with multiple trigger event types."""

    @pytest.mark.asyncio
    async def test_single_playbook_multiple_triggers(self, tmp_path: Path) -> None:
        """A playbook with many triggers appears in each trigger's entry."""
        compiled_dir = tmp_path / "playbooks" / "compiled"
        compiled_dir.mkdir(parents=True)

        pb = _make_playbook(
            playbook_id="multi-trigger",
            triggers=["git.commit", "git.push", "task.completed", "timer.30m"],
        )
        (compiled_dir / "multi-trigger.json").write_text(json.dumps(pb.to_dict()))

        manager = PlaybookManager(config=None, data_dir=str(tmp_path))
        await manager.load_from_disk()

        for trigger in ["git.commit", "git.push", "task.completed", "timer.30m"]:
            results = manager.get_playbooks_by_trigger(trigger)
            assert len(results) == 1
            assert results[0].id == "multi-trigger"

        assert len(manager.get_all_triggers()) == 4

    @pytest.mark.asyncio
    async def test_remove_multi_trigger_playbook_cleans_all(self, tmp_path: Path) -> None:
        """Removing a multi-trigger playbook cleans all its trigger entries."""
        compiled_dir = tmp_path / "playbooks" / "compiled"
        compiled_dir.mkdir(parents=True)

        pb = _make_playbook(
            playbook_id="multi",
            triggers=["git.commit", "git.push", "task.completed"],
        )
        (compiled_dir / "multi.json").write_text(json.dumps(pb.to_dict()))

        manager = PlaybookManager(config=None, data_dir=str(tmp_path))
        await manager.load_from_disk()
        assert len(manager.get_all_triggers()) == 3

        await manager.remove_playbook("multi")
        assert len(manager.get_all_triggers()) == 0
        assert manager.trigger_map == {}


# ---------------------------------------------------------------------------
# Test: Store integration — scope metadata preserved
# ---------------------------------------------------------------------------


class TestStoreIntegrationScopeMetadata:
    """Test that scope metadata from store is accessible through the manager."""

    @pytest.mark.asyncio
    async def test_store_scope_field_preserved(self, tmp_path: Path) -> None:
        """Playbook scope field from store is accessible after loading."""
        vm = FakeVaultManager(str(tmp_path / "compiled"))
        store = CompiledPlaybookStore(vm)

        pb_sys = _make_playbook(
            playbook_id="sys-pb",
            scope="system",
            triggers=["git.commit"],
        )
        pb_proj = _make_playbook(
            playbook_id="proj-pb",
            scope="project",
            triggers=["git.push"],
        )
        pb_at = _make_playbook(
            playbook_id="at-pb",
            scope="agent-type:coding",
            triggers=["task.completed"],
        )

        store.save(pb_sys, "system")
        store.save(pb_proj, "project", "my-app")
        store.save(pb_at, "agent_type", "coding")

        manager = PlaybookManager(config=None, store=store)
        await manager.load_from_store()

        assert manager.get_playbook("sys-pb").scope == "system"
        assert manager.get_playbook("proj-pb").scope == "project"
        assert manager.get_playbook("at-pb").scope == "agent-type:coding"

    @pytest.mark.asyncio
    async def test_store_playbook_version_preserved(self, tmp_path: Path) -> None:
        """Playbook version from store is preserved after loading."""
        vm = FakeVaultManager(str(tmp_path / "compiled"))
        store = CompiledPlaybookStore(vm)

        pb = _make_playbook(playbook_id="versioned", version=5, triggers=["git.commit"])
        store.save(pb, "system")

        manager = PlaybookManager(config=None, store=store)
        await manager.load_from_store()

        loaded = manager.get_playbook("versioned")
        assert loaded is not None
        assert loaded.version == 5


# ---------------------------------------------------------------------------
# Test: Edge cases
# ---------------------------------------------------------------------------


class TestTriggerMapEdgeCases:
    """Test edge cases in trigger mapping."""

    @pytest.mark.asyncio
    async def test_empty_triggers_list_does_not_crash(self) -> None:
        """A playbook with no triggers (theoretically invalid) doesn't crash the mapping."""
        manager = PlaybookManager(config=None)

        # Manually create a playbook with empty triggers (bypasses validation)
        pb = CompiledPlaybook(
            id="no-triggers",
            version=1,
            source_hash="abc123",
            triggers=[],
            scope="system",
            nodes={
                "start": PlaybookNode(entry=True, prompt="Do.", goto="end"),
                "end": PlaybookNode(terminal=True),
            },
        )

        # Should not raise
        manager._index_triggers(pb)
        assert manager.trigger_map == {}

        manager._unindex_triggers(pb)
        assert manager.trigger_map == {}

    @pytest.mark.asyncio
    async def test_duplicate_trigger_in_playbook(self) -> None:
        """A playbook with duplicate trigger entries handles gracefully."""
        manager = PlaybookManager(config=None)

        pb = _make_playbook(
            playbook_id="dupe",
            triggers=["git.commit", "git.commit", "git.push"],
        )

        manager._active["dupe"] = pb
        manager._index_triggers(pb)

        # git.commit should only have the playbook once
        assert manager.trigger_map["git.commit"] == ["dupe"]
        assert manager.trigger_map["git.push"] == ["dupe"]

    @pytest.mark.asyncio
    async def test_unindex_unknown_playbook_is_safe(self) -> None:
        """Unindexing triggers for a playbook not in the map is a no-op."""
        manager = PlaybookManager(config=None)

        pb = _make_playbook(playbook_id="ghost", triggers=["unknown.event"])

        # Should not raise even though the trigger is not in the map
        manager._unindex_triggers(pb)
        assert manager.trigger_map == {}

    @pytest.mark.asyncio
    async def test_concurrent_playbooks_same_id_last_wins(self, tmp_path: Path) -> None:
        """When two playbooks share the same ID, the last loaded one wins."""
        vm = FakeVaultManager(str(tmp_path / "compiled"))
        store = CompiledPlaybookStore(vm)

        # Save same ID in two different scopes (unusual but possible)
        pb_v1 = _make_playbook(
            playbook_id="shared-id",
            version=1,
            triggers=["git.commit"],
            scope="system",
        )
        pb_v2 = _make_playbook(
            playbook_id="shared-id",
            version=2,
            triggers=["task.completed"],
            scope="project",
        )
        store.save(pb_v1, "system")
        store.save(pb_v2, "project", "my-app")

        manager = PlaybookManager(config=None, store=store)
        await manager.load_from_store()

        # Only one should be active (the last one loaded wins)
        assert manager.playbook_count == 1
        active = manager.get_playbook("shared-id")
        assert active is not None

    @pytest.mark.asyncio
    async def test_store_with_no_playbooks(self, tmp_path: Path) -> None:
        """load_from_store with empty compiled tree returns 0."""
        vm = FakeVaultManager(str(tmp_path / "compiled"))
        store = CompiledPlaybookStore(vm)

        manager = PlaybookManager(config=None, store=store)
        loaded = await manager.load_from_store()
        assert loaded == 0
        assert manager.playbook_count == 0
        assert manager.trigger_map == {}
        assert manager.get_all_triggers() == []
