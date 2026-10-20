"""Durable incident delivery and bounded, claim-fenced supervisor decisions."""

import asyncio
import json
import subprocess
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, insert, select, update

from src.api.auth import RequestScope
from src.commands.handler import CommandHandler
from src.config import AppConfig, DatabaseConfig
from src.database import Database
from src.database.tables import (
    integration_branch_owners,
    integration_batches,
    integration_owner_recoveries,
    integration_repair_operations,
    integration_repair_stages,
    messages,
    projects,
    task_context,
    task_branch_origins,
    task_integration_checkpoints,
    tasks,
    workspaces,
)
from src.models import (
    KIND_MODE_WORKTREE, Agent, AgentProfile, AgentState, Project, RepoConfig, RepoSourceType, SessionRecord,
    Task, TaskStatus, Workspace, WorkspaceKind,
)
from src.orchestrator import Orchestrator
from tests.pg_dsn import ensure_worker_postgres_dsn
from tests.db_fixtures import lease_dsn

pytestmark = pytest.mark.asyncio
POSTGRES_TEST_DSN = ensure_worker_postgres_dsn()


@pytest.fixture
async def env(tmp_path, request):
    db = Database(lease_dsn("recovery.db"))
    await db.initialize()
    await db.create_project(Project(id="p", name="Project"))
    await db.create_project(Project(id="other", name="Other"))
    await db.create_profile(AgentProfile(id="worker", name="Worker"))
    await db.create_task(
        Task(
            id="t",
            project_id="p",
            title="Work",
            status=TaskStatus.BLOCKED,
            profile_id="worker", route_source="legacy",
            intelligence_class="deep-high",
            branch_name="aq/keep",
            description="Keep requirements",
            created_at=100,
            updated_at=100,
        )
    )
    await db.update_task("t", created_at=100)
    config = AppConfig(database=DatabaseConfig(url=lease_dsn("recovery.db")), data_dir=str(tmp_path / "data"), workspace_dir=str(tmp_path / "ws"))
    orch = Orchestrator(config)
    orch.db = db
    orch.session_providers = SimpleNamespace(
        create=lambda *_: SimpleNamespace(
            stop=AsyncMock(), confirm_stopped=AsyncMock(return_value=True),
        )
    )
    yield SimpleNamespace(db=db, orch=orch, config=config, handler=CommandHandler(orch, config))
    await db.close()


async def incident(env, sid="s", started=200, reason="stuck_timeout"):
    await stopped_attempt(env, sid=sid, started=started, reason=reason)
    await env.db.queue_task_recovery_notifications()
    return await env.db.get_task_meta("t", "supervisor_recovery_incident")


async def stopped_attempt(env, sid="s", started=200, reason="stuck_timeout", attention=True,
                          agent_id=None, lifecycle="task"):
    await env.db.create_session(
        SessionRecord(
            id=sid,
            task_id="t",
            project_id="p",
            profile_id="worker",
            harness="fake",
            provider="fake",
            name=sid,
            lifecycle=lifecycle,
            agent_id=agent_id,
            state="running",
            desired_state="running",
            epoch="test",
            instance_token=sid,
            work_dir="/never-used",
            started_at=started,
            last_activity=started + 7199,
        )
    )
    await env.db.update_session(
        sid, state="stopped", desired_state="stopped", ended_at=started + 7200, end_reason=reason
    )
    if attention:
        await env.db.set_task_meta("t", "needs_attention", reason)


async def notify(env, project_id="p", **extra):
    """The failure-event half: what the blocked-task-escalation playbook calls."""
    return await env.handler.execute(
        "task_recovery_notify", {"task_id": "t", "project_id": project_id, **extra}
    )


async def queued_messages(env):
    async with env.db._engine.connect() as conn:
        return (await conn.execute(select(messages))).mappings().all()


ORPHAN_NOW = 10_000


async def missing_session_task(env, *, heartbeat=100, updated_at=100, **changes):
    await env.db.create_agent(Agent(
        id="idle", name="Idle", profile_id="worker", last_heartbeat=heartbeat,
    ))
    # update_task deliberately refreshes updated_at; reproduce the abandoned
    # assignment with its original task-scoped heartbeat and revision.
    async with env.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "t").values(
            status="IN_PROGRESS", assigned_agent_id="idle", updated_at=updated_at,
            **changes,
        ))


async def test_missing_session_requeues_once_preserving_task_identity(env):
    await missing_session_task(env)
    before = await env.db.get_task("t")
    result = await env.handler.recover_missing_session_task("t", now=ORPHAN_NOW)
    assert result == {"success": True, "outcome": "recovered", "status": "READY"}
    after = await env.db.get_task("t")
    assert after.assigned_agent_id is None
    assert after.retry_count == before.retry_count + 1
    for field in ("branch_name", "profile_id", "intelligence_class", "description", "project_id"):
        assert getattr(after, field) == getattr(before, field)
    again = await env.handler.recover_missing_session_task("t", now=ORPHAN_NOW)
    assert again["outcome"] == "not_orphaned"
    comments = (await env.db.list_task_comments("t"))["comments"]
    assert len(comments) == 1
    assert "Recovered orphaned" in comments[0]["body"]


@pytest.mark.parametrize("guard", [
    "heartbeat", "recent_assignment", "launch", "control_lock", "adapter", "live_agent",
    "live_task", "manual_pause", "attention", "held_incident", "timer", "container",
    "daemon_task", "paused_project", "hold_label",
])
async def test_missing_session_does_not_recover_active_or_held_work(env, guard):
    await missing_session_task(
        env, heartbeat=ORPHAN_NOW if guard == "heartbeat" else 100,
        updated_at=ORPHAN_NOW if guard == "recent_assignment" else 100,
        **({"resume_after": ORPHAN_NOW + 60} if guard == "timer" else {}),
        **({"task_type": "promotion"} if guard == "daemon_task" else {}),
    )
    if guard == "launch":
        env.orch._running_tasks["t"] = object()
    elif guard == "adapter":
        env.orch._adapters["idle"] = object()
    elif guard in ("live_agent", "live_task"):
        await env.db.create_session(SessionRecord(
            id="live", name="live", provider="fake", harness="fake", profile_id="worker",
            project_id="p", agent_id="idle" if guard == "live_agent" else None,
            task_id="t" if guard == "live_task" else None, lifecycle="pool",
            state="running", desired_state="running", epoch="test", instance_token="live",
            work_dir="/unused", started_at=200,
        ))
    elif guard == "manual_pause":
        await env.db.set_task_meta("t", "manual_pause", {"reason": "operator"})
    elif guard == "attention":
        await env.db.set_task_meta("t", "needs_attention", "operator_review")
    elif guard == "held_incident":
        await env.db.set_task_meta("t", "supervisor_recovery_incident", {
            "id": "existing", "decision": "hold",
        })
    elif guard == "container":
        await env.db.create_task(Task(id="child", project_id="p", title="Child", description=""))
        await env.db.add_dependency("child", "t", dep_type="parent-child")
    elif guard == "hold_label":
        await env.db.add_task_label("t", "hold:operator")
    elif guard == "paused_project":
        async with env.db._engine.begin() as conn:
            await conn.execute(update(projects).where(projects.c.id == "p").values(status="PAUSED"))
    if guard != "recent_assignment":
        # Hierarchy/label edits can refresh the revision. Exercise the guard
        # itself, rather than accidentally passing on the launch grace alone.
        async with env.db._engine.begin() as conn:
            await conn.execute(update(tasks).where(tasks.c.id == "t").values(updated_at=100))
    if guard == "control_lock":
        async with env.orch._task_control_lock("t"):
            result = await env.handler.recover_missing_session_task("t", now=ORPHAN_NOW)
    else:
        result = await env.handler.recover_missing_session_task("t", now=ORPHAN_NOW)
    assert result["outcome"] != "recovered"
    task = await env.db.get_task("t")
    assert task.status is TaskStatus.IN_PROGRESS
    assert task.retry_count == 0


@pytest.mark.parametrize("race", ["heartbeat", "epoch", "session"])
async def test_missing_session_final_recheck_fences_new_execution(env, race):
    await missing_session_task(env)
    observed = await env.db.missing_session_recovery_candidate("t", now=ORPHAN_NOW, grace=480)
    assert observed is not None
    if race == "heartbeat":
        await env.db.update_agent("idle", last_heartbeat=ORPHAN_NOW)
    elif race == "epoch":
        async with env.db._engine.begin() as conn:
            await conn.execute(update(tasks).where(tasks.c.id == "t").values(claim_epoch=2))
    else:
        await env.db.create_session(SessionRecord(
            id="new", name="new", provider="fake", harness="fake", profile_id="worker",
            project_id="p", agent_id="idle", task_id="t", lifecycle="task", state="starting",
            desired_state="running", epoch="test", instance_token="new", work_dir="/unused",
            started_at=ORPHAN_NOW,
        ))
    assert await env.db.recover_missing_session_task(observed, now=ORPHAN_NOW, grace=480) is None
    assert (await env.db.get_task("t")).status is TaskStatus.IN_PROGRESS


async def test_missing_session_exhaustion_reuses_one_supervisor_incident(env):
    await missing_session_task(env, retry_count=2, max_retries=2)
    result = await env.handler.recover_missing_session_task("t", now=ORPHAN_NOW)
    assert result["status"] == "FAILED"
    assert (await env.db.get_task("t")).retry_count == 2
    await env.db.queue_task_recovery_notifications()
    await env.db.queue_task_recovery_notifications()
    assert len(await queued_messages(env)) == 1
    current = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    assert current["reason"] == "session_missing"


@pytest.mark.parametrize("stopped", [True, False, None])
async def test_missing_session_checks_exact_previous_writer(env, stopped):
    await stopped_attempt(env, attention=False)
    await env.db.update_session("s", task_id=None)
    await missing_session_task(env)
    probe = AsyncMock(return_value=stopped)
    env.orch.session_providers = SimpleNamespace(
        create=lambda *_: SimpleNamespace(confirm_stopped=probe),
    )
    result = await env.handler.recover_missing_session_task("t", now=ORPHAN_NOW)
    assert probe.await_count == 1
    assert probe.call_args.args[0].instance_token == "s"
    assert (result["outcome"] == "recovered") is (stopped is True)


@pytest.mark.parametrize("guard", [None, "changed_branch", "missing_checkout", "pinned"])
async def test_missing_session_keeps_committed_staged_and_unstaged_work(env, tmp_path, guard):
    from src.git.manager import GitError
    from src.orchestrator.task_checkpoint import restore_checkpoint

    await missing_session_task(env)
    await env.db.update_agent("idle", state=AgentState.BUSY, current_task_id="t")
    path = tmp_path / "retained"
    _handoff_git(tmp_path, "init", "--initial-branch=aq/keep", str(path))
    (path / "committed").write_text("committed progress\n")
    _handoff_git(path, "add", ".")
    _handoff_git(path, "commit", "-m", "task progress")
    head = _handoff_git(path, "rev-parse", "HEAD")
    (path / "draft").write_text("staged progress\n")
    _handoff_git(path, "add", "draft")
    (path / "draft").write_text("unstaged progress\n")
    (path / "untracked").write_text("untracked progress\n")
    await env.db.create_workspace(Workspace(
        id="retained", project_id="p", workspace_path=str(path), source_type=RepoSourceType.CLONE,
        locked_by_task_id="t", locked_by_agent_id="idle", locked_at=100,
    ))
    if guard == "changed_branch":
        _handoff_git(path, "switch", "-c", "aq/other")
    elif guard == "pinned":
        async with env.db._engine.begin() as conn:
            await conn.execute(update(workspaces).where(workspaces.c.id == "retained")
                               .values(job_pin_count=1))
    elif guard == "missing_checkout":
        path.rename(tmp_path / "moved")
        with pytest.raises(GitError):
            await env.handler.recover_missing_session_task("t", now=ORPHAN_NOW)
    if guard != "missing_checkout":
        result = await env.handler.recover_missing_session_task("t", now=ORPHAN_NOW)
    if guard:
        if guard == "changed_branch":
            assert result["outcome"] == "workspace_branch_changed"
        assert (await env.db.get_task("t")).status is TaskStatus.IN_PROGRESS
        assert (await env.db.get_workspace("retained")).locked_by_task_id == "t"
        return
    assert result["status"] == "READY"
    assert (await env.db.get_workspace("retained")).locked_by_task_id is None
    assert (await env.db.get_agent("idle")).state is AgentState.IDLE
    assert (await env.db.get_agent("idle")).current_task_id is None
    assert (await env.db.get_task("t")).preferred_workspace_id == "retained"
    assert _handoff_git(path, "rev-parse", "HEAD") == head
    # Simulate slot reuse; normal preparation can restore all three forms of
    # local progress without resetting the task's branch to its parent's base.
    _handoff_git(path, "reset", "--hard")
    _handoff_git(path, "clean", "-fd")
    assert await restore_checkpoint(env.db, env.orch.git, "t", str(path)) == "aq/keep"
    assert _handoff_git(path, "rev-parse", "HEAD") == head
    assert _handoff_git(path, "show", ":draft") == "staged progress"
    assert (path / "draft").read_text() == "unstaged progress\n"
    assert (path / "untracked").read_text() == "untracked progress\n"


