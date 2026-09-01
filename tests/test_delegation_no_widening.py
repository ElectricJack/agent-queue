"""Package 0 T-16 — delegated permissions cannot widen, recursively.

``src/commands/task_commands.py`` documented its own gap in-tree: when
``create_task`` is invoked by a task agent over the embedded MCP server
(HTTP), there was no per-task identity on the request, so
``_caller_profile_id`` was unset and ``_check_capability_escalation`` never
fired. The stated fallback — the harness ``--allowedTools`` flag — did not
apply either, because AQ command names were dropped from that flag entirely.
So the check existed but was unreachable for every real tmux session.

Package 0 derives the principal from the *session row*, which exists for
HTTP and MCP callers too. These tests drive the real ``CommandHandler``
(and the real ``/api/execute`` surface) against a real SQLite database with
real ``sessions`` rows — no stubbed caller identity — because a stub would
re-prove the check in the one place it already worked.
"""

from __future__ import annotations

import time

import httpx
import pytest
from fastapi import FastAPI

from src.api import dependencies as deps
from src.api.auth import SessionTokenStore
from src.api.execute import router as execute_router
from src.api.middleware import RequestContextMiddleware, TokenAuthMiddleware
from src.models import AgentProfile, Project, SessionRecord, Task, TaskStatus

pytestmark = pytest.mark.asyncio


BROAD = AgentProfile(
    id="broad",
    name="Broad",
    harness_tools=["Bash", "Read", "Write", "Edit", "Glob", "Grep", "Task", "WebFetch"],
    aq_commands=[
        "prime", "task_show", "task_comment", "task_close", "task_heartbeat",
        "session_drain_ack", "create_task", "list_tasks", "edit_task", "add_dependency",
    ],
    plugin_tools=["read_file", "write_file", "mcp__github__create_issue"],
)
NARROW = AgentProfile(
    id="narrow",
    name="Narrow",
    harness_tools=["Bash", "Read", "Glob", "Grep"],
    aq_commands=[
        "prime", "task_show", "task_comment", "task_close", "task_heartbeat",
        "session_drain_ack", "create_task",
    ],
    plugin_tools=["read_file"],
)
NARROWER = AgentProfile(
    id="narrower",
    name="Narrower",
    harness_tools=["Bash", "Read"],
    aq_commands=["prime", "task_show", "task_close", "create_task"],
    plugin_tools=[],
)


async def _seed(handler, profiles=(BROAD, NARROW, NARROWER)):
    db = handler.db
    await db.create_project(Project(id="p", name="Project"))
    for profile in profiles:
        await db.create_profile(profile)
    handler._invalidate_principal_cache()
    return db


async def _session_for(handler, profile_id: str, session_id: str | None = None) -> str:
    """A running session holding one task — what a live pool worker is.

    ``create_task`` routes a session-scoped, non-elevated caller down the
    worker-filing path (swarm work model §12), which requires a held task,
    so an idle session would be refused before delegation is ever reached.
    """
    session_id = session_id or f"s-{profile_id}"
    held_id = f"t-{profile_id}"
    now = time.time()
    await handler.db.create_task(
        Task(
            id=held_id,
            project_id="p",
            title=held_id,
            description="held",
            status=TaskStatus.IN_PROGRESS,
            profile_id=profile_id,
        )
    )
    await handler.db.create_session(
        SessionRecord(
            id=session_id,
            project_id="p",
            profile_id=profile_id,
            harness="claude",
            provider="fake",
            name=session_id,
            lifecycle="task",
            task_id=held_id,
            state="running",
            work_dir="/tmp",
            epoch="e1",
            instance_token="tok-" + session_id,
            started_at=now,
            last_activity=now,
        )
    )
    handler._invalidate_principal_cache()
    return session_id


def _scope(session_id: str) -> dict:
    return {
        "kind": "session",
        "session_id": session_id,
        "task_id": None,
        "project_id": "p",
        "elevated": False,
    }


async def _create(handler, session_id: str, **args):
    payload = {
        "project_id": "p",
        "title": "child",
        "description": "d",
        "reason": "delegation test",
        **args,
    }
    payload["_scope"] = _scope(session_id)
    return await handler.execute("create_task", payload)


@pytest.fixture
async def handler(command_handler_factory):
    h = await command_handler_factory()
    h.config.security.capability_enforcement = "enforce"
    await _seed(h)
    return h


class TestRecursiveChain:
    async def test_broad_may_delegate_to_narrow(self, handler):
        sid = await _session_for(handler, "broad")
        result = await _create(handler, sid, profile_id="narrow")
        assert "error" not in result, result

    async def test_narrow_may_not_delegate_to_broad(self, handler):
        sid = await _session_for(handler, "narrow")
        result = await _create(handler, sid, profile_id="broad")
        assert "Capability escalation rejected" in result["error"]

    async def test_narrow_may_delegate_to_narrower(self, handler):
        sid = await _session_for(handler, "narrow")
        result = await _create(handler, sid, profile_id="narrower")
        assert "error" not in result, result

    async def test_narrower_may_not_delegate_to_narrow(self, handler):
        """The third level: narrowing composes, it does not reset."""
        sid = await _session_for(handler, "narrower")
        result = await _create(handler, sid, profile_id="narrow")
        assert "Capability escalation rejected" in result["error"]

    async def test_a_profile_may_delegate_to_itself(self, handler):
        sid = await _session_for(handler, "narrow")
        result = await _create(handler, sid, profile_id="narrow")
        assert "error" not in result, result


