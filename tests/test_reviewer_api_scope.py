"""A reviewer token can execute its documented reject path, and nothing more.

The reviewer profile documents exactly two verdicts: approve (``task_close``
on its own review task) and reject (``reopen_with_feedback`` on the *reviewed*
task, then ``task_close``).  The reject half needs a command outside
``AGENT_COMMAND_SET`` aimed at a task other than the token's own — so it needs
a verified carve-out, shaped like the triage one but scoped to the single task
this review was spawned for rather than to the project's whole queue.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from src.api import dependencies as deps
from src.api.auth import SessionTokenStore
from src.api.codegen import build_category_routers
from src.api.execute import router as execute_router
from src.api.middleware import TokenAuthMiddleware
from src.api.scope import _FINAL_REVIEWER_COMMANDS, _TRIAGE_COMMANDS
from src.commands.handler import CommandHandler
from src.config import DatabaseConfig, AppConfig, DiscordConfig
from src.git.manager import PullRequestIdentity
from src.git.github_contracts import GitHubRepositoryBinding
from src.models import Agent, AgentProfile, AgentState, Project, SessionRecord, Task, TaskStatus
from src.orchestrator import Orchestrator
from src.vault import ensure_default_intelligence_classes
from tests.db_fixtures import lease_dsn
from tests.assignment_routing_helpers import route_source_for

#: The identity ``pr_merge`` resolves for the final reviewer's PR.  ``gh``
#: never runs in this test, so the OIDs only have to be well-formed.
PR_URL = "https://github.com/o/r/pull/1"
REPOSITORY = GitHubRepositoryBinding(1, "o/r")
PR_IDENTITY = PullRequestIdentity(
    repository="o/r",
    number=1,
    base_ref="main",
    base_oid="a" * 40,
    head_ref="feature/reviewed",
    head_oid="b" * 40,
    changed_files=1,
)

pytestmark = pytest.mark.asyncio

@pytest.fixture(scope="module")
def api_app():
    """Share the route tree; authentication and handlers resolve per request.

    Each case still owns its database adapter, tokens, mocks and HTTP client.
    No lifespan runs here and no loop-bound resources live on this application.
    """
    app = FastAPI()
    app.include_router(execute_router)
    paths = {}
    for router in build_category_routers():
        app.include_router(router)
        for route in router.routes:
            paths[route.operation_id] = route.path
    app.add_middleware(TokenAuthMiddleware)
    return app, paths


@pytest.fixture(params=["execute", "typed"])
async def api(tmp_path, monkeypatch, request, api_app, reuse_database):
    db = await reuse_database("reviewer-auth.db")
    data_dir = str(tmp_path / "data")
    ensure_default_intelligence_classes(data_dir)
    config = AppConfig(
        discord=DiscordConfig(bot_token="test", guild_id="1"),
        database=DatabaseConfig(url=lease_dsn("reviewer-auth.db")),
        workspace_dir=str(tmp_path / "workspaces"),
        data_dir=data_dir,
    )
    orch = Orchestrator(config)
    orch.db = db
    handler = CommandHandler(orch, config)
    for pid in ("p", "other"):
        await db.create_project(Project(
            id=pid, name=pid, repo_url=f"https://github.com/o/{'r' if pid == 'p' else pid}.git",
        ))
    for profile_id in ("reviewer", "final-reviewer", "coder"):
        await db.upsert_profile(AgentProfile(
            id=profile_id, name=profile_id, harness="codex", needs_workspace=False,
            default_class="standard-low",
            allowed_tools=["pr_merge", "git_diff"] if profile_id == "final-reviewer" else [],
        ))
    # The reviewed work, plus an unrelated completed task in the same project
    # and one in a second project.  All three are reopenable by construction —
    # only scope may separate them.
    for tid, pid in (("reviewed", "p"), ("unrelated", "p"), ("foreign", "other")):
        await db.create_task(Task(
            id=tid, project_id=pid, title=tid, description="Worker output",
            status=TaskStatus.DEFINED, profile_id="coder",
            route_source="legacy", branch_name=f"feature/{tid}",
            pr_url=(PR_URL if tid == "reviewed" else None),
        ))
        await db.transition_task(tid, TaskStatus.COMPLETED, context="test")

    for worker, role, description in (
        ("reviewer-agent", "reviewer", "Reviewing task: reviewed\nBranch: feature/reviewed"),
        ("worker-agent", "coder", "Assigned work"),
    ):
        await db.create_agent(Agent(id=worker, name=worker, profile_id=role))
        await db.create_task(Task(
            id=f"{worker}-job", project_id="p", title=worker, description=description,
            status=TaskStatus.IN_PROGRESS, profile_id=role,
            route_source=route_source_for(role), assigned_agent_id=worker,
        ))
        await db.update_agent(worker, state=AgentState.BUSY, current_task_id=f"{worker}-job")
        await db.create_session(SessionRecord(
            id=f"s-{worker}", task_id=f"{worker}-job", project_id="p",
            agent_id=worker, profile_id=role, harness="codex", provider="fake",
            name=f"s-{worker}", lifecycle="task", state="running",
            work_dir=str(tmp_path), epoch="test", instance_token=f"instance-{worker}",
            started_at=time.time(), last_claim_epoch=0,
        ))
    # The provenance edge the ``per-task-review`` pipeline rule writes.
    await db.add_dependency("reviewer-agent-job", "reviewed", "discovered-from")

    await db.create_agent(Agent(
        id="final-reviewer-agent", name="final-reviewer-agent", profile_id="final-reviewer",
    ))
    await db.create_task(Task(
        id="final-reviewer-agent-job", project_id="p", title="final review",
        description="Final review for branch feature/reviewed.", status=TaskStatus.IN_PROGRESS,
        profile_id="final-reviewer", route_source="role", assigned_agent_id="final-reviewer-agent",
        branch_name="feature/reviewed", pr_url=PR_URL,
    ))
    await db.update_agent(
        "final-reviewer-agent", state=AgentState.BUSY,
        current_task_id="final-reviewer-agent-job",
    )
    await db.create_session(SessionRecord(
        id="s-final-reviewer-agent", task_id="final-reviewer-agent-job", project_id="p",
        agent_id="final-reviewer-agent", profile_id="final-reviewer", harness="codex",
        provider="fake", name="s-final-reviewer-agent", lifecycle="task", state="running",
        work_dir=str(tmp_path), epoch="test", instance_token="instance-final-reviewer-agent",
        started_at=time.time(), last_claim_epoch=0,
    ))
    # A final review's branch-wide authority is its blocks -> review -> worker graph.
    await db.add_dependency("final-reviewer-agent-job", "reviewer-agent-job", "blocks")

    store = SessionTokenStore(db)
    tokens = {
        worker: await store.mint(
            session_id=f"s-{worker}", task_id=f"{worker}-job", project_id="p",
        )
        for worker in ("reviewer-agent", "worker-agent", "final-reviewer-agent")
    }
    merge_pr = AsyncMock(return_value={"success": True, "sha": "merged"})
    monkeypatch.setattr(
        orch.git, "bind_github_repository",
        AsyncMock(return_value=REPOSITORY),
    )
    monkeypatch.setattr(orch.git, "acheck_pr_merged", AsyncMock(return_value=False))
    monkeypatch.setattr(orch.git, "apr_check_rollup", AsyncMock(return_value=[]))
    monkeypatch.setattr(orch.git, "apr_behind_base", AsyncMock(return_value=None))
    monkeypatch.setattr(orch.git, "apr_base_ref", AsyncMock(return_value="main"))
    monkeypatch.setattr(orch.git, "amerge_pr", merge_pr)
    # ``pr_merge`` pins the merge to the identity GitHub will merge and fails
    # closed when it cannot resolve one; there is no real PR behind this URL.
    monkeypatch.setattr(
        orch.git, "avalidate_pr_for_merge", AsyncMock(return_value=PR_IDENTITY),
    )
    monkeypatch.setattr(deps, "_command_handler", handler)
    monkeypatch.setattr(deps, "_orchestrator", orch)
    monkeypatch.setattr(deps, "_token_store", store)
    monkeypatch.setattr(deps, "_require_session_token", True)
    app, paths = api_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        async def post(command, args=None, *, worker="reviewer-agent"):
            headers = {"Authorization": f"Bearer {tokens[worker]}"}
            if request.param == "execute":
                return await client.post(
                    "/api/execute", headers=headers,
                    json={"command": command, "args": args or {}},
                )
            return await client.post(paths[command], headers=headers, json=args or {})

        def result(response):
            assert response.status_code == 200, response.text
            payload = response.json()
            if request.param == "execute":
                assert payload["ok"], payload
                return payload["result"]
            return payload

        yield SimpleNamespace(
            db=db, store=store, tokens=tokens, post=post, result=result, merge_pr=merge_pr,
        )


# --- the grant ------------------------------------------------------------


async def test_reviewer_rejects_the_task_it_is_reviewing(api):
    result = api.result(await api.post("reopen_with_feedback", {
        "task_id": "reviewed", "feedback": "Rename the flag and add a regression test.",
    }))
    assert result["reopened"] == "reviewed"
    reviewed = await api.db.get_task("reviewed")
    assert reviewed.status == TaskStatus.READY
    assert "Rename the flag" in reviewed.description


async def test_rejecting_does_not_cancel_the_rejecting_review_itself(api):
    """The reject path ends in ``task_close`` on the review task, so the
    reopen cascade must not fail the review that produced the verdict."""
    api.result(await api.post("reopen_with_feedback", {
        "task_id": "reviewed", "feedback": "Needs work.",
    }))
    assert (await api.db.get_task("reviewer-agent-job")).status == TaskStatus.IN_PROGRESS
    api.result(await api.post("task_close", {
        "task_id": "reviewer-agent-job", "outcome": "pass",
        "summary": "rejected — reopened reviewed with feedback",
    }))


@pytest.mark.parametrize("command", ["get_task", "task_show"])
async def test_reviewer_reads_the_reviewed_task(api, command):
    result = api.result(await api.post(command, {"task_id": "reviewed"}))
    assert result["id"] == "reviewed"
    assert result["project_id"] == "p"


async def test_reviewer_cannot_annotate_the_reviewed_task(api):
    response = await api.post("task_comment", {
        "task_id": "reviewed", "body": "Reviewed the diff; flag name is misleading.",
    })
    assert response.status_code == 403, response.text


async def test_reviewer_reads_the_reviewed_task_comments(api):
    listed = api.result(await api.post("task_comments", {"task_id": "reviewed"}))
    assert listed["comments"] == []


async def test_reviewer_keeps_ordinary_access_to_its_own_review_task(api):
    result = api.result(await api.post("task_show", {"task_id": "reviewer-agent-job"}))
    assert result["id"] == "reviewer-agent-job"
    api.result(await api.post("task_comment", {
        "task_id": "reviewer-agent-job", "body": "Starting the review.",
    }))


async def test_final_reviewer_can_reopen_workers_from_its_review_branch(api):
    result = api.result(await api.post("reopen_with_feedback", {
        "task_id": "reviewed", "feedback": "CI is red.",
    }, worker="final-reviewer-agent"))
    assert result["reopened"] == "reviewed"


async def test_final_reviewer_can_read_workers_from_its_review_branch(api):
    result = api.result(await api.post(
        "task_show", {"task_id": "reviewed"}, worker="final-reviewer-agent",
    ))
    assert result["id"] == "reviewed"


@pytest.mark.parametrize("command", sorted(_FINAL_REVIEWER_COMMANDS & _TRIAGE_COMMANDS))
async def test_final_reviewer_keeps_its_carve_out_for_commands_triage_also_claims(api, command):
    """The shared read commands must not cost the final reviewer its grant.

    The mirror of the triage-side regression: making the final-reviewer block
    fall through when the session is not a final reviewer must not weaken the
    branch-bound grant for a session that *is* one.
    """
    result = api.result(await api.post(
        command, {"task_id": "reviewed"}, worker="final-reviewer-agent",
    ))
    assert result["id"] == "reviewed"


async def test_final_reviewer_can_merge_its_review_branch_pr(api):
    response = await api.post("pr_merge", {
        "project_id": "p", "pr_url": PR_URL, "method": "squash",
    }, worker="final-reviewer-agent")
    assert response.status_code == 200, response.text
    payload = response.json()
    result = payload.get("result", payload)
    assert result["sha"] == "merged"
    api.merge_pr.assert_awaited_once()
    assert api.merge_pr.await_args.args[1] == PR_URL
    # The merge is pinned to the validated identity, not just the URL.
    assert api.merge_pr.await_args.kwargs == {
        "method": "squash",
        "expected_head_oid": PR_IDENTITY.head_oid,
        "expected_base_ref": PR_IDENTITY.base_ref,
        "repository": REPOSITORY,
    }


async def test_final_reviewer_can_call_git_diff(api):
    response = await api.post("git_diff", {"project_id": "p"}, worker="final-reviewer-agent")
    assert response.status_code != 403, response.text


# --- the refusals ---------------------------------------------------------


@pytest.mark.parametrize("command,args", [
    ("reopen_with_feedback", {"task_id": "unrelated", "feedback": "not my branch"}),
    ("task_show", {"task_id": "unrelated"}),
    ("pr_merge", {"project_id": "p", "pr_url": "https://github.com/o/r/pull/2"}),
])
async def test_final_reviewer_cannot_reach_another_branch(api, command, args):
    response = await api.post(command, args, worker="final-reviewer-agent")
    assert response.status_code == 403, response.text


@pytest.mark.parametrize("command,args", [
    ("list_tasks", {}),
    ("delete_task", {"task_id": "reviewed"}),
    ("restart_task", {"task_id": "reviewed"}),
])
async def test_final_reviewer_does_not_gain_operator_commands(api, command, args):
    response = await api.post(command, args, worker="final-reviewer-agent")
    assert response.status_code == 403, response.text


@pytest.mark.parametrize("command,args", [
    ("reopen_with_feedback", {"task_id": "unrelated", "feedback": "not mine"}),
    ("reopen_with_feedback", {"task_id": "foreign", "feedback": "not mine"}),
    ("task_show", {"task_id": "unrelated"}),
    ("get_task", {"task_id": "unrelated"}),
    ("task_comment", {"task_id": "unrelated", "body": "not mine"}),
    ("task_comments", {"task_id": "unrelated"}),
])
async def test_reviewer_cannot_reach_a_task_it_is_not_reviewing(api, command, args):
    response = await api.post(command, args)
    assert response.status_code == 403, response.text
    assert (await api.db.get_task("unrelated")).status == TaskStatus.COMPLETED
    assert (await api.db.get_task("foreign")).status == TaskStatus.COMPLETED


@pytest.mark.parametrize("target", ["reviewed", "unrelated"])
async def test_plain_worker_cannot_reopen_anything(api, target):
    response = await api.post(
        "reopen_with_feedback", {"task_id": target, "feedback": "let me through"},
        worker="worker-agent",
    )
    assert response.status_code == 403, response.text
    assert response.text.count("out of scope") == 1
    assert (await api.db.get_task(target)).status == TaskStatus.COMPLETED


async def test_worker_cannot_impersonate_a_reviewer_through_request_fields(api):
    response = await api.post("reopen_with_feedback", {
        "task_id": "reviewed", "feedback": "let me through",
        "session_id": "s-reviewer-agent",
        "_scope": {"kind": "local", "elevated": True, "session_id": "s-reviewer-agent"},
    }, worker="worker-agent")
    assert response.status_code == 403, response.text
    assert (await api.db.get_task("reviewed")).status == TaskStatus.COMPLETED


@pytest.mark.parametrize(
    "change",
    ["stopped", "sleeping", "wrong-session-profile", "wrong-task-profile",
     "wrong-agent", "closed-review", "idle-agent", "stale-epoch", "no-edge"],
)
async def test_stale_or_changed_assignment_loses_reviewer_capabilities(api, change):
    if change in {"stopped", "sleeping"}:
        await api.db.update_session("s-reviewer-agent", state=change)
    elif change == "wrong-session-profile":
        await api.db.update_session("s-reviewer-agent", profile_id="coder")
    elif change == "wrong-task-profile":
        await api.db.update_task("reviewer-agent-job", profile_id="coder", route_source="legacy")
    elif change == "wrong-agent":
        await api.db.update_task("reviewer-agent-job", assigned_agent_id="worker-agent")
    elif change == "closed-review":
        await api.db.update_task("reviewer-agent-job", status=TaskStatus.COMPLETED)
    elif change == "idle-agent":
        await api.db.update_agent(
            "reviewer-agent", state=AgentState.IDLE, current_task_id=None,
        )
    elif change == "stale-epoch":
        await api.db.update_task("reviewer-agent-job", claim_epoch=7)
    else:
        await api.db.remove_dependency("reviewer-agent-job", "reviewed")
    response = await api.post(
        "reopen_with_feedback", {"task_id": "reviewed", "feedback": "stale"},
    )
    assert response.status_code == 403, response.text
    assert (await api.db.get_task("reviewed")).status == TaskStatus.COMPLETED


async def test_rewriting_its_own_description_cannot_widen_a_reviewer_s_reach(api):
    """``task_set`` is a reviewer's own-task capability, so the description is
    agent-controlled: it may only narrow the authoritative provenance edges."""
    api.result(await api.post("task_set", {
        "task_id": "reviewer-agent-job",
        "description": "Reviewing task: unrelated\nBranch: feature/unrelated",
    }))
    response = await api.post(
        "reopen_with_feedback", {"task_id": "unrelated", "feedback": "widened"},
    )
    assert response.status_code == 403, response.text
    assert (await api.db.get_task("unrelated")).status == TaskStatus.COMPLETED
    # The edge still points at the real reviewed task, which stays reachable.
    api.result(await api.post("task_show", {"task_id": "reviewed"}))


async def test_ambiguous_review_provenance_fails_closed_even_after_description_rewrite(api):
    await api.db.add_dependency("reviewer-agent-job", "unrelated", "discovered-from")
    api.result(await api.post("task_set", {
        "task_id": "reviewer-agent-job",
        "description": "Reviewing task: unrelated\nBranch: feature/unrelated",
    }))
    for task_id in ("reviewed", "unrelated"):
        response = await api.post("task_show", {"task_id": task_id})
        assert response.status_code == 403, response.text


async def test_reviewer_does_not_gain_operator_commands(api):
    for command, args in (
        ("list_tasks", {}),
        ("delete_task", {"task_id": "unrelated"}),
        ("restart_task", {"task_id": "unrelated"}),
        ("pr_merge", {"project_id": "p", "pr_url": PR_URL}),
    ):
        response = await api.post(command, args)
        assert response.status_code == 403, (command, response.text)
    assert await api.db.get_task("unrelated") is not None


async def test_typed_and_execute_routes_refuse_identically(api):
    cases = [
        ("reopen_with_feedback", {"task_id": "unrelated", "feedback": "no"},
         "out of scope: a reviewer may only act on the task it is reviewing"),
        ("task_show", {"task_id": "unrelated"},
         "out of scope: a reviewer may only act on the task it is reviewing"),
        ("task_close", {"task_id": "reviewed", "outcome": "pass"},
         "out of scope: task_id mismatch"),
    ]
    for command, args, expected in cases:
        response = await api.post(command, args)
        assert response.status_code == 403, response.text
        payload = response.json()
        assert payload.get("error") == expected or payload.get("detail") == expected, payload


@pytest.fixture(params=["execute", "typed"])
async def fidelity_api(command_handler_factory, monkeypatch, request, api_app):
    from tests.test_fidelity_review import _held_batch, _ensure, _reviewer_session

    handler = await command_handler_factory()
    handler.config.security.capability_enforcement = "enforce"
    batch, decision = await _held_batch(handler)
    reviewer_id = (await _ensure(handler, batch))["task_id"]
    scope = await _reviewer_session(handler, reviewer_id, "s-fidelity-http", epoch=3)
    store = SessionTokenStore(handler.db)
    token = await store.mint(session_id=scope.session_id, task_id=None, project_id="p1",
                             session_instance_token=scope.session_instance_token)
    monkeypatch.setattr(deps, "_command_handler", handler)
    monkeypatch.setattr(deps, "_orchestrator", handler.orchestrator)
    monkeypatch.setattr(deps, "_token_store", store)
    monkeypatch.setattr(deps, "_require_session_token", True)
    app, paths = api_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        async def post(command, args=None):
            if command == "approved_spec_packet":
                args = {"project_id": "p1", **(args or {})}
            headers = {"Authorization": f"Bearer {token}"}
            if request.param == "execute":
                response = await client.post("/api/execute", headers=headers,
                    json={"command": command, "args": args or {}})
                payload = response.json()
                return response.status_code, payload.get("result", payload)
            response = await client.post(paths[command], headers=headers, json=args or {})
            return response.status_code, response.json()

        yield SimpleNamespace(handler=handler, db=handler.db, batch=batch, decision=decision,
                              scope=scope, post=post, reviewer_id=reviewer_id)


async def test_fidelity_http_reads_exact_batch_and_immutable_packet(fidelity_api):
    api = fidelity_api
    for mode in ("flat", "tree", "compact"):
        status, result = await api.post("list_tasks", {"display_mode": mode,
                                                      "show_dependencies": True})
        assert status == 200, result
        assert result["display_mode"] == mode
        if mode == "flat":
            assert {t["id"] for t in result["tasks"]} == set(api.batch["task_ids"])
        else:
            assert len(result["tasks"]) == len(api.batch["task_ids"])
        assert api.reviewer_id not in str(result)
    for task_id in api.batch["task_ids"]:
        status, result = await api.post("task_show", {"task_id": task_id})
        assert status == 200 and result["id"] == task_id, result
        assert result["description"] and result["batch_member"]["section_anchor"]
        assert "primary_outcome" in result["batch_member"]
    status, result = await api.post("get_downstream_tasks", {
        "task_id": api.batch["members"]["api"],
    })
    assert status == 200 and result["success"], result
    assert api.batch["members"]["final"] in {task["id"] for task in result["tasks"]}
    status, packet = await api.post("approved_spec_packet", {
        "decision_id": api.decision["decision_id"],
        "packet_sha256": api.decision["packet_sha256"],
    })
    assert status == 200 and packet["success"], packet
    assert "# Approved spec" in packet["body"] and packet["design"] is None
    # Own control-task reads retain ordinary scope.
    status, result = await api.post("task_show", {"task_id": api.reviewer_id})
    assert status == 200 and result["id"] == api.reviewer_id, result


async def test_fidelity_http_never_expands_unrelated_members_or_transitive_results(fidelity_api):
    api = fidelity_api
    await api.db.create_project(Project(id="foreign-project", name="Foreign"))
    for tid, pid in (("private-same-project", "p1"), ("private-foreign", "foreign-project")):
        await api.db.create_task(Task(id=tid, project_id=pid, title=tid,
                                     description="Private queue work"))
        # Both direct details and transitive downstream expansion used to be
        # capable of exposing rows beyond an authorized seed/project.
        await api.db.add_dependency(tid, api.batch["task_ids"][1], "blocks")
        for command in ("task_show", "get_downstream_tasks"):
            status, result = await api.post(command, {"task_id": tid})
            assert status == 403, result
    await api.db.add_dependency("private-foreign", "private-same-project", "blocks")
    # Ordinary task_show expands both these relationships. Neither may
    # disclose ambient queue rows through an authorized member read.
    await api.db.update_task(api.batch["task_ids"][1], parent_task_id="private-same-project")
    await api.db.update_task("private-foreign", parent_task_id=api.batch["task_ids"][1])
    for command, args in (
        ("list_tasks", {"show_dependencies": True}),
        ("list_tasks", {"display_mode": "tree"}),
        ("task_show", {"task_id": api.batch["task_ids"][1]}),
        ("get_downstream_tasks", {"task_id": api.batch["task_ids"][1]}),
    ):
        status, result = await api.post(command, args)
        assert status == 200, result
        assert "private-same-project" not in str(result)
        assert "private-foreign" not in str(result)
    for command in ("list_tasks", "approved_spec_packet"):
        args = {"project_id": "foreign-project"}
        if command == "approved_spec_packet":
            args.update(decision_id=api.decision["decision_id"],
                        packet_sha256=api.decision["packet_sha256"])
        status, result = await api.post(command, args)
        assert status == 403, result


@pytest.mark.parametrize("change", ["epoch", "instance", "session", "reassigned", "released",
                                     "metadata", "dedup", "project"])
async def test_fidelity_http_read_authority_revoked(fidelity_api, change):
    from sqlalchemy import update
    from src.database.tables import ingest_batches

    api = fidelity_api
    status, result = await api.post("list_tasks")
    assert status == 200, result
    if change == "epoch":
        await api.db.update_task(api.reviewer_id, claim_epoch=4)
    elif change == "instance":
        await api.db.update_session(api.scope.session_id, instance_token="new-instance")
    elif change == "session":
        await api.db.update_session(api.scope.session_id, state="stopped")
    elif change == "metadata":
        await api.db.set_task_meta(api.reviewer_id, "fidelity_review", {"batch_id": "spoofed"})
    elif change == "dedup":
        await api.db.update_task(api.reviewer_id, dedup_key="fidelity-review:batch:spoofed")
    else:
        values = {"reassigned": {"reviewer_task_id": None},
                  "released": {"state": "released"},
                  "project": {"project_id": "foreign-project"}}[change]
        if change == "project":
            await api.db.create_project(Project(id="foreign-project", name="Foreign"))
        async with api.db.immediate() as conn:
            await conn.execute(update(ingest_batches).where(
                ingest_batches.c.batch_id == api.batch["batch_id"],
            ).values(**values))
    for command, args in (
        ("list_tasks", {}),
        ("task_show", {"task_id": api.batch["task_ids"][0]}),
        ("get_downstream_tasks", {"task_id": api.batch["task_ids"][1]}),
        ("approved_spec_packet", {"decision_id": api.decision["decision_id"],
                                  "packet_sha256": api.decision["packet_sha256"]}),
    ):
        status, result = await api.post(command, args)
        assert status == 403, result


async def test_fidelity_http_cannot_choose_packet_or_mutation_authority(fidelity_api):
    api = fidelity_api
    for override in ({"decision_id": "unrelated-approved-decision"},
                     {"packet_sha256": "0" * 64}):
        status, result = await api.post("approved_spec_packet", {
            "decision_id": api.decision["decision_id"],
            "packet_sha256": api.decision["packet_sha256"], **override,
        })
        assert status in {200, 422} and not result.get("success", False), result
    status, result = await api.post("task_batch_commit", {"proposal_id": api.batch["batch_id"]})
    assert status == 403, result
    # Scope's read carve-out must not bypass the profile's capability policy.
    from src.models import AgentProfile
    await api.db.upsert_profile(AgentProfile(id="fidelity-reviewer", name="Fidelity",
        harness="codex", needs_workspace=False, aq_commands=["task_show"]))
    api.handler._principal_cache.clear()
    status, result = await api.post("list_tasks")
    assert status in {200, 403} and not result.get("success", False), result
    assert "capability" in str(result), result


async def test_fidelity_http_packet_pins_source_and_approved_design_lineage(fidelity_api):
    from sqlalchemy import update
    from src.database.tables import ingest_batches
    from tests.test_review_feedback import prepared, accept

    api = fidelity_api
    # Create a real second approved document, then make it the batch's
    # server-owned design lineage. It is readable only as that lineage.
    design = await api.handler.execute("review_submit", {
        "project_id": "p1", "kind": "spec", "title": "Approved design",
        "content": "---\nspec_kind: design\n---\n# Design\nPinned design body.\n",
    })
    assert design["success"], design
    args, preview = await prepared(api.handler, design["review_id"])
    approved = await accept(api.handler, args, preview)
    assert approved["success"], approved
    revision = await api.db.get_review_revision(design["review_id"], 1)
    async with api.db.immediate() as conn:
        await conn.execute(update(ingest_batches).where(
            ingest_batches.c.batch_id == api.batch["batch_id"],
        ).values(lineage_kind="resolved", design_review_id=design["review_id"],
                 design_revision=1, design_content_sha256=revision["content_sha256"]))
    packet_args = {"decision_id": api.decision["decision_id"],
                   "packet_sha256": api.decision["packet_sha256"]}
    status, packet = await api.post("approved_spec_packet", packet_args)
    assert status == 200 and packet["success"], packet
    assert packet["design"] == {"review_id": design["review_id"], "revision": 1,
        "content_sha256": revision["content_sha256"], "body": revision["content"]}
    # Even another actually approved packet is not client-selectable.
    status, refused = await api.post("approved_spec_packet", {
        "decision_id": approved["decision_id"], "packet_sha256": preview["packet_sha256"],
    })
    assert status in {200, 422} and not refused.get("success", False), refused
    for changed in ({"source_content_sha256": "0" * 64},
                    {"design_content_sha256": "0" * 64}):
        async with api.db.immediate() as conn:
            await conn.execute(update(ingest_batches).where(
                ingest_batches.c.batch_id == api.batch["batch_id"],
            ).values(**{"source_content_sha256": packet["content_sha256"], **changed}))
        status, refused = await api.post("approved_spec_packet", packet_args)
        assert status in {200, 422} and not refused.get("success", False), refused