async def decide(env, current, decision="retry", **extra):
    return await env.handler.execute(
        "task_recover",
        {
            "task_id": "t",
            "incident_id": current["id"],
            "decision": decision,
            "reason": "Transcript shows progress; preserve work and continue.",
            **extra,
        },
    )


async def apply_human_recovery_reply(env, current, decision):
    created = await env.handler.execute(
        "escalation_create",
        {
            "project_id": "p",
            "task_id": "t",
            "source_kind": "task_recovery",
            "source_identity": current["id"],
            "incident_key": "task-recovery:" + current["id"],
            "summary": "Worker attempt is blocked",
            "investigation": "Supervisor inspected the stopped attempt and recovery guards",
            "decision_requested": "Retry the task or keep it blocked?",
            "choices": ["retry", "hold"],
            "severity": "high",
        },
    )
    replay = await env.handler.execute(
        "escalation_create",
        {
            "project_id": "p",
            "task_id": "t",
            "source_kind": "task_recovery",
            "source_identity": current["id"],
            "incident_key": "task-recovery:" + current["id"],
            "summary": "Worker attempt is blocked",
            "investigation": "Supervisor inspected the stopped attempt and recovery guards",
            "decision_requested": "Retry the task or keep it blocked?",
            "choices": ["retry", "hold"],
            "severity": "high",
        },
    )
    assert created["created"] is True
    assert replay["created"] is False
    escalation = created["escalation"]
    accepted = await env.handler.execute(
        "escalation_reply",
        {
            "escalation_id": escalation["id"],
            "text": decision,
            "external_message_id": "human-recovery-" + decision,
        },
    )
    await env.db.create_session(
        SessionRecord(
            id="supervisor-" + decision,
            project_id="p",
            profile_id="supervisor",
            harness="fake",
            provider="fake",
            name="n-supervisor--p",
            lifecycle="named",
            state="running",
            desired_state="running",
            epoch="test",
            instance_token="supervisor-token-" + decision,
            work_dir="/never-used",
            started_at=300,
        )
    )
    args = {
            "escalation_id": escalation["id"],
            "reply_id": accepted["reply"]["id"],
            "expected_revision": accepted["escalation"]["revision"],
            "idempotency_key": "apply-human-recovery-" + decision,
            "action_kind": "task_recover",
            "target_id": "t",
            "decision": decision,
            "_scope": {
                "kind": "session",
                "session_id": "supervisor-" + decision,
                "session_instance_token": "supervisor-token-" + decision,
                "project_id": "p",
                "elevated": True,
            },
        }
    legacy = await env.handler.execute("escalation_apply_reply", args)
    assert legacy["error_code"] == "ask.context_missing", legacy
    args["expected"] = legacy["expected"]
    preview = await env.handler.execute("escalation_apply_reply", {**args, "prepare": True})
    assert preview["success"], preview
    return await env.handler.execute("escalation_apply_reply", {**args, "preview_id": preview["preview"]["id"]})



async def test_notification_survives_restart_without_duplicates_and_has_diagnostics(env):
    current = await incident(env)
    await asyncio.gather(
        env.db.queue_task_recovery_notifications(), env.db.queue_task_recovery_notifications()
    )
    async with env.db._engine.connect() as conn:
        queued = (await conn.execute(select(messages))).mappings().all()
    assert len(queued) == 1
    assert queued[0]["to_id"] == "supervisor-p"
    assert queued[0]["project_id"] == "p"
    assert queued[0]["from_kind"] == "system"
    assert queued[0]["archive_after_inject"] == 1
    assert current["id"] in queued[0]["body"]
    assert '"idle_seconds": 1' in queued[0]["body"]
    assert "aq task recover" in queued[0]["body"]
    assert "task-recovery:<incident-id>" in queued[0]["body"]
    assert (await env.db.get_task("t")).status == TaskStatus.BLOCKED


async def test_stale_pending_incident_is_superseded_when_task_resumes(env):
    current = await incident(env)
    message_id = "msg-" + current["id"]

    await env.db.transition_task("t", TaskStatus.READY, force=True, context="restart")
    await env.db.queue_task_recovery_notifications()

    stale = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    assert stale["decision"] == "superseded"
    assert (await env.db.get_message(message_id)).archived_at is not None

    await env.db.transition_task("t", TaskStatus.BLOCKED, force=True)
    replacement = await incident(env, sid="replacement", started=9000)
    assert replacement["id"] != current["id"]
    assert (await env.db.get_message("msg-" + replacement["id"])).archived_at is None


@pytest.mark.parametrize("route_source", ["legacy", "router", "role", "override"])
async def test_retry_preserves_work_and_routing_records_comment_and_consumes_budget_once(
    env, route_source,
):
    route = {"constraints": {"exclude_providers": ["gemini"]}, "reason": "previous selection"}
    await env.db.update_task("t", route_source=route_source, route=route,
                             class_hint="standard-high")
    before = await env.db.get_task("t")
    current = await incident(env)
    results = await asyncio.gather(decide(env, current), decide(env, current))
    assert sum("error" not in r for r in results) == 1, results
    task = await env.db.get_task("t")
    assert task.status == TaskStatus.READY
    assert task.retry_count == 1
    assert task.class_hint == before.class_hint == "standard-high"
    if route_source in {"legacy", "router"}:
        assert (task.route_source, task.profile_id, task.intelligence_class) == (
            "unrouted", None, None,
        )
        assert task.provider_intent == "class_only"
        expected_route = {"constraints": route["constraints"]}
        if route_source == "legacy":
            expected_route["legacy"] = {
                "profile_id": before.profile_id,
                "intelligence_class": before.intelligence_class,
                "provider_intent": before.provider_intent,
            }
        assert task.route == expected_route
    else:
        assert (task.route_source, task.profile_id, task.intelligence_class) == (
            route_source, "worker", "deep-high",
        )
        assert task.route == route
    # A stuck-session recovery requests a fresh route without a work verdict.
    assert await env.db.get_task_meta("t", "routing_failed_attempts") is None
    assert task.branch_name == "aq/keep" and task.description == "Keep requirements"
    assert await env.db.get_task_meta("t", "needs_attention") is None
    assert await env.db.get_task_meta("t", "supervisor_recovery_attempts") == 1
    comments = await env.handler.execute("task_comments", {"task_id": "t"})
    assert len(comments["comments"]) == 1
    assert "Transcript shows progress" in comments["comments"][0]["body"]


async def test_cleared_hold_retries_once_with_audit_and_preserved_work(env):
    current = await incident(env)
    assert "error" not in await decide(env, current, "hold")
    held = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    assert "error" in await decide(env, current)
    assert "error" in await decide(env, current, expected_hold_at=held["decided_at"] - 1)
    results = await asyncio.gather(*(
        decide(env, current, expected_hold_at=held["decided_at"])
        for _ in range(2)
    ))
    assert sum("error" not in result for result in results) == 1, results
    task = await env.db.get_task("t")
    assert task.status == TaskStatus.READY and task.retry_count == 1
    assert task.branch_name == "aq/keep" and task.description == "Keep requirements"
    released = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    assert released["decision"] == "retry"
    assert released["retry_allowed"] is False
    assert await env.db.count_ready_by_profile("p") == {task.profile_id: 1}
    assert await env.db.claim_frontier_exclusions("t") == []
    assert released["decision_history"] == [{
        key: held[key]
        for key in ("decision", "decision_reason", "decided_at", "decided_by")
    }]
    comments = await env.handler.execute("task_comments", {"task_id": "t"})
    assert len(comments["comments"]) == 2
    assert "Cleared recovery hold" in comments["comments"][0]["body"]


@pytest.mark.parametrize("reason", ["slot_reset_failed", "session_close_hard_failure"])
async def test_operator_can_retry_reviewed_nonautomatic_hold_once(env, reason):
    current = await incident(env, reason=reason)
    held, _ = await supervisor_handoff_hold(env, current)
    ordinary = await decide(env, current, expected_hold_at=held["decided_at"])
    assert "automatic recovery is not allowed" in ordinary["error"]
    results = await asyncio.gather(*(
        decide(env, current, operator_release=True, expected_hold_at=held["decided_at"])
        for _ in range(2)
    ))
    assert sum("error" not in result for result in results) == 1, results
    task = await env.db.get_task("t")
    assert task.status == TaskStatus.READY and task.retry_count == 1
    assert task.branch_name == "aq/keep" and task.description == "Keep requirements"
    assert await env.db.get_task_meta("t", "supervisor_recovery_attempts") == 1
    recorded = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    assert recorded["operator_release"] is True
    assert recorded["decided_by"] == "human:local-operator"
    assert recorded["decision_history"] == [
        {key: held[key] for key in ("decision", "decision_reason", "decided_at", "decided_by")}
    ]


async def test_operator_retry_can_review_nonautomatic_session_exit(env):
    await stopped_attempt(env, reason="productive_death")
    await env.db.set_task_meta("t", "needs_attention", "session_not_live")
    await env.db.queue_task_recovery_notifications()
    current = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    held, _ = await supervisor_handoff_hold(env, current)
    assert "error" in await decide(env, current, expected_hold_at=held["decided_at"])
    result = await decide(env, current, operator_release=True, expected_hold_at=held["decided_at"])
    assert result["status"] == "READY", result


@pytest.mark.parametrize("guard", ["retry_budget", "recovery_budget", "gate", "routing", "live",
                                   "resource", "pause", "project_budget", "stale_hold"])
async def test_operator_retry_preserves_execution_and_budget_guards(env, guard):
    current = await incident(env, reason="slot_reset_failed")
    held, _ = await supervisor_handoff_hold(env, current)
    if guard == "retry_budget":
        await env.db.update_task("t", retry_count=3)
    elif guard == "recovery_budget":
        await env.db.set_task_meta("t", "supervisor_recovery_attempts", 2)
    elif guard == "gate":
        await env.db.create_gate(project_id="p", gate_type="human", title="Review",
                                 waiter_task_ids=["t"])
    elif guard == "routing":
        await env.db.update_task("t", intelligence_class="standard-high")
    elif guard == "live":
        env.orch.session_providers.create = lambda *_: SimpleNamespace(
            confirm_stopped=AsyncMock(return_value=False)
        )
    elif guard == "resource":
        await env.db.create_agent(Agent(id="holder", name="Holder", profile_id="worker",
                                        state=AgentState.BUSY, current_task_id="t"))
    elif guard == "pause":
        await env.db.set_task_meta("t", "manual_pause", {"reason": "human hold"})
    elif guard == "project_budget":
        await env.db.update_project("p", budget_limit=1, total_tokens_used=1)
    result = await decide(env, current, operator_release=True,
                          expected_hold_at=held["decided_at"] - int(guard == "stale_hold"))
    assert "error" in result, result
    assert (await env.db.get_task("t")).status == TaskStatus.BLOCKED
    assert (await env.db.get_task_meta("t", "supervisor_recovery_incident"))["decision"] == "hold"


@pytest.mark.parametrize("status", [TaskStatus.BLOCKED, TaskStatus.READY, TaskStatus.FAILED])
async def test_release_only_preserves_task_and_other_guards(env, status):
    current = await incident(env, reason="slot_reset_failed")
    held, _ = await supervisor_handoff_hold(env, current)
    if status != TaskStatus.BLOCKED:
        await env.db.update_task("t", status=status)
    await env.db.create_gate(project_id="p", gate_type="human", title="Review",
                             waiter_task_ids=["t"])
    before = await env.db.get_task("t")
    result = await decide(env, current, "release", operator_release=True,
                          expected_hold_at=held["decided_at"])
    assert result["decision"] == "release", result
    assert asdict(await env.db.get_task("t")) == asdict(before)
    assert await env.db.get_task_meta("t", "needs_attention") == "slot_reset_failed"
    assert await env.db.get_task_meta("t", "supervisor_recovery_attempts") is None
    assert "frontier_recovery_hold" not in {
        item["code"] for item in await env.db.claim_frontier_exclusions("t")
    }
    assert "error" in await decide(env, current, "release", operator_release=True,
                                   expected_hold_at=held["decided_at"])


@pytest.mark.parametrize("guard", [None, "provider_live", "live_session", "epoch", "instance",
                                   "branch_owner", "operation", "lost_epoch", "wrong_holder"])
