"""Tests for capability inheritance + no-upward-escalation in ``_cmd_create_task``.

When a sandboxed caller (a playbook with ``profile_id:`` set, or a task
running under a profile) invokes ``create_task``, the runtime enforces:

  1. **Default inheritance** — child task inherits the caller's
     ``profile_id`` when no explicit one is given.
  2. **No upward escalation** — explicit child ``profile_id`` must have
     ``allowed_tools`` and ``mcp_servers`` that are subsets of the
     caller's.

This blocks the confused-deputy attack where prompt-injected text in a
sandboxed playbook says "create a task with ``profile_id=admin``."  See
``docs/specs/design/sandboxed-playbooks.md``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.commands.task_commands import _check_capability_escalation
from src.models import AgentProfile


def _profile(pid: str, *, tools: list[str] | None = None, servers: list[str] | None = None):
    return AgentProfile(
        id=pid,
        name=pid,
        allowed_tools=tools or [],
        mcp_servers=servers or [],
    )


# ---------------------------------------------------------------------------
# _check_capability_escalation — pure subset semantics
# ---------------------------------------------------------------------------


class TestCheckCapabilityEscalation:
    def test_strict_subset_allowed(self):
        parent = _profile("p", tools=["a", "b", "c"], servers=["x", "y"])
        child = _profile("c", tools=["a", "b"], servers=["x"])
        assert _check_capability_escalation(parent, child) == ""

    def test_equal_capabilities_allowed(self):
        parent = _profile("p", tools=["a", "b"], servers=["x"])
        child = _profile("c", tools=["a", "b"], servers=["x"])
        assert _check_capability_escalation(parent, child) == ""

    def test_child_declaring_nothing_is_not_automatically_narrower(self):
        """An empty legacy ``allowed_tools`` means the CLI's own defaults.

        The old check treated "child declares nothing" as strictest and
        always allowed it. That was the wrong reading: under legacy
        semantics an empty list meant *emit no allowlist flag*, i.e. the
        harness defaults plus AGENT_COMMAND_SET — broader than a narrow
        parent, not narrower. Playbook V2 Package 0 §3.3 makes that grant
        explicit, so delegating to such a profile is correctly refused.
        """
        parent = _profile("p", tools=["a"], servers=["x"])
        child = _profile("c", tools=[], servers=[])
        assert _check_capability_escalation(parent, child) != ""

    def test_explicitly_empty_child_is_allowed(self):
        """``[]`` in an authored ``## Capabilities`` block does mean none."""
        parent = AgentProfile(
            id="p", name="p", harness_tools=["Bash"], aq_commands=["a"], plugin_tools=[]
        )
        child = AgentProfile(
            id="c", name="c", harness_tools=[], aq_commands=[], plugin_tools=[]
        )
        assert _check_capability_escalation(parent, child) == ""

    def test_escalation_is_caught_in_every_namespace(self):
        base = {"harness_tools": ["Bash"], "aq_commands": ["a"], "plugin_tools": ["read_file"]}
        parent = AgentProfile(id="p", name="p", **base)
        for ns, label in (
            ("harness_tools", "harness tool"),
            ("aq_commands", "aq_command"),
            ("plugin_tools", "plugin_tool"),
        ):
            kwargs = dict(base)
            kwargs[ns] = [*kwargs[ns], "extra"]
            child = AgentProfile(id="c", name="c", **kwargs)
            msg = _check_capability_escalation(parent, child)
            assert label in msg, (ns, msg)
            assert "'extra'" in msg

    def test_extra_tool_rejected(self):
        parent = _profile("p", tools=["a"], servers=[])
        child = _profile("c", tools=["a", "b"], servers=[])
        msg = _check_capability_escalation(parent, child)
        # The message now names the namespace the escalation happened in,
        # so an operator can see which kind of capability was widened.
        assert "aq_command" in msg
        assert "'b'" in msg

    def test_extra_server_rejected(self):
        parent = _profile("p", tools=[], servers=["x"])
        child = _profile("c", tools=[], servers=["x", "y"])
        msg = _check_capability_escalation(parent, child)
        assert "MCP server" in msg
        assert "'y'" in msg

    def test_empty_parent_blocks_any_child_capability(self):
        parent = _profile("p", tools=[], servers=[])
        child = _profile("c", tools=["a"], servers=[])
        assert _check_capability_escalation(parent, child) != ""


# ---------------------------------------------------------------------------
# _cmd_create_task — end-to-end via CommandHandler.execute
# ---------------------------------------------------------------------------


