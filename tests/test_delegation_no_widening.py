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

Mandatory task routing (spec 2026-09-28 §5.1, §5.2) narrowed what reaches the
check: no filer names a worker profile any more, so a session naming a
profile is refused with ``routing.choice_forbidden`` before delegation is
considered.  The only profile a filing may name is a *role* profile
(``triage``, ``spec-ingest``, ``reviewer``, ``final-reviewer``) named by a
``PLAYBOOK`` or ``SERVICE`` principal, and that delegation is still bounded
by the caller's capabilities — which is what the recursive tests below prove,
with role profiles standing in for the delegated children.
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
from tests.assignment_routing_helpers import route_source_for

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
            profile_id=profile_id, route_source=route_source_for(profile_id),
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


#: Role profiles (spec §4, D3) carrying the capability shapes above: the only
#: profiles a filing may still name, and only from a playbook or a service.
ROLE_BROAD = AgentProfile(
    id="triage", name="Triage", harness_tools=list(BROAD.harness_tools or []),
    aq_commands=list(BROAD.aq_commands or []), plugin_tools=list(BROAD.plugin_tools or []),
)
ROLE_NARROW = AgentProfile(
    id="spec-ingest", name="Spec ingest", harness_tools=list(NARROW.harness_tools or []),
    aq_commands=list(NARROW.aq_commands or []), plugin_tools=list(NARROW.plugin_tools or []),
)
ROLE_NARROWER = AgentProfile(
    id="final-reviewer", name="Final reviewer",
    harness_tools=list(NARROWER.harness_tools or []),
    aq_commands=list(NARROWER.aq_commands or []), plugin_tools=list(NARROWER.plugin_tools or []),
)


def _playbook(profile_id: str | None):
    """An enforced playbook principal running under *profile_id*."""
    from src.commands.principal import ExecutionPrincipal, PrincipalKind
    from src.profiles.capabilities import CapabilityPolicy

    return ExecutionPrincipal(
        kind=PrincipalKind.PLAYBOOK,
        policy=CapabilityPolicy.from_namespaces(aq_commands=["create_task"]),
        project_id="p",
        profile_id=profile_id,
    )


async def _delegate(handler, caller: str | None, role: str) -> dict:
    """A playbook running as *caller* files a task on the *role* profile."""
    from src.commands.principal import principal_context

    with principal_context(_playbook(caller)):
        return await handler.execute(
            "create_task",
            {"project_id": "p", "title": "child", "description": "d", "profile_id": role},
        )


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
    await _seed(h, (BROAD, NARROW, NARROWER, ROLE_BROAD, ROLE_NARROW, ROLE_NARROWER))
    return h


class TestRecursiveChain:
    """A role delegation from a playbook still cannot widen, recursively."""

    async def test_broad_may_delegate_to_narrow(self, handler):
        result = await _delegate(handler, "broad", "spec-ingest")
        assert "error" not in result, result
        task = await handler.db.get_task(result["task_id"])
        assert (task.profile_id, task.route_source) == ("spec-ingest", "role")

    async def test_narrow_may_not_delegate_to_broad(self, handler):
        result = await _delegate(handler, "narrow", "triage")
        assert "Capability escalation rejected" in result["error"]

    async def test_narrow_may_delegate_to_narrower(self, handler):
        result = await _delegate(handler, "narrow", "final-reviewer")
        assert "error" not in result, result

    async def test_narrower_may_not_delegate_to_narrow(self, handler):
        """The third level: narrowing composes, it does not reset."""
        result = await _delegate(handler, "narrower", "spec-ingest")
        assert "Capability escalation rejected" in result["error"]

    async def test_a_profile_may_delegate_to_its_equal(self, handler):
        result = await _delegate(handler, "narrow", "spec-ingest")
        assert "error" not in result, result

    async def test_a_session_may_not_delegate_at_all(self, handler):
        """A worker names no profile, not even a narrower one (spec §5.2)."""
        sid = await _session_for(handler, "broad")
        before = len(await handler.db.list_tasks())

        for profile_id in ("narrow", "spec-ingest"):
            result = await _create(handler, sid, profile_id=profile_id)
            assert result.get("code") == "routing.choice_forbidden", result
        assert len(await handler.db.list_tasks()) == before