async def test_hold_release_repairs_only_exact_stopped_assignment(env, guard):
    await env.db.create_agent(Agent(id="old", name="Old", profile_id="worker"))
    await stopped_attempt(env, reason="claim_binding_stale", agent_id="old", lifecycle="pool")
    await env.db.update_session("s", last_claim_epoch=0)
    await env.db.queue_task_recovery_notifications()
    current = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    held, _ = await supervisor_handoff_hold(env, current)
    await env.db.transition_task(
        "t", TaskStatus.READY, force=True, context="test", assigned_agent_id="old",
    )
    if guard == "provider_live":
        env.orch.session_providers.create = lambda *_: SimpleNamespace(
            confirm_stopped=AsyncMock(return_value=False)
        )
    elif guard == "live_session":
        await env.db.create_session(SessionRecord(
            id="other-worker", project_id="p", profile_id="worker", harness="fake",
            provider="fake", name="other-worker", lifecycle="pool", state="sleeping",
            desired_state="sleeping", agent_id="old", epoch="test", instance_token="other",
            work_dir="/never-used", started_at=9000,
        ))
    elif guard == "epoch":
        await env.db.update_task("t", claim_epoch=1)
    elif guard == "instance":
        async def replaced_during_probe(_):
            await env.db.update_session("s", instance_token="replacement")
            return True
        env.orch.session_providers.create = lambda *_: SimpleNamespace(
            confirm_stopped=replaced_during_probe
        )
    elif guard == "branch_owner":
        async with env.db.immediate() as conn:
            await conn.execute(insert(integration_branch_owners).values(
                id="owner", repository_id="r", ref="aq/keep", owner_id="t",
                owner_role="worker", handoff_state="handoff_pending", session_id="s",
                fence_token=2, created_at=1, updated_at=8000,
            ))
    elif guard == "operation":
        await bind_train_repair(env)
    elif guard in {"lost_epoch", "wrong_holder"}:
        await env.db.update_session("s", last_claim_epoch=None, task_id=None, claim_phase=None)
        await env.db.set_task_meta("t", "claimed_by_session",
                                   "s" if guard == "lost_epoch" else "someone-else")
    before = await env.db.get_task("t")
    result = await decide(env, current, "release", operator_release=True,
                          expected_hold_at=held["decided_at"])
    accepted = guard in (None, "branch_owner", "operation", "lost_epoch")
    assert ("error" not in result) is accepted, result
    after = await env.db.get_task("t")
    cleared = guard in (None, "lost_epoch")
    assert asdict(after) == {**asdict(before), "assigned_agent_id": None if cleared else "old"}
    recorded = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    assert recorded["decision"] == ("release" if accepted else "hold")
    if cleared:
        assert "already_assigned" not in {
            item["code"] for item in await env.db.claim_frontier_exclusions("t")
        }


@pytest.mark.parametrize("race", ["historical_release", "during_stop_probe"])
async def test_old_hold_release_cannot_clear_successor_assignment_or_hold(env, race):
    await env.db.create_agent(Agent(id="old", name="Old", profile_id="worker"))
    await env.db.create_agent(Agent(id="new", name="New", profile_id="worker"))
    await stopped_attempt(env, reason="claim_binding_stale", agent_id="old", lifecycle="pool")
    await env.db.update_session("s", last_claim_epoch=0)
    await env.db.queue_task_recovery_notifications()
    old = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    held, scope = await supervisor_handoff_hold(env, old)
    await env.db.transition_task(
        "t", TaskStatus.READY, force=True, context="test", assigned_agent_id="old",
    )
    if race == "historical_release":
        assert "error" not in await decide(
            env, old, "release", operator_release=True, expected_hold_at=held["decided_at"],
        )

    async def successor_holds(_=None):
        await env.db.update_task("t", claim_epoch=1)
        await env.db.transition_task(
            "t", TaskStatus.BLOCKED, force=True, context="test", assigned_agent_id="new",
        )
        await stopped_attempt(env, sid="successor", started=9000, reason="context_thrash",
                              agent_id="new", lifecycle="pool")
        await env.db.update_session("successor", last_claim_epoch=1)
        await env.db.set_task_meta("t", "claimed_by_session", "successor")
        await env.db.queue_task_recovery_notifications()
        current = await env.db.get_task_meta("t", "supervisor_recovery_incident")
        assert current["id"] != old["id"]
        assert "error" not in await decide(env, current, "hold", _scope=scope)
        return True

    if race == "historical_release":
        await successor_holds()
    else:
        env.orch.session_providers.create = lambda *_: SimpleNamespace(
            confirm_stopped=successor_holds,
        )
    result = await decide(
        env, old, "release", operator_release=True, expected_hold_at=held["decided_at"],
    )
    assert "error" in result, result
    task = await env.db.get_task("t")
    assert task.status is TaskStatus.BLOCKED
    assert task.claim_epoch == 1 and task.assigned_agent_id == "new"
    assert task.retry_count == 0
    current = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    assert current["id"] != old["id"] and current["decision"] == "hold"
    assert (await env.db.get_session("successor")).last_claim_epoch == 1


@pytest.mark.parametrize("authority", ["global", "project", "stopped", "wrong_instance",
                                       "worker", "service"])
async def test_operator_hold_release_requires_live_global_authority(env, authority):
    from src.commands.principal import ExecutionPrincipal, principal_context

    current = await incident(env)
    held, scope = await supervisor_handoff_hold(env, current)
    if authority in {"global", "stopped", "wrong_instance"}:
        await env.db.update_session("supervisor", project_id=None,
                                    **({"state": "stopped"} if authority == "stopped" else {}))
        scope["project_id"] = None
    scope["session_instance_token"] = "wrong" if authority == "wrong_instance" else "supervisor"
    if authority == "worker":
        scope["elevated"] = False
    if authority == "service":
        with principal_context(ExecutionPrincipal.service("reconciler")):
            result = await decide(env, current, "release", operator_release=True,
                                  expected_hold_at=held["decided_at"])
    else:
        result = await decide(env, current, "release", operator_release=True,
                              expected_hold_at=held["decided_at"], _scope=scope)
    assert ("error" not in result) is (authority == "global"), result


async def test_release_requires_flag_and_exact_hold(env):
    current = await incident(env)
    assert "error" in await decide(env, current, "release", operator_release=True,
                                   expected_hold_at=1)
    held, _ = await supervisor_handoff_hold(env, current)
    assert "error" in await decide(env, current, "release", expected_hold_at=held["decided_at"])
    assert "error" in await decide(env, current, "release", operator_release=True)
    assert "error" in await decide(env, {"id": "wrong"}, "release", operator_release=True,
                                   expected_hold_at=held["decided_at"])


async def bind_train_repair(env):
    async with env.db.immediate() as conn:
        await conn.execute(insert(integration_repair_operations).values(
            id="train-repair", target_kind="batch", batch_id="batch", episode_id="episode",
            state="human_required", active_stage=0, policy_snapshot={}, artifact_snapshot={},
            required_check_version="checks", created_at=100, updated_at=100,
        ))
        await conn.execute(insert(integration_repair_stages).values(
            operation_id="train-repair", ordinal=0, policy={"primary_attempts": 2},
            repair_task_id="t", writer_kind="repair_delegate", starting_sha="a" * 40,
            deadline_at=500, attempts=2, state="active",
        ))


async def test_release_only_keeps_integration_owner_stage_and_budget(env):
    current = await incident(env)
    held, _ = await supervisor_handoff_hold(env, current)
    await bind_train_repair(env)
    args = {"operator_release": True, "expected_hold_at": held["decided_at"]}
    assert "integration operation" in (await decide(env, current, **args))["error"]
    result = await decide(env, current, "release", **args)
    assert result["status"] == "BLOCKED", result
    async with env.db._engine.connect() as conn:
        operation = (await conn.execute(select(integration_repair_operations))).mappings().one()
        stage = (await conn.execute(select(integration_repair_stages))).mappings().one()
    assert operation["state"] == "human_required"
    assert stage["attempts"] == 2 and stage["deadline_at"] == 500


@pytest.mark.parametrize("kind", ["slot_reset_failed", "train", "legacy_train", "ordinary",
                                  "operator_hold", "ordinary_train", "stale_train"])
async def test_urgent_supervisor_hold_deadline_escalates_once_preserving_barrier(
    env, monkeypatch, kind,
):
    import src.database.queries.task_recovery_queries as recovery

    clock = [10_000.0]
    monkeypatch.setattr(recovery.time, "time", lambda: clock[0])
    current = await incident(env, reason="slot_reset_failed" if kind in {
        "slot_reset_failed", "operator_hold",
    } else "stuck_timeout")
    if kind == "operator_hold":
        assert "error" not in await decide(env, current, "hold")
        held = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    else:
        held, _ = await supervisor_handoff_hold(env, current)
    if kind in {"train", "legacy_train"}:
        await bind_train_repair(env)
    if kind in {"ordinary_train", "stale_train"}:
        await env.db.create_repo(RepoConfig(id="r", project_id="p", source_type=RepoSourceType.CLONE))
        await env.db.update_task("t", repo_id="r", created_by_kind="system", created_by_id="batch",
                                 dedup_key="repair:batch:1")
        async with env.db.immediate() as conn:
            await conn.execute(insert(integration_batches).values(
                id="batch", project_id="p", repository_id="r", request_id="request",
                target_ref="refs/heads/main", intent="open", repair_attempt_count=(
                    2 if kind == "stale_train" else 1
                ), source_manifest_digest="manifest", base_sha="a" * 40,
                lifecycle="repairing", integration_branch="aq/candidate/batch",
                policy_snapshot={}, artifact_snapshot={}, cleanup_state="pending",
                created_at=100, updated_at=100,
            ))
            await conn.execute(insert(task_context).values(
                id="t", task_id="t", type="snippet", label="Repair input", created_at=100,
                content=json.dumps({"batch_id": "batch", "attempt": 1}),
            ))
    if kind == "legacy_train":
        held.pop("decided_author_kind", None)
        held.pop("hold_deadline_at", None)
        await env.db.set_task_meta("t", "supervisor_recovery_incident", held)
    await env.db.queue_task_recovery_notifications()
    assert len(await queued_messages(env)) == 1
    clock[0] += recovery.RECOVERY_HOLD_SECONDS
    await asyncio.gather(env.db.queue_task_recovery_notifications(),
                         env.db.queue_task_recovery_notifications())
    messages_now = await queued_messages(env)
    urgent = kind in {"slot_reset_failed", "train", "legacy_train", "ordinary_train"}
    assert len(messages_now) == (3 if urgent else 1)
    expired = [row for row in messages_now if "-hold-expired-" in row["id"]]
    if urgent:
        assert {row["to_id"] for row in expired} == {"supervisor-p", "supervisor-global"}
        assert all(current["id"] in row["body"] and "--expected-hold-at" in row["body"]
                   for row in expired)
        updated = await env.db.get_task_meta("t", "supervisor_recovery_incident")
        assert updated["hold_deadline_at"] == held["decided_at"] + recovery.RECOVERY_HOLD_SECONDS
        assert updated["hold_escalated_at"] == clock[0]
        assert "deadline expired" in updated["next_action"]
    assert (await env.db.get_task_meta("t", "supervisor_recovery_incident"))["decision"] == "hold"
    assert (await env.db.get_task("t")).status == TaskStatus.BLOCKED
    assert (await env.db.get_task("t")).retry_count == 0
    await env.db.queue_task_recovery_notifications()
    assert len(await queued_messages(env)) == len(messages_now)


@pytest.mark.parametrize(
    "lifecycle,end_reason,held,accepted",
    [
        ("pool", "drained", True, True),
        ("pool", "drained", False, False),
        ("task", "drained", True, False),
        ("pool", "manual_stop", True, False),
    ],
)
async def test_only_exact_operational_pool_hold_can_release_a_drained_attempt(
    env, lifecycle, end_reason, held, accepted
):
    await stopped_attempt(env, reason=end_reason)
    await env.db.update_session("s", lifecycle=lifecycle)
    await env.db.set_task_meta("t", "needs_attention", "session_not_live")
    await env.db.queue_task_recovery_notifications()
    current = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    args = {}
    if held:
        assert "error" not in await decide(env, current, "hold")
        hold = await env.db.get_task_meta("t", "supervisor_recovery_incident")
        args["expected_hold_at"] = hold["decided_at"]
    result = await decide(env, current, **args)
    assert ("error" not in result) is accepted, result
    task = await env.db.get_task("t")
    assert task.retry_count == int(accepted)
    assert task.status == (TaskStatus.READY if accepted else TaskStatus.BLOCKED)
    if held:
        recorded = await env.db.get_task_meta("t", "supervisor_recovery_incident")
        assert recorded["decision"] == ("retry" if accepted else "hold")
        assert bool(recorded["decision_history"]) is accepted


@pytest.mark.parametrize("guard", ["budget", "live_worker", "hold_label"])
@pytest.mark.parametrize("drained", [False, True])
async def test_hold_release_rechecks_recovery_guards(env, guard, drained):
    if drained:
        await stopped_attempt(env, reason="drained")
        await env.db.update_session("s", lifecycle="pool")
        await env.db.set_task_meta("t", "needs_attention", "session_not_live")
        await env.db.queue_task_recovery_notifications()
        current = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    else:
        current = await incident(env)
    assert "error" not in await decide(env, current, "hold")
    held = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    if guard == "budget":
        await env.db.update_task("t", retry_count=3)
    elif guard == "live_worker":
        env.orch.session_providers.create = lambda *_: SimpleNamespace(
            confirm_stopped=AsyncMock(return_value=False)
        )
    else:
        await env.db.add_task_label("t", "hold:recovery")
    result = await decide(env, current, expected_hold_at=held["decided_at"])
    assert "error" in result, result
    assert (await env.db.get_task("t")).status == TaskStatus.BLOCKED
    assert (await env.db.get_task_meta("t", "supervisor_recovery_incident"))["decision"] == "hold"