class TestPerNamespace:
    async def test_one_extra_plugin_tool_is_an_escalation(self, handler):
        """Equal in two namespaces, one entry wider in the third."""
        await handler.db.create_profile(
            AgentProfile(
                id="plus-plugin",
                name="Plus",
                harness_tools=list(NARROW.harness_tools or []),
                aq_commands=list(NARROW.aq_commands or []),
                plugin_tools=[*(NARROW.plugin_tools or []), "write_file"],
            )
        )
        sid = await _session_for(handler, "narrow")

        result = await _create(handler, sid, profile_id="plus-plugin")

        assert "Capability escalation rejected" in result["error"]
        assert "plugin_tool" in result["error"]

    async def test_one_extra_harness_tool_is_an_escalation(self, handler):
        await handler.db.create_profile(
            AgentProfile(
                id="plus-harness",
                name="Plus",
                harness_tools=[*(NARROW.harness_tools or []), "Write"],
                aq_commands=list(NARROW.aq_commands or []),
                plugin_tools=list(NARROW.plugin_tools or []),
            )
        )
        sid = await _session_for(handler, "narrow")

        result = await _create(handler, sid, profile_id="plus-harness")

        assert "harness tool" in result["error"]


class TestDefaultInheritance:
    async def test_omitting_profile_id_inherits_the_callers(self, handler):
        sid = await _session_for(handler, "narrow")

        result = await _create(handler, sid)

        assert "error" not in result, result
        task = await handler.db.get_task(result["task_id"])
        assert task.profile_id == "narrow"


class TestFailClosed:
    async def test_deleted_profile_refuses_and_writes_nothing(self, handler):
        """Under ``enforce`` the dispatch gate answers first — still nothing written."""
        sid = await _session_for(handler, "narrow")
        await handler.db.delete_profile("narrow")
        handler._invalidate_principal_cache()
        before = len(await handler.db.list_tasks())

        result = await _create(handler, sid, profile_id="narrower")

        assert result.get("error_code") == "capability_denied"
        assert len(await handler.db.list_tasks()) == before

    async def test_deleted_profile_still_cannot_delegate_in_audit_mode(self, handler):
        """The delegation check is an independent gate, not a consequence of
        the dispatch gate.

        In ``audit`` an unresolved identity is shadow-allowed *at dispatch*
        so an un-migrated fleet keeps running — but it must still not be able
        to hand a child task a profile, because there is no parent bound to
        compare against.
        """
        handler.config.security.capability_enforcement = "audit"
        sid = await _session_for(handler, "narrow")
        await handler.db.delete_profile("narrow")
        handler._invalidate_principal_cache()
        before = len(await handler.db.list_tasks())

        result = await _create(handler, sid, profile_id="narrower")

        assert "refusing to create task" in result["error"]
        assert len(await handler.db.list_tasks()) == before

    async def test_session_without_a_profile_cannot_delegate(self, handler):
        """No profile at all on the session row: there is no parent policy to
        compare a child against, so delegation is refused outright."""
        handler.config.security.capability_enforcement = "audit"
        sid = await _session_for(handler, "narrow", session_id="s-noprofile")
        await handler.db.update_session(sid, profile_id="")
        handler._invalidate_principal_cache()
        before = len(await handler.db.list_tasks())

        result = await _create(handler, sid, profile_id="narrower")

        assert result["error"] == "delegation refused: caller has no resolved profile"
        assert len(await handler.db.list_tasks()) == before

    async def test_unknown_profile_id_is_refused(self, handler):
        sid = await _session_for(handler, "narrow")
        result = await _create(handler, sid, profile_id="nope")
        assert result["error"] == "Profile 'nope' not found"


class TestPlaybookShim:
    async def test_set_caller_profile_still_rejects(self, handler):
        """``src/playbooks/runner.py`` is untouched: its shim still gates."""
        handler.set_caller_profile("narrow")
        try:
            result = await handler.execute(
                "create_task",
                {"project_id": "p", "title": "c", "description": "d", "profile_id": "broad"},
            )
        finally:
            handler.set_caller_profile(None)
        assert "Capability escalation rejected" in result["error"]


class TestHttpPath:
    """The exact path task_commands.py documented as unreachable."""

    async def test_escalation_over_api_execute_is_refused_and_writes_nothing(
        self, command_handler_factory, tmp_path
    ):
        ch = await command_handler_factory()
        ch.config.security.capability_enforcement = "enforce"
        await _seed(ch)
        sid = await _session_for(ch, "narrow")

        store = SessionTokenStore(ch.db, ttl_hours=1)
        token = await store.mint(session_id=sid, task_id=None, project_id="p")
        deps._orchestrator = ch.orchestrator
        deps._command_handler = ch
        deps._token_store = store
        deps._require_session_token = False

        app = FastAPI()
        app.include_router(execute_router)
        app.add_middleware(RequestContextMiddleware)
        app.add_middleware(TokenAuthMiddleware)

        before = len(await ch.db.list_tasks())
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/execute",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "command": "create_task",
                        "args": {
                            "project_id": "p",
                            "title": "escalate",
                            "description": "d",
                            "reason": "delegation test",
                            "profile_id": "broad",
                        },
                    },
                )
        finally:
            deps._orchestrator = None
            deps._command_handler = None
            deps._token_store = None

        assert "Capability escalation rejected" in response.json()["error"]
        assert len(await ch.db.list_tasks()) == before

    async def test_client_supplied_profile_id_key_cannot_spoof_the_caller(self, handler):
        """``_profile_id`` in the body is stripped, not honoured."""
        sid = await _session_for(handler, "narrow")

        result = await _create(
            handler, sid, profile_id="broad", _profile_id="broad", _policy={"aq_commands": ["*"]}
        )

        assert "Capability escalation rejected" in result["error"]
