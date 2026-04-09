"""Tests for PlaybookManager — compilation error handling and version management.

Tests cover roadmap 5.1.9 requirements (error handling):
  (a) Markdown with no recognizable node structure produces compilation error notification
  (b) Previous valid compiled JSON is retained on disk after failed recompilation
  (c) PlaybookManager continues to use the previous version for event matching
  (d) Error notification includes file path and LLM/validation error details
  (e) Markdown with valid structure but LLM provider failure retains previous version
  (f) Partially valid markdown fails entire compilation (atomic — no partial updates)
  (g) Fixing markdown and saving again triggers successful recompilation

Also tests:
  - PlaybookManager startup loading from disk
  - Playbook deletion from active registry
  - Compilation success notification events
  - No-provider fallback (log-only mode)
  - Source-hash change detection (roadmap 5.1.5)
  - Lookup by trigger
  - PlaybookHandler integration (vault change handler)
  - Frontmatter validation errors (pre-LLM)
  - Notification event models
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.playbook_manager import PlaybookManager
from src.playbook_models import CompiledPlaybook, PlaybookNode


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


SIMPLE_PLAYBOOK_MD = _make_playbook_md()

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


def _make_failing_provider(error_msg: str = "LLM unavailable") -> AsyncMock:
    """Create a mock ChatProvider that raises on every call."""
    provider = AsyncMock()
    provider.model_name = "test-model"
    provider.create_message = AsyncMock(side_effect=Exception(error_msg))
    return provider


def _make_event_bus() -> AsyncMock:
    """Create a mock EventBus that records emitted events."""
    bus = AsyncMock()
    bus.emit = AsyncMock()
    return bus


# ---------------------------------------------------------------------------
# Test: Compilation success
# ---------------------------------------------------------------------------


class TestCompilationSuccess:
    """Test successful compilation updates the active version."""

    @pytest.mark.asyncio
    async def test_successful_compilation_activates_new_version(self, tmp_path: Path) -> None:
        """Successful compilation adds the playbook to active registry."""
        provider = _make_mock_provider()
        bus = _make_event_bus()
        manager = PlaybookManager(
            chat_provider=provider,
            event_bus=bus,
            data_dir=str(tmp_path),
        )

        result = await manager.compile_playbook(
            SIMPLE_PLAYBOOK_MD,
            source_path="/vault/system/playbooks/test.md",
        )

        assert result.success
        assert result.playbook is not None
        active = manager.get_playbook("test-playbook")
        assert active is not None
        assert active.version == 1
        assert active.id == "test-playbook"

    @pytest.mark.asyncio
    async def test_successful_compilation_persists_to_disk(self, tmp_path: Path) -> None:
        """Compiled playbook JSON is written to the data directory."""
        provider = _make_mock_provider()
        manager = PlaybookManager(
            chat_provider=provider,
            data_dir=str(tmp_path),
        )

        await manager.compile_playbook(SIMPLE_PLAYBOOK_MD)

        json_path = tmp_path / "playbooks" / "compiled" / "test-playbook.json"
        assert json_path.exists()
        data = json.loads(json_path.read_text())
        assert data["id"] == "test-playbook"
        assert data["version"] == 1

    @pytest.mark.asyncio
    async def test_successful_compilation_emits_success_event(self, tmp_path: Path) -> None:
        """A compilation_succeeded notification is emitted on success."""
        provider = _make_mock_provider()
        bus = _make_event_bus()
        manager = PlaybookManager(
            chat_provider=provider,
            event_bus=bus,
            data_dir=str(tmp_path),
        )

        await manager.compile_playbook(
            SIMPLE_PLAYBOOK_MD,
            source_path="/vault/system/playbooks/test.md",
        )

        bus.emit.assert_called_once()
        event_type, payload = bus.emit.call_args[0]
        assert event_type == "notify.playbook_compilation_succeeded"
        assert payload["playbook_id"] == "test-playbook"
        assert payload["version"] == 1
        assert payload["source_path"] == "/vault/system/playbooks/test.md"

    @pytest.mark.asyncio
    async def test_version_increments_on_recompilation(self, tmp_path: Path) -> None:
        """Recompiling with changed markdown increments the version number."""
        provider = _make_mock_provider()
        # Need two responses for two compilations
        from src.chat_providers.types import ChatResponse, TextBlock

        json_str = json.dumps(VALID_COMPILED_NODES, indent=2)
        resp = ChatResponse(content=[TextBlock(text=f"```json\n{json_str}\n```")])
        provider.create_message = AsyncMock(side_effect=[resp, resp])

        manager = PlaybookManager(
            chat_provider=provider,
            data_dir=str(tmp_path),
        )

        await manager.compile_playbook(SIMPLE_PLAYBOOK_MD)
        assert manager.get_playbook("test-playbook").version == 1

        # Use different markdown body to trigger recompilation (source hash differs)
        modified_md = _make_playbook_md(body="# Updated\n\nDo something different then finish.")
        await manager.compile_playbook(modified_md)
        assert manager.get_playbook("test-playbook").version == 2


# ---------------------------------------------------------------------------
# Test: Compilation failure — previous version retained
# ---------------------------------------------------------------------------


class TestCompilationFailureRetainsPrevious:
    """Test that failed compilation keeps the previous version active.

    Covers roadmap 5.1.9 test cases (a)-(f).
    """

    @pytest.mark.asyncio
    async def test_previous_version_stays_active_on_failure(self, tmp_path: Path) -> None:
        """(b)(c) Previous compiled version remains active after failed recompilation."""
        # First, compile a valid playbook
        provider = _make_mock_provider()
        from src.chat_providers.types import ChatResponse, TextBlock

        json_str = json.dumps(VALID_COMPILED_NODES, indent=2)
        good_resp = ChatResponse(content=[TextBlock(text=f"```json\n{json_str}\n```")])
        # Subsequent calls return garbage
        bad_resp = ChatResponse(content=[TextBlock(text="not json at all")])
        provider.create_message = AsyncMock(side_effect=[good_resp, bad_resp, bad_resp, bad_resp])

        bus = _make_event_bus()
        manager = PlaybookManager(
            chat_provider=provider,
            event_bus=bus,
            data_dir=str(tmp_path),
        )

        # Compile successfully first
        result1 = await manager.compile_playbook(SIMPLE_PLAYBOOK_MD)
        assert result1.success
        assert manager.get_playbook("test-playbook").version == 1

        bus.emit.reset_mock()

        # Compile again with modified markdown (different hash) — bad output causes failure
        modified_md = _make_playbook_md(body="# Changed\n\nDo something else entirely.")
        result2 = await manager.compile_playbook(modified_md)
        assert not result2.success

        # Previous version remains active
        active = manager.get_playbook("test-playbook")
        assert active is not None
        assert active.version == 1  # Still v1!

    @pytest.mark.asyncio
    async def test_previous_version_used_for_event_matching(self, tmp_path: Path) -> None:
        """(c) PlaybookManager continues to use previous version for event matching.

        After a failed recompilation, get_playbooks_by_trigger() must still
        return the previous version so that incoming events are correctly routed.
        """
        provider = _make_mock_provider()
        from src.chat_providers.types import ChatResponse, TextBlock

        json_str = json.dumps(VALID_COMPILED_NODES, indent=2)
        good_resp = ChatResponse(content=[TextBlock(text=f"```json\n{json_str}\n```")])
        bad_resp = ChatResponse(content=[TextBlock(text="garbage")])
        provider.create_message = AsyncMock(side_effect=[good_resp, bad_resp, bad_resp, bad_resp])

        manager = PlaybookManager(
            chat_provider=provider,
            data_dir=str(tmp_path),
        )

        # Compile successfully — playbook triggers on "git.commit"
        result1 = await manager.compile_playbook(SIMPLE_PLAYBOOK_MD)
        assert result1.success

        # Verify event matching works
        matches = manager.get_playbooks_by_trigger("git.commit")
        assert len(matches) == 1
        assert matches[0].id == "test-playbook"
        assert matches[0].version == 1

        # Fail a recompilation (different content so hash changes)
        modified_md = _make_playbook_md(body="# Updated\n\nChanged content to trigger recompile.")
        result2 = await manager.compile_playbook(modified_md)
        assert not result2.success

        # Event matching still returns the v1 playbook — previous version is
        # used for event matching despite the failed recompilation
        matches_after = manager.get_playbooks_by_trigger("git.commit")
        assert len(matches_after) == 1
        assert matches_after[0].id == "test-playbook"
        assert matches_after[0].version == 1

        # Unrelated trigger still returns empty
        assert manager.get_playbooks_by_trigger("task.completed") == []

    @pytest.mark.asyncio
    async def test_previous_json_retained_on_disk(self, tmp_path: Path) -> None:
        """(b) Previous valid compiled JSON is retained on disk after failed recompilation."""
        provider = _make_mock_provider()
        from src.chat_providers.types import ChatResponse, TextBlock

        json_str = json.dumps(VALID_COMPILED_NODES, indent=2)
        good_resp = ChatResponse(content=[TextBlock(text=f"```json\n{json_str}\n```")])
        bad_resp = ChatResponse(content=[TextBlock(text="garbage")])
        provider.create_message = AsyncMock(side_effect=[good_resp, bad_resp, bad_resp, bad_resp])

        manager = PlaybookManager(
            chat_provider=provider,
            data_dir=str(tmp_path),
        )

        await manager.compile_playbook(SIMPLE_PLAYBOOK_MD)
        json_path = tmp_path / "playbooks" / "compiled" / "test-playbook.json"
        assert json_path.exists()
        original_data = json.loads(json_path.read_text())
        assert original_data["version"] == 1

        # Failed recompilation with changed markdown — disk file untouched
        modified_md = _make_playbook_md(body="# Revised\n\nDo something new and different.")
        await manager.compile_playbook(modified_md)
        assert json_path.exists()
        after_data = json.loads(json_path.read_text())
        assert after_data["version"] == 1  # Not overwritten

    @pytest.mark.asyncio
    async def test_error_notification_includes_details_json_extraction(self, tmp_path: Path) -> None:
        """(d) Error notification includes file path and LLM error details.

        When the LLM returns unparseable output (not valid JSON), the error
        notification must include the source_path and mention the JSON
        extraction failure.
        """
        provider = _make_mock_provider()
        from src.chat_providers.types import ChatResponse, TextBlock

        # Return something that can't be parsed as valid JSON
        bad_resp = ChatResponse(content=[TextBlock(text="no json here")])
        provider.create_message = AsyncMock(side_effect=[bad_resp, bad_resp, bad_resp])

        bus = _make_event_bus()
        manager = PlaybookManager(
            chat_provider=provider,
            event_bus=bus,
            data_dir=str(tmp_path),
        )

        await manager.compile_playbook(
            SIMPLE_PLAYBOOK_MD,
            source_path="/vault/system/playbooks/test.md",
            rel_path="system/playbooks/test.md",
        )

        bus.emit.assert_called_once()
        event_type, payload = bus.emit.call_args[0]
        assert event_type == "notify.playbook_compilation_failed"
        assert payload["source_path"] == "/vault/system/playbooks/test.md"
        assert payload["playbook_id"] == "test-playbook"
        assert len(payload["errors"]) > 0
        # Should mention JSON extraction failure
        assert any("JSON" in e for e in payload["errors"])
        # Retries count should be recorded
        assert "retries_used" in payload

    @pytest.mark.asyncio
    async def test_error_notification_includes_validation_details(self, tmp_path: Path) -> None:
        """(d) Error notification includes validation error details.

        When the LLM returns structurally valid JSON but the playbook fails
        validation (e.g. missing entry node), the error notification must
        include the specific validation errors.
        """
        provider = _make_mock_provider()
        from src.chat_providers.types import ChatResponse, TextBlock

        # Return JSON that parses but fails validation — no entry node
        invalid_nodes = {
            "nodes": {
                "step": {"prompt": "Do something.", "goto": "done"},
                "done": {"terminal": True},
            }
        }
        bad_json = json.dumps(invalid_nodes)
        bad_resp = ChatResponse(content=[TextBlock(text=f"```json\n{bad_json}\n```")])
        provider.create_message = AsyncMock(side_effect=[bad_resp, bad_resp, bad_resp])

        bus = _make_event_bus()
        manager = PlaybookManager(
            chat_provider=provider,
            event_bus=bus,
            data_dir=str(tmp_path),
        )

        await manager.compile_playbook(
            SIMPLE_PLAYBOOK_MD,
            source_path="/vault/system/playbooks/test.md",
        )

        bus.emit.assert_called_once()
        event_type, payload = bus.emit.call_args[0]
        assert event_type == "notify.playbook_compilation_failed"
        assert payload["source_path"] == "/vault/system/playbooks/test.md"
        assert len(payload["errors"]) > 0
        # Validation errors should reference the structural issue
        error_text = " ".join(payload["errors"]).lower()
        assert "entry" in error_text  # Missing entry node

    @pytest.mark.asyncio
    async def test_llm_provider_failure_retains_previous(self, tmp_path: Path) -> None:
        """(e) LLM provider failure retains previous version and notifies.

        When the LLM provider raises an exception (e.g. API unavailable),
        the previous compiled version must remain active in memory AND on disk,
        and the error notification must include the LLM error details.
        """
        # First compile successfully
        good_provider = _make_mock_provider()
        bus = _make_event_bus()
        manager = PlaybookManager(
            chat_provider=good_provider,
            event_bus=bus,
            data_dir=str(tmp_path),
        )

        result1 = await manager.compile_playbook(SIMPLE_PLAYBOOK_MD)
        assert result1.success
        assert manager.get_playbook("test-playbook").version == 1

        bus.emit.reset_mock()

        # Now switch to a failing provider and use different content
        manager._compiler._provider = _make_failing_provider("API rate limited")

        modified_md = _make_playbook_md(body="# Modified\n\nDo something totally new.")
        result2 = await manager.compile_playbook(modified_md)
        assert not result2.success
        assert any("LLM call failed" in e for e in result2.errors)

        # Previous version still active in memory
        assert manager.get_playbook("test-playbook").version == 1

        # Previous version still on disk
        json_path = tmp_path / "playbooks" / "compiled" / "test-playbook.json"
        assert json_path.exists()
        disk_data = json.loads(json_path.read_text())
        assert disk_data["version"] == 1

        # Error notification emitted with full details
        bus.emit.assert_called_once()
        event_type, payload = bus.emit.call_args[0]
        assert event_type == "notify.playbook_compilation_failed"
        assert payload["previous_version"] == 1
        assert payload["playbook_id"] == "test-playbook"
        assert len(payload["errors"]) > 0
        # The LLM error message should appear in the errors
        assert any("LLM call failed" in e for e in payload["errors"])

    @pytest.mark.asyncio
    async def test_no_node_structure_produces_error(self, tmp_path: Path) -> None:
        """(a) Markdown with no recognizable node structure produces error notification.

        When the LLM returns JSON with an empty nodes dict (no recognizable
        node structure), validation must fail and an error notification is emitted
        with details about the structural problem.
        """
        provider = _make_mock_provider()
        from src.chat_providers.types import ChatResponse, TextBlock

        # LLM returns JSON with no nodes (empty)
        bad_json = json.dumps({"nodes": {}})
        bad_resp = ChatResponse(content=[TextBlock(text=f"```json\n{bad_json}\n```")])
        provider.create_message = AsyncMock(side_effect=[bad_resp, bad_resp, bad_resp])

        bus = _make_event_bus()
        manager = PlaybookManager(
            chat_provider=provider,
            event_bus=bus,
            data_dir=str(tmp_path),
        )

        result = await manager.compile_playbook(
            SIMPLE_PLAYBOOK_MD,
            source_path="/vault/system/playbooks/test.md",
        )

        assert not result.success
        # Errors should mention structural issues (no entry node, no terminal, etc.)
        assert len(result.errors) > 0
        error_text = " ".join(result.errors).lower()
        assert "entry" in error_text or "node" in error_text or "terminal" in error_text

        # Error notification emitted
        bus.emit.assert_called_once()
        event_type, payload = bus.emit.call_args[0]
        assert event_type == "notify.playbook_compilation_failed"
        assert payload["playbook_id"] == "test-playbook"
        assert len(payload["errors"]) > 0

    @pytest.mark.asyncio
    async def test_no_node_structure_no_prior_version(self, tmp_path: Path) -> None:
        """(a) Error notification for first compilation failure has previous_version=None.

        When there is no prior compiled version, the error notification should
        indicate previous_version=None to distinguish from a recompilation failure.
        """
        provider = _make_mock_provider()
        from src.chat_providers.types import ChatResponse, TextBlock

        bad_json = json.dumps({"nodes": {}})
        bad_resp = ChatResponse(content=[TextBlock(text=f"```json\n{bad_json}\n```")])
        provider.create_message = AsyncMock(side_effect=[bad_resp, bad_resp, bad_resp])

        bus = _make_event_bus()
        manager = PlaybookManager(
            chat_provider=provider,
            event_bus=bus,
            data_dir=str(tmp_path),
        )

        await manager.compile_playbook(
            SIMPLE_PLAYBOOK_MD,
            source_path="/vault/system/playbooks/test.md",
        )

        bus.emit.assert_called_once()
        _, payload = bus.emit.call_args[0]
        assert payload["previous_version"] is None
        assert payload["source_path"] == "/vault/system/playbooks/test.md"

    @pytest.mark.asyncio
    async def test_partial_compilation_fails_atomically(self, tmp_path: Path) -> None:
        """(f) Partially valid markdown fails entire compilation (atomic).

        When the LLM returns nodes where some are valid but the overall graph
        fails validation, NO partial update should occur.
        """
        provider = _make_mock_provider()
        from src.chat_providers.types import ChatResponse, TextBlock

        # Return JSON with nodes but missing entry node — validation will fail
        partial_nodes = {
            "nodes": {
                "step1": {"prompt": "Do something.", "goto": "done"},
                "done": {"terminal": True},
                # No entry: True on any node!
            }
        }
        bad_json = json.dumps(partial_nodes)
        bad_resp = ChatResponse(content=[TextBlock(text=f"```json\n{bad_json}\n```")])
        provider.create_message = AsyncMock(side_effect=[bad_resp, bad_resp, bad_resp])

        bus = _make_event_bus()
        manager = PlaybookManager(
            chat_provider=provider,
            event_bus=bus,
            data_dir=str(tmp_path),
        )

        result = await manager.compile_playbook(SIMPLE_PLAYBOOK_MD)
        assert not result.success
        assert manager.get_playbook("test-playbook") is None  # Nothing activated
        # No compiled JSON written to disk
        json_path = tmp_path / "playbooks" / "compiled" / "test-playbook.json"
        assert not json_path.exists()

    @pytest.mark.asyncio
    async def test_partial_compilation_retains_previous_version(self, tmp_path: Path) -> None:
        """(f) Partially valid recompilation retains previous version atomically.

        When a valid v1 exists and recompilation produces partially valid output
        (some nodes OK, some broken), the entire update must be rejected and
        v1 must remain active — no partial update occurs.
        """
        provider = _make_mock_provider()
        from src.chat_providers.types import ChatResponse, TextBlock

        json_str = json.dumps(VALID_COMPILED_NODES, indent=2)
        good_resp = ChatResponse(content=[TextBlock(text=f"```json\n{json_str}\n```")])

        # Partial: some nodes valid (done is terminal) but no entry node
        partial_nodes = {
            "nodes": {
                "analyze": {"prompt": "Analyze the code.", "goto": "done"},
                "done": {"terminal": True},
                # Missing entry: True — invalid graph
            }
        }
        partial_json = json.dumps(partial_nodes)
        partial_resp = ChatResponse(content=[TextBlock(text=f"```json\n{partial_json}\n```")])
        provider.create_message = AsyncMock(
            side_effect=[good_resp, partial_resp, partial_resp, partial_resp]
        )

        bus = _make_event_bus()
        manager = PlaybookManager(
            chat_provider=provider,
            event_bus=bus,
            data_dir=str(tmp_path),
        )

        # Compile v1 successfully
        result1 = await manager.compile_playbook(SIMPLE_PLAYBOOK_MD)
        assert result1.success
        assert manager.get_playbook("test-playbook").version == 1

        bus.emit.reset_mock()

        # Attempt recompilation with different content producing partial output
        modified_md = _make_playbook_md(body="# Updated\n\nTrigger recompile with bad graph.")
        result2 = await manager.compile_playbook(modified_md)
        assert not result2.success

        # v1 still active — atomic, no partial update
        active = manager.get_playbook("test-playbook")
        assert active is not None
        assert active.version == 1

        # v1 still on disk
        json_path = tmp_path / "playbooks" / "compiled" / "test-playbook.json"
        assert json_path.exists()
        disk_data = json.loads(json_path.read_text())
        assert disk_data["version"] == 1

        # Error notification emitted with previous_version reference
        bus.emit.assert_called_once()
        _, payload = bus.emit.call_args[0]
        assert payload["previous_version"] == 1


# ---------------------------------------------------------------------------
# Test: Fix and recompile
# ---------------------------------------------------------------------------


class TestFixAndRecompile:
    """Test that fixing markdown and recompiling succeeds (roadmap 5.1.9 case g)."""

    @pytest.mark.asyncio
    async def test_recompile_succeeds_after_fix(self, tmp_path: Path) -> None:
        """(g) Fixing markdown and saving again triggers successful recompilation."""
        provider = _make_mock_provider()
        from src.chat_providers.types import ChatResponse, TextBlock

        json_str = json.dumps(VALID_COMPILED_NODES, indent=2)
        good_resp = ChatResponse(content=[TextBlock(text=f"```json\n{json_str}\n```")])
        bad_resp = ChatResponse(content=[TextBlock(text="not json")])
        # First compile fails (3 attempts), then second compile succeeds
        provider.create_message = AsyncMock(side_effect=[bad_resp, bad_resp, bad_resp, good_resp])

        bus = _make_event_bus()
        manager = PlaybookManager(
            chat_provider=provider,
            event_bus=bus,
            data_dir=str(tmp_path),
        )

        # First attempt fails
        result1 = await manager.compile_playbook(SIMPLE_PLAYBOOK_MD)
        assert not result1.success
        assert manager.get_playbook("test-playbook") is None

        bus.emit.reset_mock()

        # Second attempt (after "fixing" the markdown) succeeds
        result2 = await manager.compile_playbook(SIMPLE_PLAYBOOK_MD)
        assert result2.success
        assert manager.get_playbook("test-playbook") is not None
        assert manager.get_playbook("test-playbook").version == 1

        # Success event emitted
        bus.emit.assert_called_once()
        event_type, _ = bus.emit.call_args[0]
        assert event_type == "notify.playbook_compilation_succeeded"

    @pytest.mark.asyncio
    async def test_recompile_after_fix_with_existing_version(self, tmp_path: Path) -> None:
        """(g) Fix after failed recompilation replaces the retained v1 with v2.

        Sequence: compile v1 → fail recompilation → v1 retained → fix → v2 active.
        """
        provider = _make_mock_provider()
        from src.chat_providers.types import ChatResponse, TextBlock

        json_str = json.dumps(VALID_COMPILED_NODES, indent=2)
        good_resp = ChatResponse(content=[TextBlock(text=f"```json\n{json_str}\n```")])
        bad_resp = ChatResponse(content=[TextBlock(text="garbage")])
        # v1 succeeds, recompile fails (3 attempts), fix succeeds
        provider.create_message = AsyncMock(
            side_effect=[good_resp, bad_resp, bad_resp, bad_resp, good_resp]
        )

        bus = _make_event_bus()
        manager = PlaybookManager(
            chat_provider=provider,
            event_bus=bus,
            data_dir=str(tmp_path),
        )

        # Compile v1 successfully
        result1 = await manager.compile_playbook(SIMPLE_PLAYBOOK_MD)
        assert result1.success
        assert manager.get_playbook("test-playbook").version == 1

        # Failed recompilation (different content so hash changes)
        bad_md = _make_playbook_md(body="# Broken\n\nThis triggers recompile but fails.")
        result2 = await manager.compile_playbook(bad_md)
        assert not result2.success
        assert manager.get_playbook("test-playbook").version == 1  # v1 retained

        bus.emit.reset_mock()

        # Fix and recompile successfully (different content again)
        fixed_md = _make_playbook_md(body="# Fixed\n\nThis version compiles cleanly.")
        result3 = await manager.compile_playbook(fixed_md)
        assert result3.success
        assert manager.get_playbook("test-playbook").version == 2  # v2 now active

        # Disk updated to v2
        json_path = tmp_path / "playbooks" / "compiled" / "test-playbook.json"
        disk_data = json.loads(json_path.read_text())
        assert disk_data["version"] == 2

        # Success event emitted for v2
        bus.emit.assert_called_once()
        event_type, payload = bus.emit.call_args[0]
        assert event_type == "notify.playbook_compilation_succeeded"
        assert payload["version"] == 2


# ---------------------------------------------------------------------------
# Test: Failure isolation (multiple playbooks)
# ---------------------------------------------------------------------------


class TestFailureIsolation:
    """Test that a compilation failure for one playbook does not affect others."""

    @pytest.mark.asyncio
    async def test_failure_does_not_affect_other_playbooks(self, tmp_path: Path) -> None:
        """Failing compilation of playbook B leaves playbook A untouched."""
        provider = _make_mock_provider()
        from src.chat_providers.types import ChatResponse, TextBlock

        json_str = json.dumps(VALID_COMPILED_NODES, indent=2)
        good_resp = ChatResponse(content=[TextBlock(text=f"```json\n{json_str}\n```")])
        bad_resp = ChatResponse(content=[TextBlock(text="garbage")])
        # First call for playbook A succeeds; next 3 calls for playbook B fail
        provider.create_message = AsyncMock(
            side_effect=[good_resp, bad_resp, bad_resp, bad_resp]
        )

        manager = PlaybookManager(
            chat_provider=provider,
            data_dir=str(tmp_path),
        )

        # Compile playbook A successfully
        md_a = _make_playbook_md(playbook_id="playbook-a", triggers="- git.push")
        result_a = await manager.compile_playbook(md_a)
        assert result_a.success
        assert manager.get_playbook("playbook-a") is not None

        # Compile playbook B — fails
        md_b = _make_playbook_md(playbook_id="playbook-b", triggers="- task.completed")
        result_b = await manager.compile_playbook(md_b)
        assert not result_b.success

        # Playbook A is unaffected
        assert manager.get_playbook("playbook-a") is not None
        assert manager.get_playbook("playbook-a").version == 1
        assert manager.get_playbooks_by_trigger("git.push") != []

        # Playbook B was never activated
        assert manager.get_playbook("playbook-b") is None

    @pytest.mark.asyncio
    async def test_retries_count_in_error_notification(self, tmp_path: Path) -> None:
        """Error notification records how many retries were attempted."""
        provider = _make_mock_provider()
        from src.chat_providers.types import ChatResponse, TextBlock

        bad_resp = ChatResponse(content=[TextBlock(text="garbage")])
        # 3 attempts: initial + 2 retries
        provider.create_message = AsyncMock(side_effect=[bad_resp, bad_resp, bad_resp])

        bus = _make_event_bus()
        manager = PlaybookManager(
            chat_provider=provider,
            event_bus=bus,
            data_dir=str(tmp_path),
        )

        result = await manager.compile_playbook(SIMPLE_PLAYBOOK_MD)
        assert not result.success
        assert result.retries_used == 2  # max_retries default is 2

        bus.emit.assert_called_once()
        _, payload = bus.emit.call_args[0]
        assert payload["retries_used"] == 2


# ---------------------------------------------------------------------------
# Test: Disk loading
# ---------------------------------------------------------------------------


class TestDiskLoading:
    """Test loading compiled playbooks from disk at startup."""

    @pytest.mark.asyncio
    async def test_load_from_disk(self, tmp_path: Path) -> None:
        """Compiled playbooks are loaded from disk on startup."""
        compiled_dir = tmp_path / "playbooks" / "compiled"
        compiled_dir.mkdir(parents=True)

        playbook = _make_playbook()
        (compiled_dir / "test-playbook.json").write_text(json.dumps(playbook.to_dict(), indent=2))

        manager = PlaybookManager(data_dir=str(tmp_path))
        loaded = await manager.load_from_disk()

        assert loaded == 1
        active = manager.get_playbook("test-playbook")
        assert active is not None
        assert active.version == 1

    @pytest.mark.asyncio
    async def test_load_skips_invalid_json(self, tmp_path: Path) -> None:
        """Invalid JSON files on disk are skipped during loading."""
        compiled_dir = tmp_path / "playbooks" / "compiled"
        compiled_dir.mkdir(parents=True)

        # Write a valid one
        playbook = _make_playbook()
        (compiled_dir / "good.json").write_text(json.dumps(playbook.to_dict()))

        # Write an invalid one
        (compiled_dir / "bad.json").write_text("not json {{{")

        manager = PlaybookManager(data_dir=str(tmp_path))
        loaded = await manager.load_from_disk()

        assert loaded == 1

    @pytest.mark.asyncio
    async def test_load_skips_validation_errors(self, tmp_path: Path) -> None:
        """Playbooks that fail validation are skipped during loading."""
        compiled_dir = tmp_path / "playbooks" / "compiled"
        compiled_dir.mkdir(parents=True)

        # Write a structurally invalid playbook (no entry node)
        bad_data = {
            "id": "bad",
            "version": 1,
            "source_hash": "abc123",
            "triggers": ["test"],
            "scope": "system",
            "nodes": {
                "step": {"prompt": "Do something.", "terminal": True},
                # No entry node!
            },
        }
        (compiled_dir / "bad.json").write_text(json.dumps(bad_data))

        manager = PlaybookManager(data_dir=str(tmp_path))
        loaded = await manager.load_from_disk()

        assert loaded == 0

    @pytest.mark.asyncio
    async def test_load_no_data_dir(self) -> None:
        """Loading without a data_dir returns 0."""
        manager = PlaybookManager()
        loaded = await manager.load_from_disk()
        assert loaded == 0

    @pytest.mark.asyncio
    async def test_load_empty_directory(self, tmp_path: Path) -> None:
        """Loading from an empty compiled directory returns 0."""
        compiled_dir = tmp_path / "playbooks" / "compiled"
        compiled_dir.mkdir(parents=True)

        manager = PlaybookManager(data_dir=str(tmp_path))
        loaded = await manager.load_from_disk()
        assert loaded == 0


# ---------------------------------------------------------------------------
# Test: Playbook deletion
# ---------------------------------------------------------------------------


class TestPlaybookDeletion:
    """Test removing playbooks from the active registry."""

    @pytest.mark.asyncio
    async def test_remove_existing_playbook(self, tmp_path: Path) -> None:
        """Removing an existing playbook clears it from memory and disk."""
        compiled_dir = tmp_path / "playbooks" / "compiled"
        compiled_dir.mkdir(parents=True)

        playbook = _make_playbook()
        json_path = compiled_dir / "test-playbook.json"
        json_path.write_text(json.dumps(playbook.to_dict()))

        manager = PlaybookManager(data_dir=str(tmp_path))
        await manager.load_from_disk()
        assert manager.get_playbook("test-playbook") is not None

        removed = await manager.remove_playbook("test-playbook")
        assert removed
        assert manager.get_playbook("test-playbook") is None
        assert not json_path.exists()

    @pytest.mark.asyncio
    async def test_remove_nonexistent_playbook(self) -> None:
        """Removing a nonexistent playbook returns False."""
        manager = PlaybookManager()
        removed = await manager.remove_playbook("nonexistent")
        assert not removed


# ---------------------------------------------------------------------------
# Test: Trigger lookup
# ---------------------------------------------------------------------------


class TestTriggerLookup:
    """Test finding playbooks by trigger event type."""

    @pytest.mark.asyncio
    async def test_get_playbooks_by_trigger(self, tmp_path: Path) -> None:
        """Playbooks are found by matching trigger strings."""
        compiled_dir = tmp_path / "playbooks" / "compiled"
        compiled_dir.mkdir(parents=True)

        pb1 = _make_playbook(playbook_id="pb-1", triggers=["git.commit", "git.push"])
        pb2 = _make_playbook(playbook_id="pb-2", triggers=["task.completed"])
        pb3 = _make_playbook(playbook_id="pb-3", triggers=["git.commit"])

        (compiled_dir / "pb-1.json").write_text(json.dumps(pb1.to_dict()))
        (compiled_dir / "pb-2.json").write_text(json.dumps(pb2.to_dict()))
        (compiled_dir / "pb-3.json").write_text(json.dumps(pb3.to_dict()))

        manager = PlaybookManager(data_dir=str(tmp_path))
        await manager.load_from_disk()

        git_commit_playbooks = manager.get_playbooks_by_trigger("git.commit")
        assert len(git_commit_playbooks) == 2
        ids = {pb.id for pb in git_commit_playbooks}
        assert ids == {"pb-1", "pb-3"}

        task_playbooks = manager.get_playbooks_by_trigger("task.completed")
        assert len(task_playbooks) == 1
        assert task_playbooks[0].id == "pb-2"

        assert manager.get_playbooks_by_trigger("nonexistent") == []


# ---------------------------------------------------------------------------
# Test: No-provider mode
# ---------------------------------------------------------------------------


class TestNoProviderMode:
    """Test behavior when no chat provider is configured."""

    @pytest.mark.asyncio
    async def test_compile_without_provider_fails_gracefully(self) -> None:
        """Compilation without a chat provider returns a clear error."""
        manager = PlaybookManager()
        result = await manager.compile_playbook(SIMPLE_PLAYBOOK_MD)
        assert not result.success
        assert any("No chat provider" in e for e in result.errors)

    @pytest.mark.asyncio
    async def test_compile_without_provider_no_event(self) -> None:
        """No event is emitted when no provider is configured."""
        bus = _make_event_bus()
        manager = PlaybookManager(event_bus=bus)
        await manager.compile_playbook(SIMPLE_PLAYBOOK_MD)
        # No failure event emitted — the no-provider case is an
        # operational configuration issue, not a compilation error
        bus.emit.assert_not_called()


# ---------------------------------------------------------------------------
# Test: Frontmatter-only errors (before LLM call)
# ---------------------------------------------------------------------------


class TestFrontmatterErrors:
    """Test that frontmatter validation errors are surfaced correctly."""

    @pytest.mark.asyncio
    async def test_missing_frontmatter_emits_error(self, tmp_path: Path) -> None:
        """Markdown without frontmatter produces a compilation error notification."""
        provider = _make_mock_provider()
        bus = _make_event_bus()
        manager = PlaybookManager(
            chat_provider=provider,
            event_bus=bus,
            data_dir=str(tmp_path),
        )

        result = await manager.compile_playbook(
            "# No frontmatter\n\nJust some text.",
            source_path="/vault/test.md",
        )

        assert not result.success
        assert any("frontmatter" in e.lower() for e in result.errors)

        bus.emit.assert_called_once()
        event_type, payload = bus.emit.call_args[0]
        assert event_type == "notify.playbook_compilation_failed"

    @pytest.mark.asyncio
    async def test_missing_id_emits_error(self, tmp_path: Path) -> None:
        """Frontmatter without 'id' field produces error notification."""
        provider = _make_mock_provider()
        bus = _make_event_bus()
        manager = PlaybookManager(
            chat_provider=provider,
            event_bus=bus,
            data_dir=str(tmp_path),
        )

        md = """\