@pytest.mark.parametrize(
    "guard",
    [
        "pause",
        "project_pause",
        "gate",
        "hold_label",
        "budget",
        "live",
        "routing",
        "new_attempt",
        "retry_budget",
        "approval_reason",
    ],
)
async def test_retry_rejects_protected_or_changed_state(env, guard):
    current = await incident(env)
    if guard == "pause":
        await env.db.pause_task("t")
    elif guard == "project_pause":
        await env.db.update_project("p", status="PAUSED")
    elif guard == "gate":
        await env.db.create_gate(
            project_id="p", gate_type="human", title="Review", waiter_task_ids=["t"]
        )
    elif guard == "hold_label":
        await env.db.add_task_label("t", "hold:human")
    elif guard == "budget":
        await env.db.update_project("p", budget_limit=1, total_tokens_used=1)
    elif guard == "live":
        await env.db.create_session(
            SessionRecord(
                id="live",
                task_id="t",
                project_id="p",
                profile_id="worker",
                harness="fake",
                provider="fake",
                name="live",
                lifecycle="task",
                state="running",
                desired_state="running",
                epoch="test",
                instance_token="live",
                work_dir="/never-used",
                started_at=9000,
            )
        )
    elif guard == "routing":
        await env.db.update_task("t", intelligence_class="fast-low")
    elif guard == "new_attempt":
        await incident(env, sid="new", started=9000)
    elif guard == "retry_budget":
        await env.db.update_task("t", retry_count=3)
    else:
        await env.db.set_task_meta("t", "needs_attention", "human_approval_required")
    before = await env.db.get_task("t")
    result = await decide(env, current)
    assert "error" in result, result
    after = await env.db.get_task("t")
    assert after.status == before.status and after.retry_count == before.retry_count
    assert await env.db.get_task_meta("t", "supervisor_recovery_attempts") is None


async def test_hold_decision_is_durable_and_does_not_restart(env):
    current = await incident(env)
    assert "error" not in await decide(env, current, "hold")
    await env.db.queue_task_recovery_notifications()
    assert "error" in await decide(env, current)
    assert (await env.db.get_task("t")).status == TaskStatus.BLOCKED
    assert (await env.db.get_task_meta("t", "supervisor_recovery_incident"))["decision"] == "hold"


async def test_recorded_recovery_hold_excludes_task_projected_back_to_ready(env):
    current = await incident(env)
    assert "error" not in await decide(env, current, "hold")
    held = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    assert held["retry_allowed"] is False
    # A structural status update cannot authorize another execution attempt.
    await env.db.update_task("t", status=TaskStatus.READY)
    assert await env.db.count_ready_by_profile("p") == {}
    exclusions = await env.db.claim_frontier_exclusions("t")
    assert [item["code"] for item in exclusions] == ["frontier_recovery_hold"]
    await env.db.queue_task_recovery_notifications()
    assert await env.db.get_task_meta("t", "supervisor_recovery_incident") == held


@pytest.mark.parametrize("prior", [TaskStatus.READY, TaskStatus.BLOCKED])
@pytest.mark.parametrize("held", [False, True])
async def test_resume_supersedes_old_recovery_without_resetting_budgets(env, prior, held):
    current = await incident(env)
    if held:
        assert "error" not in await decide(env, current, "hold")
    recorded = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    await env.db.update_task("t", status=prior, retry_count=2, route_source="override")
    await env.db.set_task_meta("t", "supervisor_recovery_attempts", 1)
    await env.db.pause_task("t")
    await env.db.queue_task_recovery_notifications()
    if held:
        assert await env.db.get_task_meta("t", "supervisor_recovery_incident") == recorded
    ready = []
    env.db.set_ready_listener(lambda entries: record_ready(ready, entries))

    result = await env.handler.execute("resume_task", {"task_id": "t"})

    assert "error" not in result, result
    task = await env.db.get_task("t")
    assert task.status is prior and task.retry_count == 2
    assert task.branch_name == "aq/keep" and task.intelligence_class == "deep-high"
    assert await env.db.get_task_meta("t", "supervisor_recovery_attempts") == 1
    superseded = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    assert superseded["id"] == current["id"] and superseded["decision"] == "superseded"
    if held:
        assert "manual_resume" in superseded["decision_reason"]
        assert superseded["decision_history"][-1] == {
            key: recorded[key]
            for key in ("decision", "decision_reason", "decided_at", "decided_by")
        }
    assert (await env.db.get_message("msg-" + current["id"])).archived_at is not None
    assert "frontier_recovery_hold" not in {
        item["code"] for item in await env.db.claim_frontier_exclusions("t")
    }
    if prior is TaskStatus.READY:
        assert any(tid == "t" for tid, _ in ready)
        assert await env.db.count_ready_by_profile("p") == {"worker": 1}


async def test_resume_recovery_supersession_rolls_back_with_status(env):
    current = await incident(env)
    assert "error" not in await decide(env, current, "hold")
    await env.db.update_task("t", status=TaskStatus.READY)
    # Complete the stopped-session cleanup before exercising resume's rollback.
    await env.orch.pause_task("t")
    held = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    message_id = "msg-" + current["id"]
    archived_at = (await env.db.get_message(message_id)).archived_at

    with pytest.raises(RuntimeError, match="abort resume"):
        async with env.db.immediate() as conn:
            encoded = await env.db._read_manual_pause(conn, "t")
            await env.db._resume_locked(conn, "t", encoded)
            raise RuntimeError("abort resume")

    assert (await env.db.get_task("t")).status is TaskStatus.PAUSED
    assert await env.db.get_task_meta("t", "supervisor_recovery_incident") == held
    assert await env.db.get_task_meta("t", "manual_pause") is not None
    assert (await env.db.get_message(message_id)).archived_at == archived_at


async def test_resume_supersedes_held_failure_without_worker_attempt(env):
    await env.db.transition_task("t", TaskStatus.FAILED, force=True)
    await notify(env)
    current = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    assert current["attempt_id"] is None
    assert "error" not in await decide(env, current, "hold")
    held = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    # Legacy requeueing could retain a no-attempt failure's hold too.
    await env.db.update_task("t", status=TaskStatus.READY, route_source="override")
    await env.db.pause_task("t")
    await env.db.queue_task_recovery_notifications()
    assert await env.db.get_task_meta("t", "supervisor_recovery_incident") == held

    result = await env.handler.execute("resume_task", {"task_id": "t"})

    assert "error" not in result, result
    assert (await env.db.get_task("t")).status is TaskStatus.READY
    superseded = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    assert superseded["decision"] == "superseded"
    assert superseded["decision_history"][-1]["decided_at"] == held["decided_at"]
    assert await env.db.get_task_meta("t", "supervisor_recovery_attempts") is None


async def test_refused_resume_keeps_recovery_hold(env):
    current = await incident(env)
    assert "error" not in await decide(env, current, "hold")
    await env.db.pause_task("t")
    saved = await env.db.get_task_meta("t", "manual_pause")
    await env.db.set_task_meta("t", "manual_pause", {**saved, "cleanup_pending": True})
    held = await env.db.get_task_meta("t", "supervisor_recovery_incident")

    with pytest.raises(ValueError, match="pause cleanup is pending"):
        await env.db.resume_task("t")
    assert (await env.db.get_task("t")).status is TaskStatus.PAUSED
    assert await env.db.get_task_meta("t", "supervisor_recovery_incident") == held


@pytest.mark.parametrize("change", ["attention", "attempt", "claim_epoch"])
async def test_scan_supersedes_obsolete_hold_without_losing_diagnosis(env, change):
    current = await incident(env)
    assert "error" not in await decide(env, current, "hold")
    held = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    await env.db.update_task("t", status=TaskStatus.READY, route_source="override")
    if change == "attention":
        # An older resume removed the failure reason but retained its hold.
        await env.db.set_task_meta("t", "needs_attention", None)
    elif change == "attempt":
        await stopped_attempt(env, sid="replacement", started=9000)
    else:
        await env.db.update_task("t", claim_epoch=1)

    await asyncio.gather(
        env.db.queue_task_recovery_notifications(), env.db.queue_task_recovery_notifications(),
    )

    superseded = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    assert superseded["decision"] == "superseded"
    assert superseded["decision_history"] == [{
        key: held[key]
        for key in ("decision", "decision_reason", "decided_at", "decided_by")
    }]
    assert (await env.db.get_message("msg-" + current["id"])).archived_at is not None
    assert await env.db.claim_frontier_exclusions("t") == []
    assert await env.db.count_ready_by_profile("p") == {"worker": 1}
    assert (await env.db.get_task("t")).retry_count == 0
    assert await env.db.get_task_meta("t", "supervisor_recovery_attempts") is None

RECLASSIFY_KEPT = (
    "id", "decision", "decision_reason", "decided_at", "decided_by", "decision_history",
    "ask_decision_id", "hold_deadline_at", "deadline_kind", "redeliveries", "attempt_id",
    "session_id", "end_reason", "supervisor_recoveries",
)


async def recovery_rows(env):
    rows = await queued_messages(env)
    return [row for row in rows if row["body_kind"] == "task_recovery"]


async def reclassified_comments(env):
    return [
        comment for comment in (await env.db.list_task_comments("t"))["comments"]
        if "reclassified" in comment["body"]
    ]


@pytest.mark.parametrize("held", ["pending", "supervisor_hold", "human_hold"])
async def test_reclassified_stopped_attempt_keeps_one_incident_hold_and_budget(env, held):
    """A refused stale-binding unwind re-diagnoses the SAME stopped attempt.

    The legacy record (no identity_reason) keeps its id, decision, provenance and
    budget; no second notification, human decision or automatic retry follows.
    """
    current = await incident(env, reason="context_thrash")
    assert "identity_reason" not in current
    if held == "supervisor_hold":
        await supervisor_handoff_hold(env, current)
    elif held == "human_hold":
        assert (await apply_human_recovery_reply(env, current, "hold"))["success"]
    await env.db.queue_task_recovery_notifications()
    before = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    assert before["decision"] == (None if held == "pending" else "hold")

    await env.db.set_task_meta("t", "needs_attention", "claim_binding_stale")
    # A late decision against the old diagnosis is refused until the scan records it.
    late = await decide(env, current, "hold")
    assert "error" in late, late
    if held == "pending":
        assert "diagnosis changed" in late["error"]
    await asyncio.gather(
        env.db.queue_task_recovery_notifications(), env.db.queue_task_recovery_notifications(),
    )
    for _ in range(2):
        replay = await notify(env)
        assert replay["incident_id"] == current["id"], replay
    await env.db.queue_task_recovery_notifications()

    after = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    assert {key: after.get(key) for key in RECLASSIFY_KEPT} == {
        key: before.get(key) for key in RECLASSIFY_KEPT
    }
    assert after["reason"] == "claim_binding_stale"
    assert after["identity_reason"] == "context_thrash"
    assert [(entry["from"], entry["to"]) for entry in after["reason_history"]] == [
        ("context_thrash", "claim_binding_stale"),
    ]
    assert after["retry_allowed"] is False and after["retry_reason_allowed"] is False
    assert "Not an automatically retryable failure" in after["next_action"] or held != "pending"
    assert [row["id"] for row in await recovery_rows(env)] == ["msg-" + current["id"]]
    assert len(await reclassified_comments(env)) == 1
    task = await env.db.get_task("t")
    assert task.status is TaskStatus.BLOCKED and task.retry_count == 0
    assert await env.db.get_task_meta("t", "supervisor_recovery_attempts") is None
    assert "error" in await decide(env, current)
    if held == "pending":
        assert "error" not in await decide(env, current, "hold")
        assert (await env.db.get_task_meta("t", "supervisor_recovery_incident"))["id"] == current["id"]


@pytest.mark.parametrize("first,second,allowed", [
    ("context_thrash", "stuck_timeout", False),
    ("stuck_timeout", "exited_holding_task", True),
])
async def test_reclassification_never_relabels_nonautomatic_failure_as_retryable(
    env, first, second, allowed,
):
    await stopped_attempt(env, reason="stuck_timeout", attention=False)
    await env.db.set_task_meta("t", "needs_attention", first)
    await env.db.queue_task_recovery_notifications()
    current = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    await env.db.set_task_meta("t", "needs_attention", second)
    await env.db.queue_task_recovery_notifications()
    after = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    assert after["id"] == current["id"] and after["reason"] == second
    assert after["retry_allowed"] is allowed, after
    result = await decide(env, current)
    if allowed:
        assert result["status"] == "READY", result
        assert await env.db.get_task_meta("t", "supervisor_recovery_attempts") == 1
    else:
        assert "diagnosed as not automatically retryable" in result["error"], result
        assert (await env.db.get_task("t")).retry_count == 0
        assert await env.db.get_task_meta("t", "supervisor_recovery_attempts") is None


@pytest.mark.parametrize("scan", [False, True])
async def test_reclassification_makes_prepared_human_packet_stale(env, scan):
    from src.asks.models import SourceIdentity
    current = await incident(env)
    ask = await env.handler._publish_source_ask(
        SourceIdentity("p", "recovery", current["id"], {"task_id": "t"})
    )
    args = {"ask_id": ask["id"], "expected": {k: ask[k] for k in (
        "decision_generation", "source_fingerprint", "feedback_version")},
        "option_key": "retry", "overall_note": "Preserve evidence and retry",
        "idempotency_key": "recovery-reclassified"}
    preview = await env.handler._cmd_ask_prepare_decision(args)
    assert preview["success"], preview
    # Still a retryable diagnosis: only the changed facts make the approval stale.
    await env.db.set_task_meta("t", "needs_attention", "session_not_live")
    if scan:
        await env.db.queue_task_recovery_notifications()
    result = await env.handler._cmd_ask_decide({**args, "preview_id": preview["preview"]["id"]})
    assert result["success"] is False and result["error_code"] == "ask.stale_source", result
    task = await env.db.get_task("t")
    assert task.status is TaskStatus.BLOCKED and task.retry_count == 0
    assert await env.db.get_task_meta("t", "supervisor_recovery_attempts") is None
    recorded = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    assert recorded["id"] == current["id"] and recorded["decision"] is None
    assert recorded.get("ask_decision_id") is None