@pytest.fixture
async def env(tmp_path):
    """Real Database, Orchestrator, and CommandHandler.

    Profiles created:
      - "pipeline" : the playbook caller (narrow capabilities)
      - "triage"   : a role profile broader than pipeline (escalation case)
      - "reviewer" : a role profile narrower than pipeline (success case)
    """
    from src.commands.handler import CommandHandler
    from src.config import AppConfig, DatabaseConfig
    from src.database import Database
    from src.models import Project
    from src.orchestrator import Orchestrator
    from src.vault import ensure_default_intelligence_classes
    from tests.db_fixtures import lease_dsn

    dsn = lease_dsn("task-cap-heritage.db")
    db = Database(dsn)
    await db.initialize()

    await db.create_project(Project(id="p", name="Project"))

    # pipeline: caller -- 2 tools, 1 server
    await db.create_profile(AgentProfile(
        id="pipeline", name="pipeline", harness="claude",
        default_class="standard-high", needs_workspace=False,
        allowed_tools=["a", "b"],
        mcp_servers=["s"],
    ))
    # triage: broader -- 3 tools, 1 server (escalation: tool 'c' extra)
    await db.create_profile(AgentProfile(
        id="triage", name="triage", harness="claude",
        default_class="standard-high", needs_workspace=False,
        allowed_tools=["a", "b", "c"],
        mcp_servers=["s"],
    ))
    # reviewer: narrower -- 1 tool, 0 servers
    await db.create_profile(AgentProfile(
        id="reviewer", name="reviewer", harness="claude",
        default_class="standard-high", needs_workspace=False,
        allowed_tools=["a"],
        mcp_servers=[],
    ))

    data_dir = str(tmp_path / "data")
    ensure_default_intelligence_classes(data_dir)

    config = AppConfig(data_dir=data_dir, database=DatabaseConfig(url=dsn))
    orch = Orchestrator(config)
    orch.db = db
    orch.git = MagicMock()
    orch._emit_notify = AsyncMock()
    handler = CommandHandler(orch, config)

    yield handler, db

    await db.close()


def _principal(name: str):
    from src.commands.authorization import required_capability
    from src.commands.principal import ExecutionPrincipal, PrincipalKind
    from src.profiles.capabilities import CapabilityPolicy
    from src.routing.filing import FILING_COMMANDS

    policy = CapabilityPolicy.from_namespaces(
        aq_commands=sorted(
            set(FILING_COMMANDS) | {required_capability(n) for n in FILING_COMMANDS}
        )
    )
    if name == "playbook":
        return ExecutionPrincipal(
            kind=PrincipalKind.PLAYBOOK, policy=policy, profile_id="pipeline"
        )
    return ExecutionPrincipal(kind=PrincipalKind.SESSION, policy=policy)


async def _execute(handler, principal, args: dict) -> dict:
    from src.commands.principal import principal_context

    if principal is None:
        return await handler.execute("create_task", dict(args))
    with principal_context(principal):
        return await handler.execute("create_task", dict(args))


def _surface_args() -> dict:
    return {"project_id": "p", "title": "T", "description": "d"}


class TestCreateTaskCapabilityInheritance:
    """End-to-end create_task security paths via CommandHandler.execute."""

    @pytest.mark.asyncio
    async def test_unprofiled_filing_by_operator_leaves_task_unrouted(self, env):
        """Operator (no profile) creates a task: profile_id=None, route_source='unrouted'."""
        handler, db = env
        result = await _execute(handler, None, _surface_args())
        assert "error" not in result, result
        task_id = result.get("task_id") or result.get("created")
        task = await db.get_task(task_id)
        assert task.profile_id is None
        assert task.route_source == "unrouted"

    @pytest.mark.asyncio
    async def test_playbook_filing_naming_broader_role_profile_is_rejected(self, env):
        """Playbook caller names 'triage' which is a superset: escalation rejected."""
        handler, _ = env
        from src.commands.principal import principal_context

        principal = _principal("playbook")
        args = {**_surface_args(), "profile_id": "triage"}
        with principal_context(principal):
            result = await handler.execute("create_task", args)
        # 'triage' passes the routing gate (it is a role profile the playbook
        # may name), so this must be stopped by the capability check: the
        # child's allowlist is a superset of the caller's.
        assert "error" in result, result
        assert "escalation" in str(result["error"]).lower(), result

    @pytest.mark.asyncio
    async def test_playbook_filing_naming_narrower_role_profile_succeeds(self, env):
        """Playbook caller names 'reviewer' which is a subset: succeeds with route_source='role'."""
        handler, db = env
        from src.commands.principal import principal_context

        principal = _principal("playbook")
        args = {**_surface_args(), "profile_id": "reviewer"}
        with principal_context(principal):
            result = await handler.execute("create_task", args)
        assert "error" not in result, result
        assert result.get("task_id") is not None, result
        task = await db.get_task(result["task_id"])
        assert task.profile_id == "reviewer"
        assert task.route_source == "role"


# ---------------------------------------------------------------------------
# Direct-escalation-check unit tests with real DB round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_escalation_fire_end_to_end_via_db(env):
    """Confirm the escalation fires when the DB returns profiles from create_task path."""
    _handler, db = env
    caller_profile = await db.get_profile("pipeline")
    child_profile = await db.get_profile("triage")
    escalation = _check_capability_escalation(caller_profile, child_profile)
    assert escalation != ""

    child_profile2 = await db.get_profile("reviewer")
    escalation2 = _check_capability_escalation(caller_profile, child_profile2)
    assert escalation2 == ""
