"""Durable incident delivery and bounded, claim-fenced supervisor decisions."""

import asyncio
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
    integration_owner_recoveries,
    messages,
    projects,
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
        create=lambda *_: SimpleNamespace(confirm_stopped=AsyncMock(return_value=True))
    )
    yield SimpleNamespace(db=db, orch=orch, config=config, handler=CommandHandler(orch, config))
    await db.close()


async def incident(env, sid="s", started=200, reason="stuck_timeout"):
    await stopped_attempt(env, sid=sid, started=started, reason=reason)
    await env.db.queue_task_recovery_notifications()
    return await env.db.get_task_meta("t", "supervisor_recovery_incident")


async def stopped_attempt(env, sid="s", started=200, reason="stuck_timeout", attention=True):
    await env.db.create_session(
        SessionRecord(
            id=sid,
            task_id="t",
            project_id="p",
            profile_id="worker",
            harness="fake",
            provider="fake",
            name=sid,
            lifecycle="task",
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
            "text": f"Decision: {decision}",
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
    return await env.handler.execute(
        "escalation_apply_reply",
        {
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
        },
    )


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


async def test_retry_preserves_work_and_routing_records_comment_and_consumes_budget_once(env):
    current = await incident(env)
    results = await asyncio.gather(decide(env, current), decide(env, current))
    assert sum("error" not in r for r in results) == 1, results
    task = await env.db.get_task("t")
    assert task.status == TaskStatus.READY
    assert task.retry_count == 1
    assert task.profile_id == "worker" and task.intelligence_class == "deep-high"
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
    assert released["decision_history"] == [{
        key: held[key]
        for key in ("decision", "decision_reason", "decided_at", "decided_by")
    }]
    comments = await env.handler.execute("task_comments", {"task_id": "t"})
    assert len(comments["comments"]) == 2
    assert "Cleared recovery hold" in comments["comments"][0]["body"]


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


@pytest.mark.parametrize("decision,expected_status", [("retry", "READY"), ("hold", "BLOCKED")])
async def test_human_evidence_drives_guarded_recovery_or_keep_blocked(env, decision, expected_status):
    current = await incident(env)
    result = await apply_human_recovery_reply(env, current, decision)
    assert result["success"] is True
    assert result["escalation"]["state"] == "resolved"
    assert result["action_result"]["status"] == expected_status
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
    assert mixed == "Handle `aq message status internal --json`."
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
    assert _render_nudge([user]) == "Handle `aq message status user-message --json`."
    profile = Path(__file__).parents[1] / "src/profiles/defaults/supervisor/profile.md"
    text = " ".join(profile.read_text().split())
    assert 'Answer user messages with `aq reply <msg-id> "…"`' in text
    assert "Internal recovery notices need a recovery decision, not an `aq reply`" in text