async def test_reclassification_back_and_forth_is_recorded_once_each(env):
    current = await incident(env, reason="context_thrash")
    for reason in ("claim_binding_stale", "context_thrash", "context_thrash"):
        await env.db.set_task_meta("t", "needs_attention", reason)
        await env.db.queue_task_recovery_notifications()
    after = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    assert after["id"] == current["id"] and after["reason"] == "context_thrash"
    assert [(entry["from"], entry["to"]) for entry in after["reason_history"]] == [
        ("context_thrash", "claim_binding_stale"), ("claim_binding_stale", "context_thrash"),
    ]
    assert len(await reclassified_comments(env)) == 2
    assert len(await recovery_rows(env)) == 1
    assert "error" not in await decide(env, current, "hold")


async def test_new_stopped_attempt_after_reclassification_is_a_new_incident(env):
    current = await incident(env, reason="context_thrash")
    assert "error" not in await decide(env, current, "hold")
    await env.db.set_task_meta("t", "needs_attention", "claim_binding_stale")
    await env.db.queue_task_recovery_notifications()
    await stopped_attempt(env, sid="successor", started=9000)
    await env.db.queue_task_recovery_notifications()
    new = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    assert new["id"] != current["id"]
    assert new["decision"] is None and "reason_history" not in new
    assert new["supervisor_recoveries"] == 0
    assert (await env.db.get_message("msg-" + current["id"])).archived_at is not None
    assert len(await recovery_rows(env)) == 2


async def test_no_attempt_failure_reclassifies_but_new_transition_is_new_incident(env):
    await env.db.transition_task("t", TaskStatus.FAILED, force=True)
    await notify(env)
    current = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    assert current["reason"] == "terminal_failed"
    assert "error" not in await decide(env, current, "hold")
    await env.db.set_task_meta("t", "needs_attention", "operator_review")
    await env.db.queue_task_recovery_notifications()
    kept = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    assert kept["id"] == current["id"] and kept["decision"] == "hold"
    assert kept["reason"] == "operator_review" and kept["identity_reason"] == "terminal_failed"
    assert len(await recovery_rows(env)) == 1

    for status in (TaskStatus.READY, TaskStatus.FAILED):
        await env.db.transition_task("t", status, force=True)
    await env.db.queue_task_recovery_notifications()
    new = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    assert new["id"] != current["id"] and new["decision"] is None
    assert (await env.db.get_message("msg-" + current["id"])).archived_at is not None


async def test_reclassified_integration_delegate_keeps_operation_stage_and_budget(env):
    await bind_train_repair(env)
    current = await incident(env)
    await env.db.set_task_meta("t", "needs_attention", "claim_binding_stale")
    await asyncio.gather(
        env.db.queue_task_recovery_notifications(), env.db.queue_task_recovery_notifications(),
    )
    after = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    assert after["id"] == current["id"] and after["reason"] == "claim_binding_stale"
    assert after["owner"]["kind"] == "integration_operation"
    assert after["owner"]["operation_id"] == "train-repair"
    assert after["retry_allowed"] is False
    assert await recovery_rows(env) == []
    async with env.db._engine.connect() as conn:
        operation = (await conn.execute(select(integration_repair_operations))).mappings().one()
        stage = (await conn.execute(select(integration_repair_stages))).mappings().one()
    assert operation["state"] == "human_required"
    assert stage["attempts"] == 2 and stage["deadline_at"] == 500
    assert stage["repair_task_id"] == "t" and stage["state"] == "active"


async def test_reclassified_ready_hold_projection_stays_held(env):
    current = await incident(env)
    assert "error" not in await decide(env, current, "hold")
    held = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    await env.db.update_task("t", status=TaskStatus.READY, route_source="override")
    await env.db.set_task_meta("t", "needs_attention", "claim_binding_stale")
    await env.db.queue_task_recovery_notifications()
    kept = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    assert kept["id"] == held["id"] and kept["decision"] == "hold"
    assert kept["decided_at"] == held["decided_at"]
    assert {item["code"] for item in await env.db.claim_frontier_exclusions("t")} >= {
        "frontier_recovery_hold"
    }


@pytest.mark.parametrize("guard", ["human_gate", "hold_label", "dependency"])
async def test_resume_releases_recovery_hold_preserving_other_frontier_guards(env, guard):
    current = await incident(env)
    assert "error" not in await decide(env, current, "hold")
    await env.db.update_task("t", status=TaskStatus.READY, route_source="override")
    if guard == "human_gate":
        gate_id, _ = await env.db.create_gate(
            project_id="p", gate_type="human", title="Approval", waiter_task_ids=["t"],
        )
    elif guard == "hold_label":
        await env.db.add_task_label("t", "hold:operator")
    else:
        await env.db.create_task(Task(id="upstream", project_id="p", title="Upstream", description=""))
        await env.db.add_dependency("t", "upstream")
    await env.db.pause_task("t")

    result = await env.handler.execute("resume_task", {"task_id": "t"})

    assert "error" not in result, result
    codes = {item["code"] for item in await env.db.claim_frontier_exclusions("t")}
    assert codes and "frontier_recovery_hold" not in codes
    assert await env.db.count_ready_by_profile("p") == {}
    if guard == "human_gate":
        assert (await env.db.get_gate(gate_id))["status"] == "open"


async def test_slot_reset_resume_supersedes_old_recovery_hold(env):
    current = await incident(env)
    assert "error" not in await decide(env, current, "hold")
    await env.db.set_task_meta("t", "slot_reset_failure", {"error": "reset failed"})

    result = await env.handler.execute("resume_task", {"task_id": "t"})

    assert "error" not in result, result
    assert (await env.db.get_task("t")).status is TaskStatus.READY
    superseded = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    assert superseded["decision"] == "superseded"
    assert "slot_reset_retry" in superseded["decision_reason"]
    assert await env.db.claim_frontier_exclusions("t") == []


@pytest.mark.parametrize("command", ["restart_task", "reopen_with_feedback"])
@pytest.mark.parametrize("prior", [TaskStatus.BLOCKED, TaskStatus.FAILED, TaskStatus.READY])
@pytest.mark.parametrize("held", [False, True])
async def test_explicit_restart_supersedes_recovery_incident_before_scan(env, command, prior, held):
    current = await incident(env)
    if held:
        assert "error" not in await decide(env, current, "hold")
    recorded = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    await env.db.update_task("t", status=prior, retry_count=2)
    await env.db.set_task_meta("t", "supervisor_recovery_attempts", 1)
    # Keep a routed profile so frontier membership measures the hold itself.
    await env.db.update_task("t", route_source="override")
    ready = []
    env.db.set_ready_listener(lambda entries: record_ready(ready, entries))
    args = {"task_id": "t", "feedback": "Retry with the diagnosed fix"}

    result = await env.handler.execute(command, args)

    assert "error" not in result, result
    task = await env.db.get_task("t")
    assert task.status is TaskStatus.READY and task.retry_count == 0
    assert task.branch_name == "aq/keep"
    assert await env.db.claim_frontier_exclusions("t") == []
    assert await env.db.count_ready_by_profile("p") == {"worker": 1}
    superseded = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    assert superseded["id"] == current["id"]
    assert superseded["decision"] == "superseded"
    assert command in superseded["decision_reason"]
    if held:
        assert superseded["decision_history"][-1] == {
            key: recorded[key]
            for key in ("decision", "decision_reason", "decided_at", "decided_by")
        }
        assert ("t", "restarted") in ready
    assert await env.db.get_task_meta("t", "supervisor_recovery_attempts") == 1
    assert (await env.db.get_message("msg-" + current["id"])).archived_at is not None
    assert "error" in await decide(env, current, "hold")
    await env.db.queue_task_recovery_notifications()
    assert await env.db.get_task_meta("t", "supervisor_recovery_incident") == superseded


async def record_ready(observed, entries):
    observed.extend(entries)


@pytest.mark.parametrize("command", ["restart_task", "reopen_with_feedback"])
@pytest.mark.parametrize("held", [False, True])
@pytest.mark.parametrize("has_attempt", [False, True])
async def test_restart_incident_supersession_rolls_back_with_ready_write(
    env, command, held, has_attempt,
):
    if has_attempt:
        current = await incident(env)
    else:
        await env.db.transition_task("t", TaskStatus.FAILED, force=True)
        await notify(env)
        current = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    if held:
        assert "error" not in await decide(env, current, "hold")
    status = (await env.db.get_task("t")).status
    before = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    message_id = "msg-" + current["id"]
    archived_at = (await env.db.get_message(message_id)).archived_at

    with pytest.raises(RuntimeError, match="abort restart"):
        async with env.db.immediate() as conn:
            await env.db._apply_transition(
                conn, "t", TaskStatus.READY, context=command, retry_count=0,
            )
            raise RuntimeError("abort restart")

    assert (await env.db.get_task("t")).status is status
    assert await env.db.get_task_meta("t", "supervisor_recovery_incident") == before
    assert (await env.db.get_message(message_id)).archived_at == archived_at


@pytest.mark.parametrize("command", ["restart_task", "reopen_with_feedback"])
async def test_refused_explicit_restart_preserves_recovery_hold(env, command):
    current = await incident(env)
    assert "error" not in await decide(env, current, "hold")
    held = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    await env.db.pause_task("t")
    before = await env.db.get_task("t")

    result = await env.handler.execute(command, {"task_id": "t", "feedback": "Try again"})

    assert "error" in result, result
    after = await env.db.get_task("t")
    assert (after.status, after.retry_count, after.description) == (
        before.status, before.retry_count, before.description,
    )
    assert await env.db.get_task_meta("t", "supervisor_recovery_incident") == held


@pytest.mark.parametrize("command", ["restart_task", "reopen_with_feedback"])
@pytest.mark.parametrize("guard", ["human_gate", "hold_label", "dependency"])
async def test_restart_releases_only_recovery_hold_preserving_other_frontier_guards(
    env, command, guard,
):
    current = await incident(env)
    assert "error" not in await decide(env, current, "hold")
    gate_id = None
    if guard == "human_gate":
        gate_id, _ = await env.db.create_gate(
            project_id="p", gate_type="human", title="Approval", waiter_task_ids=["t"],
        )
    elif guard == "hold_label":
        await env.db.add_task_label("t", "hold:operator")
    else:
        await env.db.create_task(Task(id="upstream", project_id="p", title="Upstream", description=""))
        await env.db.add_dependency("t", "upstream")
    await env.db.update_task("t", route_source="override")

    result = await env.handler.execute(command, {"task_id": "t", "feedback": "Try again"})

    assert "error" not in result, result
    assert (await env.db.get_task("t")).status is TaskStatus.READY
    assert (await env.db.get_task_meta("t", "supervisor_recovery_incident"))["decision"] == "superseded"
    codes = {item["code"] for item in await env.db.claim_frontier_exclusions("t")}
    assert "frontier_recovery_hold" not in codes
    assert codes
    assert await env.db.count_ready_by_profile("p") == {}
    if gate_id:
        assert (await env.db.get_gate(gate_id))["status"] == "open"


@pytest.mark.parametrize("decision,expected_status", [("retry", "READY"), ("hold", "BLOCKED")])
async def test_human_evidence_drives_guarded_recovery_or_keep_blocked(env, decision, expected_status):
    current = await incident(env)
    result = await apply_human_recovery_reply(env, current, decision)
    assert result["success"] is True, result
    assert result["escalation"]["state"] == "resolved"
    assert result["receipt"]["decision"] == decision
    assert (await env.db.get_task("t")).status.value == expected_status


async def test_integration_owned_recovery_keeps_operation_authority_and_budget(env, monkeypatch):
    current = await incident(env)
    monkeypatch.setattr(
        env.db,
        "get_active_integration_repair_for_task",
        AsyncMock(return_value={"id": "integration-operation"}),
    )
    result = await decide(env, current, decision="hold")
    assert "integration operation integration-operation" in result["error"]
    assert (await env.db.get_task("t")).status == TaskStatus.BLOCKED
    assert (await env.db.get_task_meta("t", "supervisor_recovery_incident"))["decision"] is None
    assert await env.db.get_task_meta("t", "supervisor_recovery_attempts") is None


async def test_two_recoveries_maximum_even_if_manual_retry_count_reset(env):
    for n in range(3):
        await env.db.transition_task("t", TaskStatus.BLOCKED, force=True, retry_count=0)
        current = await incident(env, sid=f"s{n}", started=200 + n * 8000)
        result = await decide(env, current)
        assert ("error" in result) == (n == 2), result
    assert await env.db.get_task_meta("t", "supervisor_recovery_attempts") == 2


async def test_workers_and_foreign_project_supervisors_cannot_recover(env):
    current = await incident(env)
    for scope in [
        RequestScope(kind="session", session_id="x", project_id="p"),
        RequestScope(kind="session", session_id="x", project_id="other", elevated=True),
    ]:
        result = await decide(env, current, _scope=asdict(scope))
        assert "scope" in result.get("error", "").lower(), result


async def test_recovery_metadata_cannot_be_reset_by_task_set(env):
    result = await env.handler.execute(
        "task_set", {"task_id": "t", "meta": {"supervisor_recovery_attempts": 0}}
    )
    assert "reserved" in result.get("error", "").lower()