class TestPerNamespace:
    async def test_one_extra_plugin_tool_is_an_escalation(self, handler):
        """Equal in two namespaces, one entry wider in the third."""
        await handler.db.create_profile(
            AgentProfile(
                id="reviewer",
                name="Plus",
                harness_tools=list(NARROW.harness_tools or []),
                aq_commands=list(NARROW.aq_commands or []),
                plugin_tools=[*(NARROW.plugin_tools or []), "write_file"],
            )
        )

        result = await _delegate(handler, "narrow", "reviewer")

        assert "Capability escalation rejected" in result["error"]
        assert "plugin_tool" in result["error"]

    async def test_one_extra_harness_tool_is_an_escalation(self, handler):
        await handler.db.create_profile(
            AgentProfile(
                id="reviewer",
                name="Plus",
                harness_tools=[*(NARROW.harness_tools or []), "Write"],
                aq_commands=list(NARROW.aq_commands or []),
                plugin_tools=list(NARROW.plugin_tools or []),
            )
        )

        result = await _delegate(handler, "narrow", "reviewer")

        assert "harness tool" in result["error"]


class TestDefaultInheritance:
    """A worker's profile bounds what it may name, never where its filing runs."""

    async def test_worker_filing_without_a_profile_is_routed_not_inherited(self, handler):
        sid = await _session_for(handler, "narrow")

        result = await _create(handler, sid)

        assert "error" not in result, result
        task = await handler.db.get_task(result["task_id"])
        assert task.profile_id is None
        assert await handler.db.get_task_meta(task.id, "filed_by_profile_id") == "narrow"

    async def test_worker_filing_with_a_class_is_routed_not_class_matched(self, handler):
        """A class is a hint: the filing is stored unrouted and the router
        routes it (mandatory-routing spec §5.3)."""
        handler._validate_routing_class = lambda *_args, **_kwargs: None
        sid = await _session_for(handler, "narrow")

        result = await _create(handler, sid, intelligence_class="c")

        assert "error" not in result, result
        assert "profile_source" not in result
        task = await handler.db.get_task(result["task_id"])
        assert task.profile_id is None
        assert (task.intelligence_class, task.class_hint) == (None, "c")
        assert task.route_source == "unrouted"
        assert await handler.db.get_task_meta(task.id, "filed_by_profile_id") == "narrow"

    async def test_an_explicit_profile_from_a_worker_is_refused(self, handler):
        """A worker's profile was a preference; now it is a routing choice,
        which only the router makes (spec §5.2)."""
        sid = await _session_for(handler, "narrow")
        before = len(await handler.db.list_tasks())

        result = await _create(handler, sid, profile_id="narrower")

        assert result.get("code") == "routing.choice_forbidden", result
        assert result["refused"] == ["profile_id"]
        assert len(await handler.db.list_tasks()) == before

    async def test_a_sandboxed_playbook_no_longer_inherits(self, handler):
        """No filer inherits its own profile (mandatory-routing spec §2): a
        playbook's unprofiled filing is unrouted, and the router offers only
        worker candidates, so the child still cannot exceed a worker route."""
        handler.set_caller_profile("narrow")
        try:
            result = await handler.execute(
                "create_task", {"project_id": "p", "title": "c", "description": "d"}
            )
        finally:
            handler.set_caller_profile(None)

        assert "error" not in result, result
        task = await handler.db.get_task(result["task_id"])
        assert (task.profile_id, task.route_source) == (None, "unrouted")