---
triggers:
  - git.commit
scope: system
---

# Test
"""
        result = await manager.compile_playbook(md, source_path="/vault/test.md")
        assert not result.success
        assert any("id" in e.lower() for e in result.errors)


# ---------------------------------------------------------------------------
# Test: PlaybookHandler integration
# ---------------------------------------------------------------------------


class TestPlaybookHandler:
    """Test the vault watcher handler integration with PlaybookManager."""

    @pytest.mark.asyncio
    async def test_on_playbook_changed_created(self, tmp_path: Path) -> None:
        """Handler triggers compilation on 'created' operation."""
        from src.playbook_handler import on_playbook_changed

        # Write a playbook file
        md_path = tmp_path / "test.md"
        md_path.write_text(SIMPLE_PLAYBOOK_MD)

        provider = _make_mock_provider()
        manager = PlaybookManager(
            chat_provider=provider,
            data_dir=str(tmp_path),
        )

        change = MagicMock()
        change.path = str(md_path)
        change.rel_path = "system/playbooks/test.md"
        change.operation = "created"

        await on_playbook_changed([change], playbook_manager=manager)

        assert manager.get_playbook("test-playbook") is not None

    @pytest.mark.asyncio
    async def test_on_playbook_changed_modified(self, tmp_path: Path) -> None:
        """Handler triggers recompilation on 'modified' operation."""
        from src.playbook_handler import on_playbook_changed

        md_path = tmp_path / "test.md"
        md_path.write_text(SIMPLE_PLAYBOOK_MD)

        provider = _make_mock_provider()
        from src.chat_providers.types import ChatResponse, TextBlock

        json_str = json.dumps(VALID_COMPILED_NODES, indent=2)
        resp = ChatResponse(content=[TextBlock(text=f"```json\n{json_str}\n```")])
        provider.create_message = AsyncMock(side_effect=[resp, resp])

        manager = PlaybookManager(
            chat_provider=provider,
            data_dir=str(tmp_path),
        )

        change = MagicMock()
        change.path = str(md_path)
        change.rel_path = "system/playbooks/test.md"
        change.operation = "modified"

        await on_playbook_changed([change], playbook_manager=manager)
        assert manager.get_playbook("test-playbook").version == 1

        # Write different content to simulate actual file modification
        modified_md = _make_playbook_md(body="# Changed\n\nDo something else now.")
        md_path.write_text(modified_md)

        # Modify and recompile — different hash triggers actual compilation
        await on_playbook_changed([change], playbook_manager=manager)
        assert manager.get_playbook("test-playbook").version == 2

    @pytest.mark.asyncio
    async def test_on_playbook_changed_deleted(self, tmp_path: Path) -> None:
        """Handler removes playbook from registry on 'deleted' operation."""
        from src.playbook_handler import on_playbook_changed

        compiled_dir = tmp_path / "playbooks" / "compiled"
        compiled_dir.mkdir(parents=True)

        playbook = _make_playbook(playbook_id="test")
        (compiled_dir / "test.json").write_text(json.dumps(playbook.to_dict()))

        manager = PlaybookManager(data_dir=str(tmp_path))
        await manager.load_from_disk()
        assert manager.get_playbook("test") is not None

        change = MagicMock()
        change.path = str(tmp_path / "test.md")
        change.rel_path = "system/playbooks/test.md"
        change.operation = "deleted"

        await on_playbook_changed([change], playbook_manager=manager)
        assert manager.get_playbook("test") is None

    @pytest.mark.asyncio
    async def test_on_playbook_changed_no_manager(self) -> None:
        """Handler gracefully falls back to log-only when no manager provided."""
        from src.playbook_handler import on_playbook_changed

        change = MagicMock()
        change.path = "/vault/system/playbooks/test.md"
        change.rel_path = "system/playbooks/test.md"
        change.operation = "created"

        # Should not raise
        await on_playbook_changed([change], playbook_manager=None)

    @pytest.mark.asyncio
    async def test_on_playbook_changed_unreadable_file(self, tmp_path: Path) -> None:
        """Handler handles unreadable files gracefully."""
        from src.playbook_handler import on_playbook_changed

        manager = PlaybookManager(
            chat_provider=_make_mock_provider(),
            data_dir=str(tmp_path),
        )

        change = MagicMock()
        change.path = str(tmp_path / "nonexistent.md")
        change.rel_path = "system/playbooks/nonexistent.md"
        change.operation = "created"

        # Should not raise
        await on_playbook_changed([change], playbook_manager=manager)


# ---------------------------------------------------------------------------
# Test: Event models
# ---------------------------------------------------------------------------


class TestNotificationEvents:
    """Test the playbook compilation notification event models."""

    def test_compilation_failed_event_fields(self) -> None:
        """PlaybookCompilationFailedEvent has all required fields."""
        from src.notifications.events import PlaybookCompilationFailedEvent

        event = PlaybookCompilationFailedEvent(
            playbook_id="test-pb",
            source_path="/vault/system/playbooks/test.md",
            errors=["validation error 1", "validation error 2"],
            previous_version=3,
            source_hash="abc123",
            retries_used=2,
        )

        assert event.event_type == "notify.playbook_compilation_failed"
        assert event.severity == "error"
        assert event.category == "system"
        assert event.playbook_id == "test-pb"
        assert event.source_path == "/vault/system/playbooks/test.md"
        assert len(event.errors) == 2
        assert event.previous_version == 3
        assert event.retries_used == 2

        # Serializable
        data = event.model_dump(mode="json")
        assert data["playbook_id"] == "test-pb"
        assert data["previous_version"] == 3

    def test_compilation_succeeded_event_fields(self) -> None:
        """PlaybookCompilationSucceededEvent has all required fields."""
        from src.notifications.events import PlaybookCompilationSucceededEvent

        event = PlaybookCompilationSucceededEvent(
            playbook_id="test-pb",
            source_path="/vault/system/playbooks/test.md",
            version=2,
            source_hash="abc123",
            node_count=5,
            retries_used=1,
        )

        assert event.event_type == "notify.playbook_compilation_succeeded"
        assert event.severity == "info"
        assert event.version == 2
        assert event.node_count == 5

    def test_failed_event_no_previous_version(self) -> None:
        """PlaybookCompilationFailedEvent works without a previous version."""
        from src.notifications.events import PlaybookCompilationFailedEvent

        event = PlaybookCompilationFailedEvent(
            playbook_id="new-pb",
            errors=["some error"],
        )

        assert event.previous_version is None
        data = event.model_dump(mode="json")
        assert data["previous_version"] is None


# ---------------------------------------------------------------------------
# Test: Helper functions
# ---------------------------------------------------------------------------


class TestHelperFunctions:
    """Test utility functions in the playbook handler."""

    def test_derive_playbook_id_from_path(self) -> None:
        """_derive_playbook_id_from_path extracts filename stem."""
        from src.playbook_handler import _derive_playbook_id_from_path

        assert _derive_playbook_id_from_path("system/playbooks/deploy.md") == "deploy"
        assert _derive_playbook_id_from_path("projects/my-app/playbooks/review.md") == "review"
        assert _derive_playbook_id_from_path("") is None

    def test_derive_playbook_scope(self) -> None:
        """derive_playbook_scope extracts scope and identifier."""
        from src.playbook_handler import derive_playbook_scope

        assert derive_playbook_scope("system/playbooks/deploy.md") == ("system", None)
        assert derive_playbook_scope("orchestrator/playbooks/routing.md") == (
            "orchestrator",
            None,
        )
        assert derive_playbook_scope("agent-types/coding/playbooks/quality.md") == (
            "agent_type",
            "coding",
        )
        assert derive_playbook_scope("projects/my-app/playbooks/review.md") == (
            "project",
            "my-app",
        )


# ---------------------------------------------------------------------------
# Test: Source hash change detection (roadmap 5.1.5)
# ---------------------------------------------------------------------------


class TestSourceHashChangeDetection:
    """Test that unchanged markdown skips recompilation.

    Covers roadmap 5.1.5 requirements:
      - Same markdown → skip compilation (no LLM call)
      - Different markdown → proceed with compilation
      - force=True → always compile regardless of hash
      - No existing version → proceed with compilation
      - Skipped result has correct fields (success=True, skipped=True)
      - Loaded-from-disk versions enable hash comparison
    """

    @pytest.mark.asyncio
    async def test_same_markdown_skips_compilation(self, tmp_path: Path) -> None:
        """Compiling with identical markdown skips the LLM call."""
        provider = _make_mock_provider()
        manager = PlaybookManager(
            chat_provider=provider,
            data_dir=str(tmp_path),
        )

        # First compilation — invokes LLM
        result1 = await manager.compile_playbook(SIMPLE_PLAYBOOK_MD)
        assert result1.success
        assert not result1.skipped
        assert provider.create_message.call_count == 1

        # Second compilation with same markdown — should be skipped
        result2 = await manager.compile_playbook(SIMPLE_PLAYBOOK_MD)
        assert result2.success
        assert result2.skipped
        assert result2.playbook is not None
        assert result2.playbook.id == "test-playbook"
        assert result2.playbook.version == 1  # No version increment
        # LLM was NOT called a second time
        assert provider.create_message.call_count == 1

    @pytest.mark.asyncio
    async def test_different_markdown_triggers_compilation(self, tmp_path: Path) -> None:
        """Compiling with changed markdown triggers full compilation."""
        provider = _make_mock_provider()
        from src.chat_providers.types import ChatResponse, TextBlock

        json_str = json.dumps(VALID_COMPILED_NODES, indent=2)
        resp = ChatResponse(content=[TextBlock(text=f"```json\n{json_str}\n```")])
        provider.create_message = AsyncMock(side_effect=[resp, resp])

        manager = PlaybookManager(
            chat_provider=provider,
            data_dir=str(tmp_path),
        )

        result1 = await manager.compile_playbook(SIMPLE_PLAYBOOK_MD)
        assert result1.success
        assert not result1.skipped

        # Different body → different source hash
        modified_md = _make_playbook_md(body="# Updated\n\nDo something different.")
        result2 = await manager.compile_playbook(modified_md)
        assert result2.success
        assert not result2.skipped
        assert result2.playbook.version == 2
        # LLM was called both times
        assert provider.create_message.call_count == 2

    @pytest.mark.asyncio
    async def test_force_bypasses_hash_check(self, tmp_path: Path) -> None:
        """force=True always invokes the compiler, even with same markdown."""
        provider = _make_mock_provider()
        from src.chat_providers.types import ChatResponse, TextBlock

        json_str = json.dumps(VALID_COMPILED_NODES, indent=2)
        resp = ChatResponse(content=[TextBlock(text=f"```json\n{json_str}\n```")])
        provider.create_message = AsyncMock(side_effect=[resp, resp])

        manager = PlaybookManager(
            chat_provider=provider,
            data_dir=str(tmp_path),
        )

        result1 = await manager.compile_playbook(SIMPLE_PLAYBOOK_MD)
        assert result1.success
        assert not result1.skipped

        # Same markdown but force=True — should recompile
        result2 = await manager.compile_playbook(SIMPLE_PLAYBOOK_MD, force=True)
        assert result2.success
        assert not result2.skipped
        assert result2.playbook.version == 2
        assert provider.create_message.call_count == 2

    @pytest.mark.asyncio
    async def test_no_existing_version_always_compiles(self, tmp_path: Path) -> None:
        """First compilation always proceeds (no hash to compare against)."""
        provider = _make_mock_provider()
        manager = PlaybookManager(
            chat_provider=provider,
            data_dir=str(tmp_path),
        )

        result = await manager.compile_playbook(SIMPLE_PLAYBOOK_MD)
        assert result.success
        assert not result.skipped
        assert provider.create_message.call_count == 1

    @pytest.mark.asyncio
    async def test_skipped_result_contains_source_hash(self, tmp_path: Path) -> None:
        """Skipped result includes the source_hash for the unchanged content."""
        from src.playbook_compiler import PlaybookCompiler

        provider = _make_mock_provider()
        manager = PlaybookManager(
            chat_provider=provider,
            data_dir=str(tmp_path),
        )

        await manager.compile_playbook(SIMPLE_PLAYBOOK_MD)
        result = await manager.compile_playbook(SIMPLE_PLAYBOOK_MD)
        assert result.skipped
        expected_hash = PlaybookCompiler._compute_source_hash(SIMPLE_PLAYBOOK_MD)
        assert result.source_hash == expected_hash

    @pytest.mark.asyncio
    async def test_skipped_result_returns_existing_playbook(self, tmp_path: Path) -> None:
        """Skipped result returns the active compiled playbook instance."""
        provider = _make_mock_provider()
        manager = PlaybookManager(
            chat_provider=provider,
            data_dir=str(tmp_path),
        )

        result1 = await manager.compile_playbook(SIMPLE_PLAYBOOK_MD)
        result2 = await manager.compile_playbook(SIMPLE_PLAYBOOK_MD)
        assert result2.skipped
        # Returns the same playbook object
        assert result2.playbook is result1.playbook

    @pytest.mark.asyncio
    async def test_skip_no_success_event_emitted(self, tmp_path: Path) -> None:
        """No compilation event is emitted when compilation is skipped."""
        provider = _make_mock_provider()
        bus = _make_event_bus()
        manager = PlaybookManager(
            chat_provider=provider,
            event_bus=bus,
            data_dir=str(tmp_path),
        )

        await manager.compile_playbook(SIMPLE_PLAYBOOK_MD)
        bus.emit.reset_mock()

        # Same markdown — skipped, no event
        await manager.compile_playbook(SIMPLE_PLAYBOOK_MD)
        bus.emit.assert_not_called()

    @pytest.mark.asyncio
    async def test_skip_works_after_load_from_disk(self, tmp_path: Path) -> None:
        """Hash check works against playbooks loaded from disk at startup."""
        from src.playbook_compiler import PlaybookCompiler

        # Pre-populate a compiled playbook on disk with a known hash
        compiled_dir = tmp_path / "playbooks" / "compiled"
        compiled_dir.mkdir(parents=True)

        source_hash = PlaybookCompiler._compute_source_hash(SIMPLE_PLAYBOOK_MD)
        playbook = _make_playbook(source_hash=source_hash)
        (compiled_dir / "test-playbook.json").write_text(json.dumps(playbook.to_dict(), indent=2))

        # Create manager, load from disk
        provider = _make_mock_provider()
        manager = PlaybookManager(
            chat_provider=provider,
            data_dir=str(tmp_path),
        )
        loaded = await manager.load_from_disk()
        assert loaded == 1

        # Now compile the same markdown — should be skipped
        result = await manager.compile_playbook(SIMPLE_PLAYBOOK_MD)
        assert result.success
        assert result.skipped
        assert result.playbook.version == 1
        # LLM was NOT called
        assert provider.create_message.call_count == 0

    @pytest.mark.asyncio
    async def test_handler_skips_unchanged_file(self, tmp_path: Path) -> None:
        """Vault watcher handler skips compilation when file hasn't changed."""
        from src.playbook_handler import on_playbook_changed

        md_path = tmp_path / "test.md"
        md_path.write_text(SIMPLE_PLAYBOOK_MD)

        provider = _make_mock_provider()
        manager = PlaybookManager(
            chat_provider=provider,
            data_dir=str(tmp_path),
        )

        change = MagicMock()
        change.path = str(md_path)
        change.rel_path = "system/playbooks/test.md"
        change.operation = "created"

        # First call — compiles
        await on_playbook_changed([change], playbook_manager=manager)
        assert manager.get_playbook("test-playbook") is not None
        assert provider.create_message.call_count == 1

        # File unchanged, trigger again — should skip
        change.operation = "modified"
        await on_playbook_changed([change], playbook_manager=manager)
        assert manager.get_playbook("test-playbook").version == 1
        # LLM was NOT called again
        assert provider.create_message.call_count == 1