async def test_ordinary_dependency_blocks_are_not_incidents(env):
    await env.db.queue_task_recovery_notifications()
    async with env.db._engine.connect() as conn:
        assert not (await conn.execute(select(messages))).all()


async def test_orchestrator_enqueues_before_delivery_and_respects_disabled_messaging(env):
    env.orch.message_delivery = SimpleNamespace(
        run_delivery_pass=AsyncMock(), check_reply_timeouts=AsyncMock()
    )
    env.db.queue_task_recovery_notifications = AsyncMock()
    env.config.messages.enabled = False
    await env.orch._deliver_messages()
    env.db.queue_task_recovery_notifications.assert_not_awaited()
    env.config.messages.enabled = True
    await env.orch._deliver_messages()
    env.db.queue_task_recovery_notifications.assert_awaited_once()


async def test_manual_stop_with_stale_attention_metadata_cannot_retry(env):
    current = await incident(env)
    from sqlalchemy import update
    from src.database.tables import task_session_attempts

    async with env.db._engine.begin() as conn:
        await conn.execute(update(task_session_attempts).values(end_reason="manual_stop"))
    assert "error" in await decide(env, current)


async def test_internal_recovery_nudge_does_not_request_a_user_reply():
    from src.messages.delivery import _render_nudge
    from src.models import Message

    notice = Message(
        id="internal",
        project_id=None,
        from_kind="system",
        from_id="task-recovery",
        to_kind="session",
        to_id="supervisor-global",
        body="inspect and decide",
        body_kind="task_recovery",
    )
    assert "aq reply" not in _render_nudge([notice])
    user = Message(
        id="user-message",
        project_id=None,
        from_kind="user",
        from_id="user",
        to_kind="session",
        to_id="supervisor-global",
        body="question",
    )
    mixed = _render_nudge([notice, user])
    assert mixed == "Handle `aq message status internal --json` (running it marks the message read)."
    assert "question" not in mixed


@pytest.mark.parametrize("still_running", [True, "unknown"])
async def test_stopped_record_requires_confirmed_process_exit(env, still_running):
    current = await incident(env)
    probe = (
        AsyncMock(return_value=False)
        if still_running is True
        else AsyncMock(side_effect=RuntimeError("unavailable"))
    )
    env.orch.session_providers = SimpleNamespace(
        create=lambda *_: SimpleNamespace(confirm_stopped=probe)
    )
    result = await decide(env, current)
    assert "error" in result
    assert (await env.db.get_task("t")).status == TaskStatus.BLOCKED
    assert await env.db.get_task_meta("t", "supervisor_recovery_attempts") is None


async def test_delivered_incident_rearmed_after_supervisor_exit_without_duplicate_row(env):
    from sqlalchemy import update

    await env.db.create_session(
        SessionRecord(
            id="supervisor",
            project_id="p",
            profile_id="worker",
            harness="fake",
            provider="fake",
            name="n-supervisor--p",
            lifecycle="named",
            state="running",
            desired_state="running",
            epoch="test",
            instance_token="supervisor",
            work_dir="/never-used",
            started_at=100,
        )
    )
    current = await incident(env)
    message_id = "msg-" + current["id"]
    await env.db.mark_delivered(message_id)
    async with env.db._engine.begin() as conn:
        await conn.execute(
            update(messages)
            .where(messages.c.id == message_id)
            .values(delivered_at=300, archived_at=300)
        )
    await env.db.queue_task_recovery_notifications()
    assert (await env.db.get_message(message_id)).delivered_at == 300
    await env.db.update_session("supervisor", state="stopped", desired_state="stopped")
    await env.db.queue_task_recovery_notifications()
    assert (await env.db.get_message(message_id)).delivered_at is None
    assert (await env.db.get_message(message_id)).archived_at is None
    async with env.db._engine.connect() as conn:
        assert len((await conn.execute(select(messages))).all()) == 1
    assert (await env.db.get_task_meta("t", "supervisor_recovery_incident"))["redeliveries"] == 1


async def test_supervisor_unavailable_watchdog_is_bounded_deduplicated_and_non_actionable(env):
    from src.escalations import SupervisorDeliveryWatchdog

    current = await incident(env)
    message = await env.db.get_message("msg-" + current["id"])
    watchdog = SupervisorDeliveryWatchdog(env.db, env.orch.bus, env.config)
    assert await watchdog.tick(message.created_at + 901) == 1
    assert await watchdog.tick(message.created_at + 1800) == 0
    escalations = await env.db.list_escalations(project_id="p")
    watchdog_incidents = [e for e in escalations if e["source_kind"] == "supervisor_delivery"]
    assert len(watchdog_incidents) == 1
    unavailable = watchdog_incidents[0]
    assert unavailable["source_identity"] == "task_recovery:" + current["id"]
    assert "does not approve" in unavailable["decision_requested"]

    accepted = await env.handler.execute(
        "escalation_reply",
        {
            "escalation_id": unavailable["id"],
            "text": "Retry it",
            "external_message_id": "watchdog-reply",
        },
    )
    await env.db.create_session(
        SessionRecord(
            id="supervisor-p",
            project_id="p",
            profile_id="supervisor",
            harness="fake",
            provider="fake",
            name="n-supervisor--p",
            lifecycle="named",
            state="running",
            desired_state="running",
            epoch="test",
            instance_token="supervisor-p-token",
            work_dir="/never-used",
            started_at=message.created_at,
        )
    )
    result = await env.handler.execute(
        "escalation_apply_reply",
        {
            "escalation_id": unavailable["id"],
            "reply_id": accepted["reply"]["id"],
            "expected_revision": accepted["escalation"]["revision"],
            "idempotency_key": "watchdog-cannot-retry",
            "action_kind": "task_recover",
            "target_id": "t",
            "decision": "retry",
            "_scope": {
                "kind": "session",
                "session_id": "supervisor-p",
                "session_instance_token": "supervisor-p-token",
                "project_id": "p",
                "elevated": True,
            },
        },
    )
    assert result["error_code"] == "invalid_binding"
    assert (await env.db.get_task("t")).status == TaskStatus.BLOCKED


@pytest.mark.parametrize("probe", ["unavailable", "still-listed"])
async def test_cached_tmux_absence_is_not_stop_confirmation(env, probe):
    from src.sessions.tmux import TmuxProvider, TmuxCommandError

    current = await incident(env)
    provider = TmuxProvider(env.config)
    provider.socket = "disposable-never-contacted"
    provider.is_running = AsyncMock(return_value=False)
    provider._tmux = (
        AsyncMock(side_effect=TmuxCommandError(("list-sessions",), 1, "probe unavailable"))
        if probe == "unavailable"
        else AsyncMock(return_value="s\n")
    )
    env.orch.session_providers = SimpleNamespace(create=lambda *_: provider)
    result = await decide(env, current)
    assert "error" in result
    assert (await env.db.get_task("t")).status == TaskStatus.BLOCKED


async def test_strict_tmux_probe_bypasses_cached_absence_and_confirms_missing_name():
    from src.sessions.tmux import TmuxProvider
    from src.sessions.provider import SessionHandle

    provider = TmuxProvider()
    provider.socket = "disposable-never-contacted"
    provider._tmux = AsyncMock(return_value="n-supervisor--global\nother-worker\n")
    assert (
        await provider.confirm_stopped(SessionHandle("old-worker", "tmux", "old-instance")) is True
    )
    provider._tmux.assert_awaited_once_with("list-sessions", "-F", "#{session_name}")


async def test_failure_event_and_scan_share_one_incident_with_visible_budget(env):
    await stopped_attempt(env)
    first = await notify(env)
    assert first["outcome"] == "queued", first
    await env.db.queue_task_recovery_notifications()
    replay = await notify(env)
    assert replay["outcome"] == "existing"
    assert replay["incident_id"] == first["incident_id"]
    queued = await queued_messages(env)
    assert [m["id"] for m in queued] == ["msg-" + first["incident_id"]]

    current = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    assert current["id"] == first["incident_id"]
    assert current["owner"] == {"kind": "supervisor", "id": "supervisor-p"}
    assert current["budget"] == {
        "worker_retries": {"used": 0, "limit": 3, "remaining": 3},
        "supervisor_recoveries": {"used": 0, "limit": 2, "remaining": 2},
    }
    # A task session's watchdog measures wall-clock runtime, not inactivity.
    assert current["deadline_kind"] == "runtime"
    assert current["runtime_seconds"] == 7200 and current["idle_seconds"] == 1
    assert current["retry_allowed"] is True
    assert "aq task recover" in current["next_action"]
    body = queued[0]["body"]
    assert "one incident for this failure" in body
    assert '"supervisor_recoveries": {"limit": 2, "remaining": 2, "used": 0}' in body


async def test_failed_task_without_a_worker_attempt_gets_one_triage_incident(env):
    """The unfiltered task.failed subscriber must also cover pre-claim failures."""
    await env.db.transition_task("t", TaskStatus.FAILED, force=True, context="validation_failed")

    first = await env.handler.execute(
        "task_failure_triage_notify", {"task_id": "t", "project_id": "p"}
    )
    replay = await env.handler.execute(
        "task_failure_triage_notify", {"task_id": "t", "project_id": "p"}
    )
    await env.db.queue_task_recovery_notifications()

    assert first["outcome"] == "queued"
    assert replay == {
        "outcome": "existing",
        "task_id": "t",
        "incident_id": first["incident_id"],
        "redelivered": False,
    }
    current = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    assert current["reason"] == "terminal_failed"
    assert current["attempt_id"] is None
    assert current["retry_allowed"] is False
    assert len(await queued_messages(env)) == 1


@pytest.mark.parametrize("daemon_authored", [False, True])
async def test_failed_without_attempt_hold_preserves_failure_and_archives_notice(env, daemon_authored):
    if daemon_authored:
        await env.db.update_task("t", task_type="promotion", created_by_kind="promotion")
    await env.db.transition_task("t", TaskStatus.FAILED, force=True)
    first = await notify(env)
    current = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    before = asdict(await env.db.get_task("t"))

    assert "error" in await decide(env, current)  # No attempt can prove a safe retry.
    result = await decide(env, current, "hold")
    assert "error" not in result, result
    assert result["status"] == "FAILED"
    assert asdict(await env.db.get_task("t")) == before
    held = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    assert held["decision"] == "hold" and held["retry_allowed"] is False
    assert await env.db.get_task_meta("t", "supervisor_recovery_attempts") is None
    assert (await env.db.get_message("msg-" + first["incident_id"])).archived_at is not None
    comments = (await env.db.list_task_comments("t"))["comments"]
    assert len(comments) == 1 and "no worker attempt" in comments[0]["body"]
    assert "error" in await decide(env, current, "hold")
    assert await env.db.queue_task_recovery_notifications() == 0
    assert len(await queued_messages(env)) == 1


@pytest.mark.parametrize("command", ["restart_task", "reopen_with_feedback"])
@pytest.mark.parametrize("failure_source", ["created", "transitioned"])
async def test_restart_supersedes_no_attempt_recovery_hold(env, command, failure_source):
    """swift-impact-20: a pre-claim failure must not hold the fresh attempt."""
    task_id = "preclaim"
    await env.db.create_task(Task(
        id=task_id, project_id="p", title="Pre-claim failure", description="Keep requirements",
        status=TaskStatus.FAILED if failure_source == "created" else TaskStatus.READY,
        profile_id="worker", route_source="override", branch_name="aq/preclaim", retry_count=2,
    ))
    if failure_source == "transitioned":
        await env.db.transition_task(task_id, TaskStatus.FAILED, context="validation_failed")
    first = await env.handler.execute("task_recovery_notify", {"task_id": task_id})
    current = await env.db.get_task_meta(task_id, "supervisor_recovery_incident")
    assert first["outcome"] == "queued" and current["attempt_id"] is None
    recovery_args = {
        "task_id": task_id, "incident_id": current["id"], "decision": "hold",
        "reason": "Preserve pre-claim failure for diagnosis.",
    }
    held = await env.handler.execute("task_recover", recovery_args)
    assert "error" not in held, held
    recorded = await env.db.get_task_meta(task_id, "supervisor_recovery_incident")
    assert recorded["decision"] == "hold"
    before = await env.db.get_task(task_id)
    await env.db.set_task_meta(task_id, "supervisor_recovery_attempts", 1)
    ready = []
    env.db.set_ready_listener(lambda entries: record_ready(ready, entries))

    result = await env.handler.execute(command, {"task_id": task_id, "feedback": "Fix diagnosed"})

    assert "error" not in result, result
    after = await env.db.get_task(task_id)
    assert after.status is TaskStatus.READY and after.retry_count == 0
    assert after.claim_epoch == before.claim_epoch
    assert after.branch_name == before.branch_name
    assert await env.db.claim_frontier_exclusions(task_id) == []
    assert await env.db.count_ready_by_profile("p") == {"worker": 1}
    assert (task_id, "restarted") in ready
    superseded = await env.db.get_task_meta(task_id, "supervisor_recovery_incident")
    assert superseded["id"] == current["id"] and superseded["attempt_id"] is None
    assert superseded["decision"] == "superseded"
    assert superseded["decided_by"] == f"system:{command}"
    assert superseded["decision_history"][-1] == {
        key: recorded[key]
        for key in ("decision", "decision_reason", "decided_at", "decided_by")
    }
    assert await env.db.get_task_meta(task_id, "supervisor_recovery_attempts") == 1
    assert (await env.db.get_message("msg-" + current["id"])).archived_at is not None
    stale = await env.handler.execute("task_recover", recovery_args)
    assert "error" in stale, stale
    await env.db.queue_task_recovery_notifications()
    assert await env.db.get_task_meta(task_id, "supervisor_recovery_incident") == superseded
    assert len(await queued_messages(env)) == 1


