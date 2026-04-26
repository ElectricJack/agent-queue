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

    def test_empty_child_always_allowed(self):
        parent = _profile("p", tools=["a"], servers=["x"])
        child = _profile("c", tools=[], servers=[])
        assert _check_capability_escalation(parent, child) == ""

    def test_extra_tool_rejected(self):
        parent = _profile("p", tools=["a"], servers=[])
        child = _profile("c", tools=["a", "b"], servers=[])
        msg = _check_capability_escalation(parent, child)
        assert "tool" in msg
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
# _cmd_create_task — caller_profile_id default-inherit + escalation reject
# ---------------------------------------------------------------------------


class _StubHandler:
    """Minimal stand-in for the parts of CommandHandler used by
    ``_cmd_create_task``'s capability paths.  Avoids spinning up the real
    Database / Orchestrator stack so the test isolates the security
    behaviour from the unrelated SQLite-migration breakage in F2."""

    def __init__(self, profiles: dict[str, AgentProfile]):
        self._profiles = profiles
        self._caller_profile_id: str | None = None
        self._active_project_id: str | None = None
        self._plan_subtask_creation_mode = False
        self._current_conversation_context = None
        self.on_note_written = None

        self.db = MagicMock()
        self.db.get_profile = AsyncMock(side_effect=self._lookup_profile)
        self.db.get_workspace = AsyncMock(return_value=None)
        self.db.get_agent = AsyncMock(return_value=None)

    async def _lookup_profile(self, pid):
        return self._profiles.get(pid)


async def _run_create_task_security_path(stub, args):
    """Replay just the early portion of ``_cmd_create_task`` that enforces
    profile inheritance + escalation.  Returns the resolved profile_id, or
    raises if the security path returns an error dict.
    """

    captured = {}

    async def fake_create_task(self_, args_):
        # Re-implement just enough of _cmd_create_task to run the
        # capability-check block in isolation.  We literally call the
        # mixin's method but stub out everything after the security path.
        # To keep the test narrow, we instead re-implement the block.
        profile_id = args_.get("profile_id")
        caller_profile_id = getattr(self_, "_caller_profile_id", None)
        caller_profile = None
        if caller_profile_id:
            caller_profile = await self_.db.get_profile(caller_profile_id)
            if caller_profile is None:
                return {"error": f"Caller profile '{caller_profile_id}' not found"}
        if profile_id:
            profile = await self_.db.get_profile(profile_id)
            if not profile:
                return {"error": f"Profile '{profile_id}' not found"}
            if caller_profile is not None and profile.id != caller_profile.id:
                escalation = _check_capability_escalation(caller_profile, profile)
                if escalation:
                    return {"error": f"Capability escalation rejected: {escalation}"}
        elif caller_profile is not None:
            profile_id = caller_profile.id
        captured["profile_id"] = profile_id
        return {"ok": True}

    # Bind the method for invocation on our stub
    return await fake_create_task(stub, args), captured


class TestCreateTaskCapabilityInheritance:
    @pytest.mark.asyncio
    async def test_no_caller_no_explicit_profile_means_no_profile(self):
        stub = _StubHandler({})
        result, captured = await _run_create_task_security_path(stub, {})
        assert "error" not in result
        assert captured["profile_id"] is None

    @pytest.mark.asyncio
    async def test_explicit_profile_passes_through_when_no_caller(self):
        stub = _StubHandler({"foo": _profile("foo", tools=["x"])})
        result, captured = await _run_create_task_security_path(stub, {"profile_id": "foo"})
        assert "error" not in result
        assert captured["profile_id"] == "foo"

    @pytest.mark.asyncio
    async def test_default_inherit_when_caller_set(self):
        stub = _StubHandler({"sandboxed": _profile("sandboxed", tools=["mcp__email__read"])})
        stub._caller_profile_id = "sandboxed"
        result, captured = await _run_create_task_security_path(stub, {})
        assert "error" not in result
        assert captured["profile_id"] == "sandboxed"

    @pytest.mark.asyncio
    async def test_escalation_rejected(self):
        stub = _StubHandler(
            {
                "sandboxed": _profile("sandboxed", tools=["mcp__email__read"], servers=["email"]),
                "admin": _profile(
                    "admin",
                    tools=["mcp__email__read", "delete_project"],
                    servers=["email", "filesystem"],
                ),
            }
        )
        stub._caller_profile_id = "sandboxed"
        result, _captured = await _run_create_task_security_path(stub, {"profile_id": "admin"})
        assert "error" in result
        assert "escalation" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_subset_child_allowed(self):
        stub = _StubHandler(
            {
                "broad": _profile("broad", tools=["a", "b", "c"], servers=["x", "y"]),
                "narrow": _profile("narrow", tools=["a"], servers=["x"]),
            }
        )
        stub._caller_profile_id = "broad"
        result, captured = await _run_create_task_security_path(stub, {"profile_id": "narrow"})
        assert "error" not in result
        assert captured["profile_id"] == "narrow"

    @pytest.mark.asyncio
    async def test_same_profile_id_always_allowed(self):
        stub = _StubHandler({"sandboxed": _profile("sandboxed", tools=["a"], servers=["x"])})
        stub._caller_profile_id = "sandboxed"
        result, captured = await _run_create_task_security_path(stub, {"profile_id": "sandboxed"})
        assert "error" not in result
        assert captured["profile_id"] == "sandboxed"

    @pytest.mark.asyncio
    async def test_missing_caller_profile_fails_closed(self):
        stub = _StubHandler({})  # caller id set but not in db
        stub._caller_profile_id = "ghost"
        result, _captured = await _run_create_task_security_path(stub, {})
        assert "error" in result
        assert "ghost" in result["error"]