class TestWorkerFilingRouteBound:
    """A worker filing is routed only by the router, among worker candidates.

    The old per-filing bound (``_worker_filed_route_error``) went with manual
    routes (mandatory routing §5.2): the router offers only worker candidates,
    no playbook but the bound router writes a route, and the one manual route,
    the override, refuses control and role profiles for every caller.
    """

    CONTROL = AgentProfile(id="reviewer", name="Reviewer", harness="claude")

    async def _filed(self, handler) -> str:
        await handler.db.create_profile(self.CONTROL)
        sid = await _session_for(handler, "narrow")
        result = await _create(handler, sid)
        assert "error" not in result, result
        return result["task_id"]

    async def test_a_playbook_may_not_route_a_filing(self, handler):
        from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
        from src.profiles.capabilities import CapabilityPolicy

        task_id = await self._filed(handler)
        playbook = ExecutionPrincipal(
            kind=PrincipalKind.PLAYBOOK,
            policy=CapabilityPolicy.from_namespaces(aq_commands=["task_route"]),
            project_id="p",
        )
        with principal_context(playbook):
            result = await handler._cmd_task_route({"task_id": task_id})

        assert result["code"] == "routing.not_permitted", result
        assert (await handler.db.get_task(task_id)).route_source == "unrouted"

    async def test_no_caller_may_override_a_filing_onto_a_role_profile(self, handler):
        task_id = await self._filed(handler)

        result = await handler._cmd_task_route_override({
            "task_id": task_id, "profile_id": "reviewer", "intelligence_class": "c",
            "reason": "the operator tries a review profile",
        })

        assert result["success"] is False
        assert "role profile" in result["error"]
        assert (await handler.db.get_task(task_id)).profile_id is None


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

        # The session itself names no profile at all (spec §5.2) ...
        result = await _create(handler, sid, profile_id="narrower")
        assert result.get("code") == "routing.choice_forbidden", result

        # ... and a playbook whose profile is gone cannot hand out a role.
        result = await _delegate(handler, "narrow", "final-reviewer")
        assert "refusing to create task" in result["error"]
        assert len(await handler.db.list_tasks()) == before

    async def test_unresolvable_caller_may_still_file_work_it_names_no_profile_for(
        self, handler
    ):
        """The refusal is about an *unbounded grant*, not about filing at all.

        A worker filing discovered work (``aq task create`` with no
        ``--profile``) asks for no capability grant, so there is nothing to
        bound and nothing that could be widened.  Refusing it would delete a
        capability that existed before this package — ``_caller_profile_id``
        was only ever set by the playbook runner, so every HTTP/MCP filing
        reached ``create_task`` with no caller profile — and would strand an
        un-migrated fleet in the *shipped default* mode, which is exactly
        what ``audit`` exists to prevent (child plan §3.6).

        Under ``enforce`` the question never arises: the dispatch gate denies
        an unresolved principal first, as
        ``test_deleted_profile_refuses_and_writes_nothing`` shows.
        """
        handler.config.security.capability_enforcement = "audit"
        sid = await _session_for(handler, "narrow")
        await handler.db.delete_profile("narrow")
        handler._invalidate_principal_cache()

        result = await _create(handler, sid)

        assert result.get("error") is None, result
        # No profile was granted — the child inherits nothing, which is
        # strictly narrower than any grant the refusal was protecting.
        task = await handler.db.get_task(result["task_id"])
        assert not task.profile_id

    async def test_session_without_a_profile_cannot_delegate(self, handler):
        """No profile at all on the session row: there is no parent policy to
        compare a child against, so delegation is refused outright."""
        handler.config.security.capability_enforcement = "audit"
        sid = await _session_for(handler, "narrow", session_id="s-noprofile")
        await handler.db.update_session(sid, profile_id="")
        handler._invalidate_principal_cache()
        before = len(await handler.db.list_tasks())

        result = await _create(handler, sid, profile_id="narrower")
        assert result.get("code") == "routing.choice_forbidden", result

        # A playbook principal with no profile has no parent policy either.
        result = await _delegate(handler, None, "final-reviewer")
        assert result["error"] == "delegation refused: caller has no resolved profile"
        assert len(await handler.db.list_tasks()) == before

    async def test_unknown_profile_id_is_refused(self, handler):
        sid = await _session_for(handler, "narrow")
        result = await _create(handler, sid, profile_id="nope")
        assert result.get("code") == "routing.choice_forbidden", result
        # A role id with no profile row is refused as unknown.
        result = await _delegate(handler, "broad", "reviewer")
        assert result["error"] == "Profile 'reviewer' not found"


class TestPlaybookShim:
    async def test_set_caller_profile_still_rejects(self, handler):
        """``src/playbooks/runner.py`` is untouched: its shim still gates.

        Through ``execute`` the operator principal names no profile at all;
        an in-process caller reaching the handler directly may name a role,
        still bounded by the shim's caller profile.
        """
        handler.set_caller_profile("narrow")
        try:
            refused = await handler.execute(
                "create_task",
                {"project_id": "p", "title": "c", "description": "d", "profile_id": "broad"},
            )
            result = await handler._cmd_create_task(
                {"project_id": "p", "title": "c", "description": "d", "profile_id": "triage"},
            )
        finally:
            handler.set_caller_profile(None)
        assert refused.get("code") == "routing.choice_forbidden", refused
        assert "Capability escalation rejected" in result["error"]


class TestHttpPath:
    """The exact path task_commands.py documented as unreachable."""

    async def test_a_profile_over_api_execute_is_refused_and_writes_nothing(
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

        # A session names no profile (spec §5.2): refused before delegation.
        assert response.json()["details"]["code"] == "routing.choice_forbidden"
        assert len(await ch.db.list_tasks()) == before

    async def test_client_supplied_profile_id_key_cannot_spoof_the_caller(self, handler):
        """``_profile_id`` in the body is stripped, not honoured."""
        sid = await _session_for(handler, "narrow")

        result = await _create(
            handler, sid, profile_id="broad", _profile_id="broad", _policy={"aq_commands": ["*"]}
        )
        assert result.get("code") == "routing.choice_forbidden", result

        # Without a profile the spoofed keys grant the child nothing either.
        result = await _create(handler, sid, _profile_id="broad", _policy={"aq_commands": ["*"]})
        assert "error" not in result, result
        task = await handler.db.get_task(result["task_id"])
        assert (task.profile_id, task.route_source) == (None, "unrouted")