@pytest.mark.parametrize("held", [False, True])
@pytest.mark.parametrize("edit", ["comment", "description", "reconciliation"])
async def test_no_attempt_incident_survives_ordinary_task_updates(env, held, edit):
    await env.db.transition_task("t", TaskStatus.FAILED, force=True)
    first = await notify(env)
    current = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    if held:
        assert "error" not in await decide(env, current, "hold")
    before = await env.db.get_task("t")
    if edit == "comment":
        await env.db.add_task_comment(
            "t", "Diagnosis: preserve the aborted promotion.",
            author_kind="supervisor", author_id="supervisor-p",
        )
    elif edit == "description":
        await env.db.update_task_description("t", "Keep requirements plus diagnosis")
    else:
        await env.db.transition_task(
            "t", TaskStatus.FAILED, force=True, context="integration_reconciliation",
            pr_url="https://github.com/test/repo/pull/1403",
        )
    assert (await env.db.get_task("t")).updated_at != before.updated_at

    assert await env.db.queue_task_recovery_notifications() == 0
    replay = await notify(env)
    assert replay["outcome"] == "existing" and replay["incident_id"] == first["incident_id"]
    assert replay["redelivered"] is False
    assert len(await queued_messages(env)) == 1
    recorded = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    assert recorded["id"] == current["id"]
    assert recorded["decision"] == ("hold" if held else None)
    if not held:
        assert "error" not in await decide(env, current, "hold")


@pytest.mark.parametrize("write", ["transition", "update"])
async def test_new_no_attempt_failure_rejects_old_hold_before_scan(env, write):
    await env.db.transition_task("t", TaskStatus.FAILED, force=True)
    await notify(env)
    old = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    for status in (TaskStatus.READY, TaskStatus.FAILED):
        if write == "transition":
            await env.db.transition_task("t", status, force=True)
        else:
            await env.db.update_task("t", status=status)
    result = await decide(env, old, "hold")
    assert "incident is stale" in result["error"]
    assert (await env.db.get_task_meta("t", "supervisor_recovery_incident"))["decision"] is None
    assert await env.db.queue_task_recovery_notifications() == 1
    new = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    assert new["id"] != old["id"]
    assert (await env.db.get_message("msg-" + old["id"])).archived_at is not None
    assert "error" not in await decide(env, new, "hold")


@pytest.mark.parametrize("change", ["new_stopped_attempt", "new_live_attempt", "claim_epoch"])
async def test_hold_rejects_genuine_execution_changes_before_scan(env, change):
    await env.db.transition_task("t", TaskStatus.FAILED, force=True)
    await notify(env)
    old = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    if change == "claim_epoch":
        await env.db.update_task("t", claim_epoch=1)
    elif change == "new_stopped_attempt":
        await stopped_attempt(env, sid="new", started=9000, attention=False)
    else:
        await env.db.create_session(SessionRecord(
            id="new", name="new", task_id="t", project_id="p", profile_id="worker",
            harness="fake", provider="fake", lifecycle="task", state="running",
            desired_state="running", epoch="test", instance_token="new",
            work_dir="/never-used", started_at=9000,
        ))
    result = await decide(env, old, "hold")
    assert "incident is stale" in result["error"]
    assert not (await env.db.list_task_comments("t"))["comments"]
    assert (await env.db.get_task_meta("t", "supervisor_recovery_incident"))["decision"] is None


async def test_legacy_failed_row_without_generation_keeps_one_incident_after_comment(env):
    # An installed row from before failure generations existed.
    async with env.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "t").values(status="FAILED"))
    first = await notify(env)
    await env.db.add_task_comment(
        "t", "Legacy failure diagnosis", author_kind="supervisor", author_id="supervisor-p",
    )
    assert await env.db.queue_task_recovery_notifications() == 0
    assert (await notify(env))["incident_id"] == first["incident_id"]
    assert len(await queued_messages(env)) == 1


async def test_remaining_budget_counts_down_across_recoveries(env):
    current = await incident(env)
    assert "error" not in await decide(env, current)
    await env.db.transition_task("t", TaskStatus.BLOCKED, force=True)
    following = await incident(env, sid="second", started=9000)
    assert following["id"] != current["id"]
    assert following["budget"] == {
        "worker_retries": {"used": 1, "limit": 3, "remaining": 2},
        "supervisor_recoveries": {"used": 1, "limit": 2, "remaining": 1},
    }


async def test_terminal_close_leg_is_one_incident_that_needs_operator_review(env):
    await stopped_attempt(env, reason="closed", attention=False)
    # The close leg enters BLOCKED from a running state; a same-status write
    # records no terminal context.
    await env.db.transition_task("t", TaskStatus.IN_PROGRESS, force=True, context="claim")
    await env.db.transition_task(
        "t", TaskStatus.BLOCKED, force=True, context="session_close_hard_failure"
    )
    assert await env.db.get_task_meta("t", "blocked_terminal") == "session_close_hard_failure"
    assert (await notify(env))["outcome"] == "queued"
    await env.db.queue_task_recovery_notifications()
    assert len(await queued_messages(env)) == 1
    current = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    assert current["reason"] == "session_close_hard_failure"
    assert current["retry_allowed"] is False
    assert current["next_action"].startswith("Not an automatically retryable failure")
    assert "error" in await decide(env, current)
    assert (await env.db.get_task("t")).status == TaskStatus.BLOCKED
    assert "error" not in await decide(env, current, "hold")


async def test_operator_stop_is_a_decision_not_an_incident(env):
    await stopped_attempt(env, reason="manual_stop", attention=False)
    await env.db.transition_task("t", TaskStatus.IN_PROGRESS, force=True, context="claim")
    await env.db.transition_task("t", TaskStatus.BLOCKED, force=True, context="stop_task")
    assert await env.db.get_task_meta("t", "blocked_terminal") == "stop_task"
    assert (await notify(env))["outcome"] == "not_actionable"
    await env.db.queue_task_recovery_notifications()
    assert not await queued_messages(env)


async def released_handoff(env, *, base_sha="a" * 40, preserved_sha="b" * 40):
    """The durable state left by a stopped pool writer and operator release-owner."""
    await env.db.create_repo(RepoConfig(id="r", project_id="p", source_type=RepoSourceType.CLONE))
    await env.db.create_profile(AgentProfile(id="codex-worker", name="Codex"))
    await env.db.update_task("t", repo_id="r")
    await stopped_attempt(env, reason="productive_death")
    await env.db.update_session("s", lifecycle="pool", task_id=None)
    await env.db.set_task_meta("t", "needs_attention", "session_not_live")
    await env.db.queue_task_recovery_notifications()
    current = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    async with env.db.immediate() as conn:
        await conn.execute(
            update(projects)
            .where(projects.c.id == "p")
            .values(
                hierarchical_integration_mode="train",
                integration_repository_id="r",
            )
        )
        await conn.execute(
            insert(task_branch_origins).values(
                id="origin",
                task_id="t",
                repository_id="r",
                branch_name="aq/keep",
                base_sha=base_sha,
                creation_generation=0,
                reserved=True,
                materialized=True,
                created_at=1,
            )
        )
        await conn.execute(
            insert(task_integration_checkpoints).values(
                task_id="t",
                repository_id="r",
                branch="aq/keep",
                checkpoint_sha=base_sha,
                updated_at=1,
            )
        )
        await conn.execute(
            insert(integration_branch_owners).values(
                id="owner",
                repository_id="r",
                ref="aq/keep",
                owner_id="t",
                owner_role="worker",
                handoff_state="released",
                fence_token=2,
                created_at=1,
                updated_at=8000,
            )
        )
        await conn.execute(
            insert(integration_owner_recoveries).values(
                id="release",
                owner_row_id="owner",
                repository_id="r",
                ref="aq/keep",
                task_id="t",
                outcome="preserved_and_released",
                principal="human:local-operator",
                created_at=8000,
                evidence={
                    "released_fence_token": 2,
                    "claim_released": True,
                    "workspace_unlocked": True,
                    "preserved_ref": "aq/preserved/owner",
                    "preserved_sha": preserved_sha,
                "stop_proof": {
                    "session_id": "s",
                    "instance_token": "s",
                    "name": "s",
                    "provider": "fake",
                        "desired_state": "stopped",
                        "confirmed_at": 8000,
                    },
                },
            )
        )
    await env.db.update_task(
        "t",
        profile_id="codex-worker",
        intelligence_class="standard-high",
        route_source="override",
        provider_intent="pinned",
        route={
            "profile_id": "codex-worker",
            "intelligence_class": "standard-high",
            "override": {
                "by": "local operator",
                "at": 8001,
                "reason": "Continue preserved work on the operator selected provider",
            },
        },
    )
    return current


async def test_resume_operator_released_reroute_consumes_budget_and_preserves_proof_once(env):
    current = await released_handoff(env)
    assert current["retry_allowed"] is False
    assert "Session exit" in current["retry_blocker"]
    assert "error" in await decide(env, current)  # Generic recovery stays fenced.
    results = await asyncio.gather(
        *(env.handler.execute("resume_task", {"task_id": "t"}) for _ in range(2))
    )
    assert sum("error" not in r for r in results) == 1, results
    task = await env.db.get_task("t")
    assert task.status == TaskStatus.READY and task.retry_count == 1
    assert (task.profile_id, task.intelligence_class) == ("codex-worker", "standard-high")
    assert task.branch_name == "aq/keep" and task.description == "Keep requirements"
    assert await env.db.get_task_meta("t", "supervisor_recovery_attempts") == 1
    assert await env.db.get_task_meta("t", "needs_attention") is None
    saved = await env.db.get_task_meta("t", "supervisor_recovery_checkpoint")
    assert saved["sha"] == "b" * 40 and saved["ref"] == "aq/preserved/owner"
    assert saved["release_id"] == "release" and saved["released_fence_token"] == 2
    decided = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    assert decided["operator_handoff"] == saved
    assert decided["routing"] == current["routing"]  # Never rewrite the incident snapshot.
    comments = await env.handler.execute("task_comments", {"task_id": "t"})
    assert "Operator handoff evidence" in comments["comments"][0]["body"]


def _handoff_git(path, *args):
    return subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
        cwd=path, check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.mark.parametrize("guard", [None, "stale_fence", "moved_preserved_ref"])
async def test_audited_operator_handoff_reconciles_saved_checkpoint_in_real_slot(
    env, tmp_path, guard
):
    from src.git.manager import GitError
    from src.integration.models import Fence
    from src.integration.ownership import StaleFence
    from src.orchestrator.task_checkpoint import CHECKPOINT_META, capture_checkpoint

    remote = tmp_path / "origin.git"
    _handoff_git(tmp_path, "init", "--bare", "--initial-branch=main", str(remote))
    base = tmp_path / "base"
    _handoff_git(tmp_path, "clone", str(remote), str(base))
    (base / "README.md").write_text("original\n")
    _handoff_git(base, "add", ".")
    _handoff_git(base, "commit", "-m", "initial")
    _handoff_git(base, "push", "origin", "main")
    original = _handoff_git(base, "rev-parse", "HEAD")
    kind = WorkspaceKind(project_id="p", id="project-repo", mode=KIND_MODE_WORKTREE)
    base_ws = Workspace(
        id="base", project_id="p", workspace_path=str(base),
        source_type=RepoSourceType.CLONE, kind_id=kind.id,
    )
    await env.db.create_workspace(base_ws)
    mgr = env.orch._worktree_slots()
    slot = await mgr.create_slot(base_ws, kind, 0)
    path = tmp_path / "base" / ".aq" / "worktrees" / "slot-0"
    task = await env.db.get_task("t")
    await mgr.reset_slot_for_task(slot, task, base_branch=original, target_branch="aq/keep")
    _handoff_git(path, "push", "origin", "aq/keep")
    (path / "saved.txt").write_text("staged checkpoint work\n")
    _handoff_git(path, "add", "saved.txt")
    (path / "saved.txt").write_text("unstaged checkpoint work\n")
    await capture_checkpoint(env.db, env.orch.git, "t", str(path))
    saved = await env.db.get_task_meta("t", CHECKPOINT_META)
    _handoff_git(path, "reset", "--hard")
    _handoff_git(path, "clean", "-fd", "-e", ".aq-worktree.json")
    _handoff_git(path, "switch", "--detach")
    (path / "preserved.txt").write_text("audited preserved work\n")
    _handoff_git(path, "add", "preserved.txt")
    _handoff_git(path, "commit", "-m", "preserved")
    preserved = _handoff_git(path, "rev-parse", "HEAD")
    _handoff_git(path, "push", "origin", "HEAD:refs/heads/aq/preserved/owner")
    # Published continuation after the release also wins over the older snapshot.
    (path / "published.txt").write_text("newer canonical work\n")
    _handoff_git(path, "add", "published.txt")
    _handoff_git(path, "commit", "-m", "published descendant")
    published = _handoff_git(path, "rev-parse", "HEAD")
    _handoff_git(path, "push", "origin", "HEAD:refs/heads/aq/keep")
    await released_handoff(env, base_sha=original, preserved_sha=preserved)
    result = await env.handler.execute("resume_task", {"task_id": "t"})
    assert "error" not in result, result
    project = await env.db.get_project("p")
    task = await env.db.get_task("t")
    origin, fence, _ = await env.orch._hierarchy_origin_and_fence(task, project)
    assert origin["operator_handoff"]["sha"] == preserved
    attachment = SimpleNamespace(workspace=slot, kind=kind)
    if guard == "stale_fence":
        fence = Fence(target=fence.target, owner_id=fence.owner_id, token=fence.token - 1)
    elif guard == "moved_preserved_ref":
        _handoff_git(path, "push", "origin", "HEAD:refs/heads/aq/preserved/owner")
    if guard:
        with pytest.raises(StaleFence if guard == "stale_fence" else GitError):
            await env.orch._prepare_exact_origin_workspace(task, project, attachment, origin, fence)
        assert _handoff_git(path, "branch", "--show-current") == ""
        assert _handoff_git(path, "status", "--porcelain") == ""
    else:
        branch = await env.orch._prepare_exact_origin_workspace(
            task, project, attachment, origin, fence
        )
        assert branch == "aq/keep"
        assert _handoff_git(path, "branch", "--show-current") == branch
        assert (path / "preserved.txt").read_text() == "audited preserved work\n"
        assert (path / "published.txt").read_text() == "newer canonical work\n"
        assert _handoff_git(path, "show", ":saved.txt") == "staged checkpoint work"
        assert (path / "saved.txt").read_text() == "unstaged checkpoint work\n"
    assert _handoff_git(path, "rev-parse", "HEAD") == published
    assert await env.db.get_task_meta("t", CHECKPOINT_META) == saved
    assert _handoff_git(path, "rev-parse", saved["ref"]) == saved["commit"]


