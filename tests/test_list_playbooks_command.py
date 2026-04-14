"""Tests for the list_playbooks command (roadmap 5.5.4).

Tests cover:
  (a) Returns all active playbooks across scopes with correct fields
  (b) Returns empty list when no playbooks are loaded
  (c) Returns error when playbook manager is not initialised
  (d) Filters by scope when scope parameter is provided
  (e) Returns error for invalid scope filter
  (f) Includes last run info from database when available
  (g) Includes cooldown state when playbook has cooldown configured
  (h) Includes running count for in-flight playbook runs
  (i) Includes scope_identifier for project-scoped playbooks
  (j) Includes agent_type detail for agent-type scoped playbooks
  (k) Tool registry includes list_playbooks definition
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest

from src.commands.handler import CommandHandler
from src.playbooks.models import CompiledPlaybook, PlaybookNode


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
    cooldown_seconds: int | None = None,
    max_tokens: int | None = None,
    compiled_at: str | None = "2026-01-15T10:00:00Z",
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
        cooldown_seconds=cooldown_seconds,
        max_tokens=max_tokens,
        compiled_at=compiled_at,
    )


@dataclass
class FakePlaybookRun:
    """Minimal stand-in for a PlaybookRun database record."""

    run_id: str = "run-001"
    playbook_id: str = "test-playbook"
    status: str = "completed"
    started_at: float = 1700000000.0
    completed_at: float | None = 1700000060.0
    tokens_used: int = 500
    current_node: str | None = None
    error: str | None = None


def _make_playbook_manager(
    playbooks: dict[str, CompiledPlaybook] | None = None,
    scope_identifiers: dict[str, str | None] | None = None,
    cooldown_remaining: dict[str, float] | None = None,
    runs_for_playbook: dict[str, list[str]] | None = None,
):
    """Create a mock PlaybookManager with configurable state."""
    pm = MagicMock()

    active = playbooks or {}
    pm.active_playbooks = active

    scope_ids = scope_identifiers or {}
    pm.get_scope_identifier = MagicMock(side_effect=lambda pid: scope_ids.get(pid))

    cooldowns = cooldown_remaining or {}
    pm.get_cooldown_remaining = MagicMock(
        side_effect=lambda pid, scope="system": cooldowns.get(pid, 0.0)
    )

    runs = runs_for_playbook or {}
    pm.get_runs_for_playbook = MagicMock(
        side_effect=lambda pid: runs.get(pid, [])
    )

    return pm


def _make_handler(
    *,
    has_playbook_manager: bool = True,
    playbooks: dict[str, CompiledPlaybook] | None = None,
    scope_identifiers: dict[str, str | None] | None = None,
    cooldown_remaining: dict[str, float] | None = None,
    runs_for_playbook: dict[str, list[str]] | None = None,
    db_runs: list[FakePlaybookRun] | None = None,
):
    """Create a CommandHandler with a mock orchestrator and database."""
    mock_orch = MagicMock()
    mock_db = AsyncMock()
    mock_orch.db = mock_db
    mock_config = MagicMock()

    if has_playbook_manager:
        pm = _make_playbook_manager(
            playbooks=playbooks,
            scope_identifiers=scope_identifiers,
            cooldown_remaining=cooldown_remaining,
            runs_for_playbook=runs_for_playbook,
        )
        mock_orch.playbook_manager = pm
    else:
        mock_orch.playbook_manager = None

    # Configure db.list_playbook_runs to return specified runs
    if db_runs is not None:
        mock_db.list_playbook_runs = AsyncMock(return_value=db_runs)
    else:
        mock_db.list_playbook_runs = AsyncMock(return_value=[])

    handler = CommandHandler(mock_orch, mock_config)
    return handler


# ---------------------------------------------------------------------------
# Test: Basic listing
# ---------------------------------------------------------------------------


class TestListPlaybooksBasic:
    """Tests for basic list_playbooks behavior."""

    async def test_returns_all_playbooks(self):
        """list_playbooks returns all active playbooks with correct fields."""
        pb1 = _make_playbook(playbook_id="alpha", triggers=["git.commit"])
        pb2 = _make_playbook(playbook_id="beta", triggers=["task.completed"], scope="project")

        handler = _make_handler(playbooks={"alpha": pb1, "beta": pb2})
        result = await handler._cmd_list_playbooks({})

        assert "playbooks" in result
        assert result["count"] == 2
        ids = [p["id"] for p in result["playbooks"]]
        assert "alpha" in ids
        assert "beta" in ids

    async def test_playbook_fields(self):
        """Each playbook entry contains required fields."""
        pb = _make_playbook(
            playbook_id="code-quality",
            triggers=["git.commit", "task.completed"],
            version=3,
            compiled_at="2026-01-15T10:00:00Z",
        )

        handler = _make_handler(playbooks={"code-quality": pb})
        result = await handler._cmd_list_playbooks({})

        entry = result["playbooks"][0]
        assert entry["id"] == "code-quality"
        assert entry["scope"] == "system"
        assert entry["version"] == 3
        assert entry["compiled_at"] == "2026-01-15T10:00:00Z"
        assert entry["node_count"] == 2
        assert entry["status"] == "active"
        assert "git.commit" in entry["triggers"]
        assert "task.completed" in entry["triggers"]
        assert entry["running_count"] == 0

    async def test_empty_when_no_playbooks(self):
        """Returns empty list and count=0 when no playbooks are loaded."""
        handler = _make_handler(playbooks={})
        result = await handler._cmd_list_playbooks({})

        assert result["playbooks"] == []
        assert result["count"] == 0

    async def test_sorted_by_id(self):
        """Playbooks are returned sorted by ID."""
        pb_z = _make_playbook(playbook_id="zulu")
        pb_a = _make_playbook(playbook_id="alpha")
        pb_m = _make_playbook(playbook_id="mike")

        handler = _make_handler(playbooks={"zulu": pb_z, "alpha": pb_a, "mike": pb_m})
        result = await handler._cmd_list_playbooks({})

        ids = [p["id"] for p in result["playbooks"]]
        assert ids == ["alpha", "mike", "zulu"]


# ---------------------------------------------------------------------------
# Test: Error handling
# ---------------------------------------------------------------------------


class TestListPlaybooksErrors:
    """Tests for error cases."""

    async def test_error_when_no_playbook_manager(self):
        """Returns error when playbook manager is not initialised."""
        handler = _make_handler(has_playbook_manager=False)
        result = await handler._cmd_list_playbooks({})

        assert "error" in result
        assert "not initialised" in result["error"]

    async def test_error_for_invalid_scope_filter(self):
        """Returns error for invalid scope filter value."""
        handler = _make_handler()
        result = await handler._cmd_list_playbooks({"scope": "invalid"})

        assert "error" in result
        assert "Invalid scope" in result["error"]


# ---------------------------------------------------------------------------
# Test: Scope filtering
# ---------------------------------------------------------------------------


class TestListPlaybooksScopeFilter:
    """Tests for scope-based filtering."""

    async def test_filter_by_system_scope(self):
        """scope='system' returns only system-scoped playbooks."""
        pb_sys = _make_playbook(playbook_id="sys-pb", scope="system")
        pb_proj = _make_playbook(playbook_id="proj-pb", scope="project")

        handler = _make_handler(playbooks={"sys-pb": pb_sys, "proj-pb": pb_proj})
        result = await handler._cmd_list_playbooks({"scope": "system"})

        assert result["count"] == 1
        assert result["playbooks"][0]["id"] == "sys-pb"

    async def test_filter_by_project_scope(self):
        """scope='project' returns only project-scoped playbooks."""
        pb_sys = _make_playbook(playbook_id="sys-pb", scope="system")
        pb_proj = _make_playbook(playbook_id="proj-pb", scope="project")

        handler = _make_handler(playbooks={"sys-pb": pb_sys, "proj-pb": pb_proj})
        result = await handler._cmd_list_playbooks({"scope": "project"})

        assert result["count"] == 1
        assert result["playbooks"][0]["id"] == "proj-pb"

    async def test_filter_by_agent_type_scope(self):
        """scope='agent-type' returns only agent-type scoped playbooks."""
        pb_sys = _make_playbook(playbook_id="sys-pb", scope="system")
        pb_agent = _make_playbook(playbook_id="agent-pb", scope="agent-type:coding")

        handler = _make_handler(playbooks={"sys-pb": pb_sys, "agent-pb": pb_agent})
        result = await handler._cmd_list_playbooks({"scope": "agent-type"})

        assert result["count"] == 1
        assert result["playbooks"][0]["id"] == "agent-pb"

    async def test_no_filter_returns_all(self):
        """No scope filter returns all playbooks across all scopes."""
        pb_sys = _make_playbook(playbook_id="sys-pb", scope="system")
        pb_proj = _make_playbook(playbook_id="proj-pb", scope="project")
        pb_agent = _make_playbook(playbook_id="agent-pb", scope="agent-type:coding")

        handler = _make_handler(
            playbooks={"sys-pb": pb_sys, "proj-pb": pb_proj, "agent-pb": pb_agent}
        )
        result = await handler._cmd_list_playbooks({})

        assert result["count"] == 3


# ---------------------------------------------------------------------------
# Test: Enrichment data (last run, cooldown, running, scope details)
# ---------------------------------------------------------------------------


class TestListPlaybooksEnrichment:
    """Tests for enrichment data: last run, cooldown, running count, scope details."""

    async def test_includes_last_run_info(self):
        """last_run is included when database has run history."""
        pb = _make_playbook(playbook_id="code-quality")
        run = FakePlaybookRun(
            run_id="run-abc",
            playbook_id="code-quality",
            status="completed",
            started_at=1700000000.0,
            completed_at=1700000060.0,
            tokens_used=1234,
        )

        handler = _make_handler(
            playbooks={"code-quality": pb},
            db_runs=[run],
        )
        result = await handler._cmd_list_playbooks({})

        entry = result["playbooks"][0]
        assert "last_run" in entry
        assert entry["last_run"]["run_id"] == "run-abc"
        assert entry["last_run"]["status"] == "completed"
        assert entry["last_run"]["tokens_used"] == 1234

    async def test_no_last_run_when_no_history(self):
        """last_run is absent when no runs exist in database."""
        pb = _make_playbook(playbook_id="code-quality")

        handler = _make_handler(playbooks={"code-quality": pb})
        result = await handler._cmd_list_playbooks({})

        entry = result["playbooks"][0]
        assert "last_run" not in entry

    async def test_includes_cooldown_info(self):
        """Cooldown fields are included when playbook has cooldown configured."""
        pb = _make_playbook(
            playbook_id="rate-limited",
            cooldown_seconds=300,
        )

        handler = _make_handler(
            playbooks={"rate-limited": pb},
            cooldown_remaining={"rate-limited": 120.5},
        )
        result = await handler._cmd_list_playbooks({})

        entry = result["playbooks"][0]
        assert entry["cooldown_seconds"] == 300
        assert entry["cooldown_remaining"] == 120.5

    async def test_no_cooldown_remaining_when_not_active(self):
        """cooldown_remaining is absent when cooldown is not active."""
        pb = _make_playbook(
            playbook_id="rate-limited",
            cooldown_seconds=300,
        )

        handler = _make_handler(
            playbooks={"rate-limited": pb},
            cooldown_remaining={"rate-limited": 0.0},
        )
        result = await handler._cmd_list_playbooks({})

        entry = result["playbooks"][0]
        assert entry["cooldown_seconds"] == 300
        assert "cooldown_remaining" not in entry

    async def test_no_cooldown_fields_when_no_cooldown(self):
        """cooldown_seconds is absent when playbook has no cooldown."""
        pb = _make_playbook(playbook_id="no-cooldown")

        handler = _make_handler(playbooks={"no-cooldown": pb})
        result = await handler._cmd_list_playbooks({})

        entry = result["playbooks"][0]
        assert "cooldown_seconds" not in entry
        assert "cooldown_remaining" not in entry

    async def test_includes_running_count(self):
        """running_count reflects in-flight runs."""
        pb = _make_playbook(playbook_id="busy-pb")

        handler = _make_handler(
            playbooks={"busy-pb": pb},
            runs_for_playbook={"busy-pb": ["run-1", "run-2"]},
        )
        result = await handler._cmd_list_playbooks({})

        entry = result["playbooks"][0]
        assert entry["running_count"] == 2

    async def test_includes_scope_identifier_for_project(self):
        """scope_identifier is included for project-scoped playbooks."""
        pb = _make_playbook(playbook_id="proj-pb", scope="project")

        handler = _make_handler(
            playbooks={"proj-pb": pb},
            scope_identifiers={"proj-pb": "my-project"},
        )
        result = await handler._cmd_list_playbooks({})

        entry = result["playbooks"][0]
        assert entry["scope_identifier"] == "my-project"

    async def test_no_scope_identifier_for_system(self):
        """scope_identifier is absent for system-scoped playbooks."""
        pb = _make_playbook(playbook_id="sys-pb", scope="system")

        handler = _make_handler(playbooks={"sys-pb": pb})
        result = await handler._cmd_list_playbooks({})

        entry = result["playbooks"][0]
        assert "scope_identifier" not in entry

    async def test_includes_agent_type_detail(self):
        """agent_type is included for agent-type scoped playbooks."""
        pb = _make_playbook(playbook_id="agent-pb", scope="agent-type:coding")

        handler = _make_handler(playbooks={"agent-pb": pb})
        result = await handler._cmd_list_playbooks({})

        entry = result["playbooks"][0]
        assert entry["agent_type"] == "coding"

    async def test_includes_max_tokens(self):
        """max_tokens is included when configured on the playbook."""
        pb = _make_playbook(playbook_id="budget-pb", max_tokens=50000)

        handler = _make_handler(playbooks={"budget-pb": pb})
        result = await handler._cmd_list_playbooks({})

        entry = result["playbooks"][0]
        assert entry["max_tokens"] == 50000

    async def test_no_max_tokens_when_none(self):
        """max_tokens is absent when not configured."""
        pb = _make_playbook(playbook_id="no-budget")

        handler = _make_handler(playbooks={"no-budget": pb})
        result = await handler._cmd_list_playbooks({})

        entry = result["playbooks"][0]
        assert "max_tokens" not in entry

    async def test_db_error_gracefully_handled(self):
        """Database errors are handled gracefully — last_run is omitted."""
        pb = _make_playbook(playbook_id="db-error-pb")

        handler = _make_handler(playbooks={"db-error-pb": pb})
        # Make db.list_playbook_runs raise
        handler.db.list_playbook_runs = AsyncMock(side_effect=Exception("DB gone"))

        result = await handler._cmd_list_playbooks({})

        assert result["count"] == 1
        entry = result["playbooks"][0]
        assert entry["id"] == "db-error-pb"
        assert "last_run" not in entry


# ---------------------------------------------------------------------------
# Test: Tool registry
# ---------------------------------------------------------------------------


class TestListPlaybooksToolRegistry:
    """Tests for tool registry integration."""

    def test_tool_definition_exists(self):
        """list_playbooks is registered in the tool registry."""
        from src.tools import _ALL_TOOL_DEFINITIONS, _TOOL_CATEGORIES

        # Check category mapping
        assert "list_playbooks" in _TOOL_CATEGORIES
        assert _TOOL_CATEGORIES["list_playbooks"] == "playbook"

        # Check tool definition exists
        tool_names = [t["name"] for t in _ALL_TOOL_DEFINITIONS]
        assert "list_playbooks" in tool_names

    def test_tool_definition_has_scope_enum(self):
        """The list_playbooks tool definition includes scope enum."""
        from src.tools import _ALL_TOOL_DEFINITIONS

        tool = next(t for t in _ALL_TOOL_DEFINITIONS if t["name"] == "list_playbooks")
        scope_prop = tool["input_schema"]["properties"]["scope"]
        assert scope_prop["type"] == "string"
        assert set(scope_prop["enum"]) == {"system", "project", "agent-type"}