@pytest.mark.parametrize(
    "guard",
    [
        "missing_audit",
        "sweep",
        "stale_fence",
        "wrong_instance",
        "wrong_name",
        "wrong_provider",
        "unreleased_claim",
        "old_override",
        "changed_affinity",
        "retry_budget",
        "recovery_budget",
        "project_budget",
        "gate",
        "hold_label",
        "live_worker",
        "recovery_hold",
        "foreign_supervisor",
        "worker",
    ],
)
async def test_operator_handoff_resume_retains_all_proof_authority_and_budget_guards(env, guard):
    current = await released_handoff(env)
    args = {"task_id": "t"}
    if guard in {
        "missing_audit", "sweep", "stale_fence", "wrong_instance", "wrong_name",
        "wrong_provider", "unreleased_claim",
    }:
        async with env.db.immediate() as conn:
            if guard == "missing_audit":
                await conn.execute(delete(integration_owner_recoveries))
            elif guard == "sweep":
                await conn.execute(update(integration_owner_recoveries).values(principal="sweep"))
            elif guard == "stale_fence":
                await conn.execute(update(integration_branch_owners).values(fence_token=3))
            else:
                audit = (await conn.execute(select(integration_owner_recoveries))).mappings().one()
                proof = dict(audit["evidence"])
                if guard == "wrong_instance":
                    proof["stop_proof"] = {**proof["stop_proof"], "instance_token": "reused"}
                elif guard in {"wrong_name", "wrong_provider"}:
                    proof["stop_proof"] = {
                        **proof["stop_proof"], guard.removeprefix("wrong_"): "another-worker",
                    }
                else:
                    proof["claim_released"] = False
                await conn.execute(update(integration_owner_recoveries).values(evidence=proof))
    elif guard == "old_override":
        route = (await env.db.get_task("t")).route
        route["override"]["at"] = 7999
        await env.db.update_task("t", route=route)
    elif guard == "changed_affinity":
        await env.db.create_agent(Agent(id="other-agent", name="Other", profile_id="worker"))
        await env.db.update_task("t", affinity_agent_id="other-agent")
    elif guard == "retry_budget":
        await env.db.update_task("t", retry_count=3)
    elif guard == "recovery_budget":
        await env.db.set_task_meta("t", "supervisor_recovery_attempts", 2)
    elif guard == "project_budget":
        await env.db.update_project("p", budget_limit=1, total_tokens_used=1)
    elif guard == "gate":
        await env.db.create_gate(
            project_id="p", gate_type="human", title="Review", waiter_task_ids=["t"]
        )
    elif guard == "hold_label":
        await env.db.add_task_label("t", "hold:operator")
    elif guard == "live_worker":
        env.orch.session_providers.create = lambda *_: SimpleNamespace(
            confirm_stopped=AsyncMock(return_value=False)
        )
    elif guard == "recovery_hold":
        assert "error" not in await decide(env, current, "hold")
    else:
        args["_scope"] = asdict(
            RequestScope(
                kind="session",
                session_id="untrusted",
                project_id="other" if guard == "foreign_supervisor" else "p",
                elevated=guard == "foreign_supervisor",
            )
        )
    before = await env.db.get_task("t")
    result = await env.handler.execute("resume_task", args)
    assert "error" in result, result
    after = await env.db.get_task("t")
    assert after.status == before.status and after.retry_count == before.retry_count
    assert await env.db.get_task_meta("t", "supervisor_recovery_checkpoint") is None


async def test_existing_incident_refresh_reports_changed_route_as_not_retryable(env):
    current = await incident(env)
    assert current["retry_allowed"] is True
    await env.db.update_task("t", intelligence_class="standard-high")
    await notify(env)
    refreshed = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    assert refreshed["id"] == current["id"]
    assert refreshed["retry_allowed"] is False
    assert "routing changed" in refreshed["retry_blocker"]
    message = await env.db.get_message("msg-" + current["id"])
    assert '"retry_allowed": false' in message.body
    assert "routing changed" in message.body


async def supervisor_handoff_hold(env, current):
    await env.db.create_profile(AgentProfile(id="supervisor", name="Supervisor"))
    await env.db.create_session(
        SessionRecord(
            id="supervisor",
            project_id="p",
            profile_id="supervisor",
            harness="fake",
            provider="fake",
            name="n-supervisor--p",
            lifecycle="named",
            state="running",
            desired_state="running",
            epoch="e",
            instance_token="supervisor",
            work_dir="/never-used",
            started_at=9000,
        )
    )
    scope = asdict(
        RequestScope(kind="session", session_id="supervisor", project_id="p", elevated=True)
    )
    assert "error" not in await decide(env, current, "hold", _scope=scope)
    held = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    return held, scope


async def test_operator_handoff_explicit_supervisor_hold_release_is_fenced_and_audited(env):
    current = await released_handoff(env)
    held, scope = await supervisor_handoff_hold(env, current)
    assert "error" in await env.handler.execute("resume_task", {"task_id": "t"})
    assert "error" in await decide(
        env, current, expected_hold_at=held["decided_at"] - 1, _scope=scope
    )
    results = await asyncio.gather(
        *(decide(env, current, expected_hold_at=held["decided_at"], _scope=scope) for _ in range(2))
    )
    assert sum("error" not in result for result in results) == 1, results
    task = await env.db.get_task("t")
    assert task.status == TaskStatus.READY and task.retry_count == 1
    assert task.profile_id == "codex-worker" and task.branch_name == "aq/keep"
    recorded = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    assert recorded["decision_history"] == [
        {key: held[key] for key in ("decision", "decision_reason", "decided_at", "decided_by")}
    ]
    assert recorded["decided_by"] == "supervisor session:supervisor"
    assert recorded["operator_handoff"]["release_id"] == "release"
    assert recorded["retry_allowed"] is False
    comments = await env.handler.execute("task_comments", {"task_id": "t"})
    assert "Cleared recovery hold" in comments["comments"][0]["body"]
    assert "Operator handoff evidence" in comments["comments"][0]["body"]


@pytest.mark.parametrize("missing_audit", [False, True])
async def test_operator_release_retains_audited_producer_handoff_proof(env, missing_audit):
    current = await released_handoff(env)
    held, _ = await supervisor_handoff_hold(env, current)
    if missing_audit:
        async with env.db.immediate() as conn:
            await conn.execute(delete(integration_owner_recoveries))
    result = await decide(env, current, operator_release=True, expected_hold_at=held["decided_at"])
    assert ("error" not in result) is not missing_audit, result
    task = await env.db.get_task("t")
    assert task.status == (TaskStatus.BLOCKED if missing_audit else TaskStatus.READY)


@pytest.mark.parametrize(
    "guard",
    ["manual_hold", "human_hold", "gate", "hold_label", "budget", "live_worker", "stale_fence"],
)
async def test_operator_handoff_hold_release_preserves_human_holds_and_rechecks_guards(env, guard):
    current = await released_handoff(env)
    held, scope = await supervisor_handoff_hold(env, current)
    if guard == "manual_hold":
        recorded = {**held, "decided_by": "operator"}
        await env.db.set_task_meta("t", "supervisor_recovery_incident", recorded)
    elif guard == "human_hold":
        async with env.db.immediate() as conn:
            from src.database.tables import task_comments

            await conn.execute(update(task_comments).values(author_kind="user"))
    elif guard == "gate":
        await env.db.create_gate(
            project_id="p", gate_type="human", title="Review", waiter_task_ids=["t"]
        )
    elif guard == "hold_label":
        await env.db.add_task_label("t", "hold:human")
    elif guard == "budget":
        await env.db.update_task("t", retry_count=3)
    elif guard == "live_worker":
        env.orch.session_providers.create = lambda *_: SimpleNamespace(
            confirm_stopped=AsyncMock(return_value=False)
        )
    else:
        async with env.db.immediate() as conn:
            await conn.execute(update(integration_branch_owners).values(fence_token=3))
    result = await decide(env, current, expected_hold_at=held["decided_at"], _scope=scope)
    assert "error" in result, result
    task = await env.db.get_task("t")
    assert task.status == TaskStatus.BLOCKED
    recorded = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    assert recorded["decision"] == "hold" and not recorded.get("decision_history")
    assert await env.db.get_task_meta("t", "supervisor_recovery_checkpoint") is None


async def test_event_before_the_attempt_stops_defers_to_the_scan(env):
    await env.db.create_session(
        SessionRecord(
            id="live",
            task_id="t",
            project_id="p",
            profile_id="worker",
            harness="fake",
            provider="fake",
            name="live",
            lifecycle="task",
            state="running",
            desired_state="running",
            epoch="test",
            instance_token="live",
            work_dir="/never-used",
            started_at=200,
        )
    )
    await env.db.set_task_meta("t", "needs_attention", "stuck_timeout")
    early = await notify(env)
    assert early["outcome"] == "not_actionable"
    assert "scan" in early["detail"]
    assert not await queued_messages(env)


async def test_incident_hook_is_bound_to_the_event_project_and_operators(env):
    await stopped_attempt(env)
    assert "not found" in (await notify(env, project_id="other"))["error"]
    worker = asdict(RequestScope(kind="session", session_id="x", project_id="p"))
    assert "scope" in (await notify(env, _scope=worker))["error"].lower()
    assert not await queued_messages(env)


async def test_user_message_nudge_leaves_the_reply_protocol_to_the_supervisor_profile():
    from pathlib import Path

    from src.messages.delivery import _render_nudge
    from src.models import Message

    user = Message(
        id="user-message",
        project_id=None,
        from_kind="user",
        from_id="user",
        to_kind="session",
        to_id="supervisor-global",
        body="question",
    )
    # A nudge is a one-line pointer to the durable body and asks no message
    # for a reply; which messages get an `aq reply` is the profile's to teach.
    assert _render_nudge([user]) == "Handle `aq message status user-message --json` (running it marks the message read)."
    profile = Path(__file__).parents[1] / "src/profiles/defaults/supervisor/profile.md"
    text = " ".join(profile.read_text().split())
    assert 'Answer user messages with `aq reply <msg-id> "…"`' in text
    assert "Internal recovery notices need a recovery decision, not an `aq reply`" in text


async def test_ask_recovery_crash_after_action_reconciles_without_second_retry(env, monkeypatch):
    from src.asks.models import SourceIdentity
    current = await incident(env)
    ask = await env.handler._publish_source_ask(SourceIdentity("p", "recovery", current["id"], {"task_id": "t"}))
    args = {"ask_id": ask["id"], "expected": {k: ask[k] for k in (
        "decision_generation", "source_fingerprint", "feedback_version")},
        "option_key": "retry", "overall_note": "Preserve evidence and retry", "idempotency_key": "recovery-crash"}
    preview = await env.handler._cmd_ask_prepare_decision(args)
    assert preview["success"], preview
    recover = env.handler._cmd_task_recover
    async def lost_ack(*args, **kwargs):
        await recover(*args, **kwargs)
        raise RuntimeError("Lost application acknowledgement")
    monkeypatch.setattr(env.handler, "_cmd_task_recover", lost_ack)
    result = await env.handler._cmd_ask_decide({**args, "preview_id": preview["preview"]["id"]})
    assert result["application_state"] == "unknown", result
    retry_count = (await env.db.get_task("t")).retry_count
    assert retry_count == 1
    replay = await env.handler._cmd_ask_retry_operation({"decision_id": result["decision_id"]})
    assert replay["success"], replay
    assert (await env.db.get_task("t")).retry_count == retry_count
    assert (await env.db.get_task_meta("t", "supervisor_recovery_incident"))["ask_decision_id"] == result["decision_id"]
