"""Operator task controls: persistence, race fences and existing work-state safety."""

import json
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from src.api.auth import RequestScope
from src.commands.handler import CommandHandler
from src.config import AppConfig, DatabaseConfig
from src.models import Agent, AgentProfile, Project, Task, TaskStatus
from src.orchestrator import Orchestrator
from src.sessions import proctable
from tests.assignment_routing_helpers import install_already_routed
from tests.pg_dsn import ensure_worker_postgres_dsn
from tests.db_fixtures import lease_dsn

pytestmark = pytest.mark.asyncio
POSTGRES_TEST_DSN = ensure_worker_postgres_dsn()


@pytest.fixture
async def env(tmp_path, request, reuse_database):
    # The fake provider has an isolated process inventory, just as its sessions
    # and repositories are isolated. Exercise the real scanner, not a constant
    # quiescence result. Tests register real children via /proc symlinks below;
    # inaccessible fixture processes must still retain the workspace.
    process_inventory = tmp_path / "proc"
    process_inventory.mkdir()
    db = await reuse_database("controls.db")
    for pid in ("p", "other"):
        await db.create_project(Project(id=pid, name=pid))
    await db.create_profile(AgentProfile(id="worker", name="Worker", needs_workspace=False))
    await db.create_agent(Agent(id="agent", name="Worker", profile_id="worker"))
    for tid in ("t", "peer"):
        await db.create_task(
            Task(
                id=tid,
                project_id="p",
                title=tid,
                description="Original requirements",
                status=TaskStatus.READY,
                profile_id="worker", route_source="legacy",
                retry_count=2,
                branch_name="preserved",
                pr_url="https://example.invalid/pr/1",
            )
        )
    config = AppConfig(database=DatabaseConfig(url=lease_dsn("controls.db")), data_dir=str(tmp_path / "data"), workspace_dir=str(tmp_path / "ws"))
    orch = Orchestrator(config)
    orch.db = db
    install_already_routed(orch)
    # Restart tests undo their injected failures mid-test. Their monkeypatch
    # must not also undo this fake provider's process inventory.
    with pytest.MonkeyPatch.context() as process_patch:
        process_patch.setattr(proctable, "_PROC", process_inventory)
        yield SimpleNamespace(
            db=db, orch=orch, handler=CommandHandler(orch, config), config=config,
            process_inventory=process_inventory,
        )


async def command(env, name, task_id="t", **args):
    return await env.handler.execute(name, {"task_id": task_id, **args})


async def pause(env):
    result = await command(env, "pause_task")
    assert "error" not in result, result
    assert (await env.db.get_task("t")).status == TaskStatus.PAUSED
    return result


async def test_ready_pause_persists_reload_without_affecting_peer_or_artifacts(env):
    await pause(env)
    first = await env.db.get_task("t")
    assert first.resume_after is None
    assert first.retry_count == 2
    assert first.profile_id == "worker"
    assert first.branch_name == "preserved"
    assert first.pr_url == "https://example.invalid/pr/1"
    await pause(env)
    reloaded = Orchestrator(env.config)
    reloaded.db = env.db
    await reloaded._resume_paused_tasks()
    assert (await env.db.get_task("t")).status == TaskStatus.PAUSED
    assert (await env.db.get_task("peer")).status == TaskStatus.READY
    assert await env.db.assign_task_to_agent("t", "agent") is False
    assert (await env.db.get_agent("agent")).current_task_id is None


@pytest.mark.parametrize(
    "prior",
    [
        TaskStatus.DEFINED,
        TaskStatus.READY,
        TaskStatus.BLOCKED,
        TaskStatus.WAITING_INPUT,
    ],
)
async def test_resume_restores_state_without_approving_or_resolving_gate(env, prior):
    await env.db.transition_task("t", prior, force=True)
    gate_id, _ = await env.db.create_gate(
        project_id="p", gate_type="human", title="Human review", waiter_task_ids=["t"]
    )
    await pause(env)
    result = await command(env, "resume_task")
    assert "error" not in result, result
    task = await env.db.get_task("t")
    assert task.status == prior
    assert task.is_blocked
    assert task.retry_count == 2
    assert (await env.db.get_gate(gate_id))["status"] == "open"
    assert await env.db.assign_task_to_agent("t", "agent") is False


@pytest.mark.parametrize(
    "name,extra",
    [
        ("restart_task", {}),
        ("set_task_status", {"status": "READY"}),
        ("edit_task", {"status": "READY", "description": "Must not write"}),
        ("task_set", {"meta": {"manual_pause": None}, "description": "Must not write"}),
    ],
)
async def test_operator_pause_cannot_be_bypassed_through_other_commands(env, name, extra):
    await pause(env)
    result = await command(env, name, **extra)
    assert "error" in result, result
    task = await env.db.get_task("t")
    assert task.status == TaskStatus.PAUSED
    assert task.description == "Original requirements"
    assert task.retry_count == 2


async def test_comments_and_cas_description_edit_do_not_resume_paused_task(env):
    await pause(env)
    comment = await command(env, "task_comment", body="Please check the edge case.")
    assert "error" not in comment, comment
    edit = await command(
        env, "task_set", description="Revised", expected_description="Original requirements"
    )
    assert "error" not in edit, edit
    conflict = await command(
        env, "task_set", description="Stale", expected_description="Original requirements"
    )
    assert conflict["error_code"] == "description_conflict"
    assert (await env.db.get_task("t")).status == TaskStatus.PAUSED
    assert (await env.db.get_task("t")).description == "Revised"
    history = await command(env, "task_comments")
    assert history["comments"][0]["body"] == "Please check the edge case."


@pytest.mark.parametrize("target", [TaskStatus.READY, TaskStatus.COMPLETED, TaskStatus.PAUSED])
async def test_stale_automatic_transition_cannot_remove_pause_or_bump_retries(env, target):
    await pause(env)
    try:
        await env.db.transition_task(
            "t",
            target,
            force=True,
            context="late_completion",
            retry_count=99,
            resume_after=1,
        )
    except Exception as exc:
        assert "paus" in str(exc).lower()
    task = await env.db.get_task("t")
    assert task.status == TaskStatus.PAUSED
    assert task.retry_count == 2
    assert task.resume_after is None


@pytest.mark.parametrize("status", [TaskStatus.COMPLETED, TaskStatus.FAILED])
async def test_terminal_tasks_are_not_reopened_by_pause(env, status):
    await env.db.transition_task("t", status, force=True)
    result = await command(env, "pause_task")
    assert "error" in result
    assert status.value in result["error"]
    assert (await env.db.get_task("t")).status == status


async def test_missing_and_nonpaused_resume_return_useful_error(env):
    missing = await command(env, "pause_task", "missing")
    assert "not found" in missing["error"]
    result = await command(env, "resume_task")
    assert "not paused" in result["error"].lower()
    assert (await env.db.get_task("t")).status == TaskStatus.READY


@pytest.mark.parametrize("name", ["pause_task", "resume_task"])
async def test_scope_disallows_workers_and_foreign_project_supervisors(env, name):
    for scope in [
        RequestScope(kind="session", session_id="s", project_id="p"),
        RequestScope(kind="session", session_id="s", project_id="other", elevated=True),
    ]:
        result = await command(env, name, _scope=asdict(scope))
        assert "scope" in result.get("error", "").lower(), result
    assert (await env.db.get_task("t")).status == TaskStatus.READY


class FakeProvider:
    name = "fake"

    def __init__(self):
        self.running = {"task-session", "peer-session"}
        self.fail_stop = False

    async def stop(self, handle, *, grace):
        if self.fail_stop:
            raise RuntimeError("provider unavailable")
        assert handle.instance_token == "instance"
        self.running.discard(handle.name)

    async def confirm_stopped(self, handle):
        assert handle.instance_token == "instance"
        return handle.name not in self.running


async def running_session(env, tmp_path, *, lifecycle="pool"):
    import time
    from src.models import AgentState, RepoSourceType, SessionRecord, Workspace

    if (tmp_path / ".git").exists():
        # Git fixtures model preparation onto main; scenarios can override it.
        await env.db.update_task("t", branch_name="main")
    await env.db.assign_task_to_agent("t", "agent")
    await env.db.transition_task("t", TaskStatus.IN_PROGRESS)
    await env.db.update_agent("agent", state=AgentState.BUSY)
    task = await env.db.get_task("t")
    await env.db.create_workspace(
        Workspace(
            id="workspace",
            project_id="p",
            workspace_path=str(tmp_path),
            source_type=RepoSourceType.LINK,
            locked_by_task_id="t",
            locked_by_agent_id="agent",
        )
    )
    await env.db.create_session(
        SessionRecord(
            id="s",
            task_id="t",
            agent_id="agent",
            project_id="p",
            profile_id="worker",
            harness="fake",
            provider="fake",
            name="task-session",
            lifecycle=lifecycle,
            state="running",
            work_dir=str(tmp_path),
            epoch="test",
            instance_token="instance",
            started_at=time.time(),
            claim_phase="active",
            last_claim_epoch=task.claim_epoch,
        )
    )
    provider = FakeProvider()
    env.orch.session_providers = SimpleNamespace(create=lambda *_: provider)
    workspace = await env.db.get_workspace("workspace")
    await env.db.set_task_meta("t", "claim_workspace", {
        "workspace_id": "workspace", "generation": workspace.generation,
        "session_id": "s", "instance_token": "instance", "claim_epoch": task.claim_epoch,
    })
    return provider, task


@pytest.mark.parametrize("lifecycle", ["task", "pool"])
async def test_running_pause_stops_owned_session_and_preserves_work_then_resume(
    env, tmp_path, lifecycle
):
    provider, prior = await running_session(env, tmp_path, lifecycle=lifecycle)
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("keep")
    await pause(env)
    task = await env.db.get_task("t")
    assert "task-session" not in provider.running
    assert "peer-session" in provider.running
    assert task.claim_epoch > prior.claim_epoch
    assert task.assigned_agent_id is None
    assert (await env.db.get_session("s")).state == "stopped"
    assert (await env.db.get_session("s")).task_id == ("t" if lifecycle == "task" else None)
    assert (await env.db.get_agent("agent")).current_task_id is None
    assert (await env.db.get_workspace("workspace")).locked_by_task_id is None
    assert artifact.read_text() == "keep"
    result = await command(env, "resume_task")
    assert "error" not in result, result
    task = await env.db.get_task("t")
    assert task.status == TaskStatus.READY
    assert task.retry_count == 2


async def test_stop_failure_keeps_pause_and_resources_and_resume_retries_cleanup(env, tmp_path):
    provider, _ = await running_session(env, tmp_path)
    provider.fail_stop = True
    result = await command(env, "pause_task")
    assert "error" in result
    assert (await env.db.get_task("t")).status == TaskStatus.PAUSED
    assert (await env.db.get_workspace("workspace")).locked_by_task_id == "t"
    assert (await env.db.get_agent("agent")).current_task_id == "t"
    failed = await command(env, "resume_task")
    assert "error" in failed
    assert (await env.db.get_task("t")).status == TaskStatus.PAUSED
    provider.fail_stop = False
    result = await command(env, "resume_task")
    assert "error" not in result, result
    assert "task-session" not in provider.running
    assert (await env.db.get_task("t")).status == TaskStatus.READY


@pytest.mark.parametrize("session_change", ["reaped", "reused", "stopped"])
async def test_resume_detaches_stale_pause_after_slot_reassignment(env, tmp_path, session_change):
    provider, prior = await running_session(env, tmp_path)
    await env.db.pause_task("t")
    provider.running.discard("task-session")
    gate_id, _ = await env.db.create_gate(
        project_id="p", gate_type="human", title="Still needs approval", waiter_task_ids=["t"],
    )
    await env.db.update_workspace("workspace", locked_by_task_id="peer", generation=8)
    await env.db.update_agent("agent", current_task_id="peer")
    if session_change == "reaped":
        await env.db.delete_session("s")
    elif session_change == "reused":
        await env.db.update_session("s", task_id="peer", instance_token="replacement",
                                    last_claim_epoch=99)
    else:
        await env.db.update_session("s", state="stopped", desired_state="stopped")
    peer_artifact = tmp_path / "peer-work.txt"
    peer_artifact.write_text("keep peer work")
    result = await command(env, "resume_task")
    assert "error" not in result, result
    task = await env.db.get_task("t")
    assert task.status == TaskStatus.READY
    assert task.assigned_agent_id is None
    assert task.branch_name == prior.branch_name
    assert task.repo_id == prior.repo_id
    assert task.retry_count == prior.retry_count
    assert task.is_blocked
    assert (await env.db.get_gate(gate_id))["status"] == "open"
    assert (await env.db.get_workspace("workspace")).locked_by_task_id == "peer"
    assert (await env.db.get_agent("agent")).current_task_id == "peer"
    assert peer_artifact.read_text() == "keep peer work"
    if session_change == "reused":
        assert (await env.db.get_session("s")).task_id == "peer"
        assert (await env.db.get_session("s")).state == "running"
    assert await env.db.get_task_meta("t", "manual_pause") is None
    contexts = await env.db.get_task_contexts("t")
    assert any(ctx["type"] == "manual_pause_recovery" for ctx in contexts)


@pytest.mark.parametrize("stop_proof", [False, None, "exception"])
async def test_stale_pause_never_trusts_stopped_marker_without_process_proof(
    env, tmp_path, monkeypatch, stop_proof,
):
    from unittest.mock import AsyncMock

    provider, _ = await running_session(env, tmp_path)
    await env.db.pause_task("t")
    await env.db.update_workspace("workspace", locked_by_task_id=None, locked_by_agent_id=None)
    await env.db.update_session("s", state="stopped", desired_state="stopped")
    mock = (
        AsyncMock(side_effect=RuntimeError("provider unavailable"))
        if stop_proof == "exception" else AsyncMock(return_value=stop_proof)
    )
    monkeypatch.setattr(provider, "confirm_stopped", mock)
    before = await env.db.get_task_meta("t", "manual_pause")
    result = await command(env, "resume_task", recover_stale_pause=True)
    assert "error" in result, result
    assert (await env.db.get_task("t")).status == TaskStatus.PAUSED
    assert await env.db.get_task_meta("t", "manual_pause") == before


async def test_stale_pause_recovery_rechecks_session_after_provider_probe(
    env, tmp_path, monkeypatch,
):
    provider, _ = await running_session(env, tmp_path)
    await env.db.pause_task("t")
    await env.db.update_workspace("workspace", locked_by_task_id=None, locked_by_agent_id=None)
    await env.db.update_session("s", state="stopped", desired_state="stopped")

    async def race(_):
        await env.db.update_session("s", instance_token="replacement")
        return True

    monkeypatch.setattr(provider, "confirm_stopped", race)
    result = await command(env, "resume_task", recover_stale_pause=True)
    assert "changed during recovery" in result.get("error", ""), result
    assert (await env.db.get_task("t")).status == TaskStatus.PAUSED


@pytest.mark.parametrize("session_change", ["reaped", "rebound"])
async def test_supervisor_recovers_reaped_session_and_checkpoints_retained_slot(
    env, tmp_path, session_change,
):
    repo, git, _, _ = await retained_slot(env, tmp_path)
    (repo / "work.txt").write_text("retained work\n")
    before = git("rev-parse", "HEAD")
    await env.db.pause_task("t")
    if session_change == "reaped":
        await env.db.delete_session("s")
    else:
        await env.db.update_session("s", instance_token="replacement", state="stopped",
                                    desired_state="stopped")
    refused = await command(env, "resume_task")
    assert "--recover-stale-pause" in refused.get("error", ""), refused
    assert (await env.db.get_workspace("workspace")).locked_by_task_id == "t"
    scope = RequestScope(kind="session", session_id="supervisor", project_id="p", elevated=True)
    result = await command(env, "resume_task", recover_stale_pause=True, _scope=asdict(scope))
    assert "error" not in result, result
    assert (await env.db.get_task("t")).status == TaskStatus.READY
    checkpoint = await env.db.get_task_meta("t", "manual_pause_checkpoint")
    assert git("show", f"{checkpoint['commit']}:work.txt") == "retained work"
    assert git("rev-parse", "HEAD") == before
    assert (repo / "work.txt").read_text() == "retained work\n"
    assert (await env.db.get_workspace("workspace")).locked_by_task_id is None
    assert not (repo / ".aq" / "claim.json").exists()
    if session_change == "rebound":
        assert (await env.db.get_session("s")).task_id is None


@pytest.mark.parametrize("blocker", [
    "generation", "pin", "preparation", "unknown_session", "owner", "preparing_rebound",
    "live_peer",
])
async def test_explicit_stale_pause_recovery_keeps_source_fences(env, tmp_path, blocker):
    from dataclasses import replace

    repo, _, _, _ = await retained_slot(env, tmp_path)
    await env.db.pause_task("t")
    old_session = await env.db.get_session("s")
    await env.db.delete_session("s")
    if blocker == "generation":
        await env.db.update_workspace("workspace", generation=8)
    elif blocker == "pin":
        await env.db.update_workspace("workspace", job_pin_count=1)
    elif blocker == "preparation":
        await env.db.set_task_meta("t", "prepare_cleanup_unproved", {"reason": "unknown child"})
    elif blocker == "unknown_session":
        await env.db.create_session(replace(old_session, id="unknown", instance_token="new"))
    elif blocker == "preparing_rebound":
        await env.db.create_session(replace(
            old_session, instance_token="new", state="stopped", desired_state="stopped",
            claim_phase="preparing",
        ))
    elif blocker == "live_peer":
        await env.db.create_session(replace(old_session, task_id="peer", instance_token="new"))
    else:
        from sqlalchemy import insert
        from src.database.tables import integration_branch_owners

        async with env.db.immediate() as conn:
            await conn.execute(insert(integration_branch_owners).values(
                id="owner", repository_id="repo", ref="aq/t", owner_id="t", owner_role="worker",
                fence_token=9, handoff_state="attached", session_id=None, workspace_id="workspace",
                created_at=1, updated_at=1,
            ))
    result = await command(env, "resume_task", recover_stale_pause=True)
    assert "error" in result, result
    assert (await env.db.get_task("t")).status == TaskStatus.PAUSED
    assert (await env.db.get_workspace("workspace")).locked_by_task_id == "t"
    assert (repo / ".aq" / "claim.json").exists()
    assert await env.db.get_task_meta("t", "manual_pause_checkpoint") is None
    if blocker == "live_peer":
        assert (await env.db.get_session("s")).task_id == "peer"
        assert (await env.db.get_session("s")).state == "running"


async def test_pause_cleanup_does_not_release_worker_reused_for_peer(env, tmp_path):
    provider, _ = await running_session(env, tmp_path)
    await env.db.update_agent("agent", current_task_id="peer")
    await env.db.update_workspace("workspace", locked_by_task_id="peer")
    await pause(env)
    assert "task-session" not in provider.running
    assert (await env.db.get_agent("agent")).current_task_id == "peer"
    assert (await env.db.get_workspace("workspace")).locked_by_task_id == "peer"
    assert (await env.db.get_workspace("workspace")).locked_by_agent_id == "agent"


async def test_pool_claim_cannot_take_manually_paused_task(env):
    from src.database.queries.claim_queries import _frontier_where
    from src.database.tables import tasks
    from sqlalchemy import select

    await pause(env)
    async with env.db._engine.connect() as conn:
        candidates = (
            (await conn.execute(select(tasks.c.id).where(_frontier_where("p")))).scalars().all()
        )
    assert candidates == ["peer"]


async def test_typed_pause_and_resume_routes_are_registered():
    from src.api.codegen import build_category_routers

    routes = {
        route.operation_id: route.path
        for router in build_category_routers()
        for route in router.routes
    }
    assert routes.get("pause_task") == "/api/task/pause"
    assert routes.get("resume_task") == "/api/task/resume"


@pytest.mark.parametrize(
    "fields",
    [
        {"status": TaskStatus.READY},
        {"resume_after": 1},
        {"retry_count": 99},
        {"assigned_agent_id": "agent"},
        {"claim_epoch": 0},
    ],
)
async def test_raw_lifecycle_writes_cannot_bypass_pause(env, fields):
    await pause(env)
    before = await env.db.get_task("t")
    try:
        await env.db.update_task("t", **fields)
    except Exception as exc:
        assert "paus" in str(exc).lower()
    after = await env.db.get_task("t")
    assert (
        after.status,
        after.resume_after,
        after.retry_count,
        after.assigned_agent_id,
        after.claim_epoch,
    ) == (
        before.status,
        None,
        2,
        None,
        before.claim_epoch,
    )


async def test_paused_question_cannot_be_answered_until_resumed(env):
    await env.db.transition_task("t", TaskStatus.WAITING_INPUT, force=True)
    await pause(env)
    result = await command(env, "provide_input", input="My answer")
    assert "error" in result
    assert (await env.db.get_task("t")).description == "Original requirements"
    result = await command(env, "resume_task")
    assert "error" not in result, result
    assert (await env.db.get_task("t")).status == TaskStatus.WAITING_INPUT
    result = await command(env, "provide_input", input="My answer")
    assert "error" not in result, result
    assert (await env.db.get_task("t")).status == TaskStatus.READY


async def test_container_resume_restores_in_progress_without_scheduling_a_worker(env):
    await env.db.transition_task("t", TaskStatus.IN_PROGRESS, force=True)
    async with env.db.immediate() as conn:
        await env.db.mark_container("t", conn=conn)
    await pause(env)
    result = await command(env, "resume_task")
    assert "error" not in result, result
    assert (await env.db.get_task("t")).status == TaskStatus.IN_PROGRESS
    assert (await env.db.get_task("peer")).status == TaskStatus.READY


async def test_pause_during_provider_start_waits_then_stops_the_new_session(env, tmp_path):
    import asyncio
    from src.scheduler import AssignAction
    from src.sessions.fake import FakeProvider as SessionFake
    from src.sessions.harness_registry import HarnessRegistry, load_from_vault
    from src.sessions.spec import SessionSpecBuilder
    from src.vault import ensure_default_harnesses

    await env.db.update_profile("worker", harness="claude")
    await env.db.assign_task_to_agent("t", "agent")
    await env.db.transition_task("t", TaskStatus.IN_PROGRESS)
    env.config.sessions.enabled = True
    env.config.sessions.provider = "fake"
    ensure_default_harnesses(str(tmp_path / "launch"))
    harnesses = HarnessRegistry()
    load_from_vault(harnesses, str(tmp_path / "launch" / "vault"))
    env.orch.harness_registry = harnesses
    env.orch.session_spec_builder = SessionSpecBuilder(env.config, harnesses)
    env.orch.daemon_epoch = "test"
    started, proceed = asyncio.Event(), asyncio.Event()

    class SlowStart(SessionFake):
        async def start(self, spec):
            started.set()
            await proceed.wait()
            return await super().start(spec)

    provider = SlowStart()
    env.orch.session_providers = SimpleNamespace(create=lambda *_: provider)
    task = await env.db.get_task("t")
    await env.db.set_task_meta("t", "manual_pause_checkpoint", {"retained_until_launch": True})
    await env.db.set_task_meta("t", "supervisor_recovery_checkpoint", {"sha": "abc"})
    launch = asyncio.create_task(
        env.orch._launch_session_for_task(
            AssignAction("agent", "t", "p"), task, await env.db.get_profile("worker"), str(tmp_path)
        )
    )
    await asyncio.wait_for(started.wait(), 15)
    paused = asyncio.create_task(command(env, "pause_task"))
    await asyncio.sleep(0.05)
    proceed.set()
    await asyncio.wait_for(asyncio.gather(launch, paused), 20)
    assert "error" not in paused.result(), paused.result()
    sessions = await env.db.list_sessions()
    assert sessions and all(s.state == "stopped" for s in sessions)
    assert await env.db.get_task_meta("t", "manual_pause_checkpoint") is None
    assert await env.db.get_task_meta("t", "supervisor_recovery_checkpoint") is None
    assert (await env.db.get_task("t")).status == TaskStatus.PAUSED


async def test_pause_does_not_release_workspace_during_inflight_completion(
    env, tmp_path, monkeypatch
):
    import asyncio

    _, task = await running_session(env, tmp_path)
    entered, proceed = asyncio.Event(), asyncio.Event()

    async def branch(*_):
        return "main"

    async def pipeline(_):
        entered.set()
        await proceed.wait()
        return None, True

    monkeypatch.setattr(env.orch, "_get_default_branch", branch)
    monkeypatch.setattr(env.orch, "_run_completion_pipeline", pipeline)
    completion = asyncio.create_task(
        env.orch.complete_session_task(
            task,
            outcome="pass",
            expect_claim_epoch=task.claim_epoch,
        )
    )
    await asyncio.wait_for(entered.wait(), 15)
    paused = asyncio.create_task(command(env, "pause_task"))
    await asyncio.sleep(0.05)
    still_locked = (await env.db.get_workspace("workspace")).locked_by_task_id
    proceed.set()
    await asyncio.gather(completion, paused, return_exceptions=True)
    assert still_locked == "t"
    assert (await env.db.get_task("t")).status == TaskStatus.COMPLETED
    assert "COMPLETED" in paused.result().get("error", "")


async def test_pausing_unreleased_container_does_not_release_its_children(env):
    from src.models import DepType

    await env.db.transition_task("t", TaskStatus.DEFINED, force=True)
    await env.db.add_dependency("peer", "t", dep_type=DepType.PARENT_CHILD.value)
    assert (await env.db.get_task("peer")).is_blocked
    await pause(env)
    assert (await env.db.get_task("peer")).is_blocked
    assert (await env.db.get_task("peer")).status == TaskStatus.READY


async def test_reload_retries_unfinished_stop_without_resuming(env, tmp_path):
    provider, _ = await running_session(env, tmp_path)
    provider.fail_stop = True
    assert "error" in await command(env, "pause_task")
    provider.fail_stop = False
    reloaded = Orchestrator(env.config)
    reloaded.db = env.db
    reloaded.session_providers = env.orch.session_providers
    await reloaded._resume_paused_tasks()
    assert "task-session" not in provider.running
    assert (await env.db.get_task("t")).status == TaskStatus.PAUSED
    assert (await env.db.get_workspace("workspace")).locked_by_task_id is None


async def test_stale_launch_cannot_bump_paused_claim_epoch(env):
    await pause(env)
    epoch = (await env.db.get_task("t")).claim_epoch
    try:
        await env.db.bump_claim_epoch("t")
    except Exception as exc:
        assert "paus" in str(exc).lower()
    assert (await env.db.get_task("t")).claim_epoch == epoch


@pytest.mark.parametrize("name", ["edit_task", "set_task_status"])
async def test_status_override_cannot_fake_pause_without_stopping_execution(env, name):
    result = await command(env, name, status="PAUSED")
    assert "pause_task" in result.get("error", "")
    assert (await env.db.get_task("t")).status == TaskStatus.READY


async def retained_slot(env, tmp_path):
    import json
    from unittest.mock import AsyncMock
    from src.models import RepoConfig, RepoSourceType, Workspace, WorkspaceKind, WorktreeSentinel
    from src.orchestrator.worktree_manager import WorktreeSlotManager

    base, base_git = make_git_repo(tmp_path, "base")
    remote = str(tmp_path / "origin.git")
    base_git("init", "--bare", remote)
    base_git("remote", "add", "origin", remote)
    base_git("push", "origin", "main")
    repo = tmp_path / "slot"
    base_git("worktree", "add", "-b", "aq/t", str(repo))
    def git(*args):
        return base_git("-C", str(repo), *args)
    await env.db.create_repo(RepoConfig(id="repo", project_id="p", url=remote,
                                      source_type=RepoSourceType.CLONE))
    await env.db.upsert_workspace_kind(WorkspaceKind(project_id="p", id="project-repo"))
    await env.db.create_workspace(Workspace(
        id="base", project_id="p", workspace_path=str(base),
        source_type=RepoSourceType.CLONE, kind_id="project-repo",
    ))
    provider, task = await running_session(env, repo)
    await env.db.update_task("t", repo_id="repo", branch_name="aq/t")
    await env.db.update_workspace("workspace", slot_index=0, base_workspace_id="base",
                                  kind_id="project-repo", generation=7, source_type="worktree")
    await env.orch._worktree_slots().ensure_git_exclude(repo)
    provider.confirm_stopped = AsyncMock(return_value=True)
    workspace = await env.db.get_workspace("workspace")
    await env.db.set_task_meta("t", "claim_workspace", {
        "workspace_id": workspace.id, "generation": workspace.generation,
        "session_id": "s", "instance_token": "instance", "claim_epoch": task.claim_epoch,
    })
    claim = repo / ".aq" / "claim.json"
    claim.parent.mkdir(exist_ok=True)
    claim.write_text(json.dumps({
        "task_id": "t", "session_id": "s", "claim_epoch": task.claim_epoch,
    }))
    WorktreeSlotManager.write_sentinel(repo, WorktreeSentinel(
        slot="slot", slot_index=0, base_workspace_id="base", project_id="p",
        workspace_id="workspace", task_id="t", branch="aq/t",
    ))
    return repo, git, provider, task


@pytest.mark.parametrize("name", ["edit_task", "set_task_status"])
@pytest.mark.parametrize("status", ["BLOCKED", "FAILED"])
async def test_stop_finishes_retained_workspace_after_status_change(env, tmp_path, name, status):
    """Exact stopped managed slots return to eligible capacity without reopening work."""
    repo, git, provider, task = await retained_slot(env, tmp_path)
    git("checkout", "--detach")
    head = git("rev-parse", "HEAD")
    provider.running.discard("task-session")
    await env.db.update_session("s", state="stopped", desired_state="stopped")
    assert git("status", "--porcelain") == ""
    assert await env.db.count_available_workspaces("p", worktree_slot_cap=1) == 0

    changed = await command(env, name, status=status)
    assert "error" not in changed, changed
    assert (await env.db.get_task("t")).status.value == status
    assert git("rev-parse", "HEAD") == head
    assert (repo / ".aq" / "claim.json").exists()
    stopped = await command(env, "stop_task")
    assert "error" not in stopped, stopped
    assert (await env.db.get_task("t")).status.value == status
    assert (await env.db.get_task("t")).retry_count == task.retry_count
    assert await env.db.count_available_workspaces("p", worktree_slot_cap=1) == 1
    assert git("rev-parse", "HEAD") == head
    assert not (repo / ".aq" / "claim.json").exists()
    assert provider.confirm_stopped.await_count >= 2
    checkpoint = await env.db.get_task_meta("t", "manual_pause_checkpoint")
    assert checkpoint["branch"] is None
    assert checkpoint["retained_stop_branch"]["branch"] == "aq/t"


@pytest.mark.parametrize("status", ["BLOCKED", "FAILED"])
async def test_retained_stop_preserves_unpublished_staging_and_restores(env, tmp_path, status):
    from src.orchestrator.task_checkpoint import prepare_checkpoint, restore_checkpoint

    repo, git, _, _ = await retained_slot(env, tmp_path)
    (repo / "committed.txt").write_text("unpublished\n")
    git("add", "committed.txt")
    git("commit", "-m", "Unpublished work")
    (repo / "committed.txt").write_text("staged\n")
    git("add", "committed.txt")
    (repo / "committed.txt").write_text("unstaged\n")
    (repo / "untracked.txt").write_text("untracked\n")
    head, index, status_before = git("rev-parse", "HEAD"), git("write-tree"), git("status", "--porcelain")
    await command(env, "set_task_status", status=status)
    stopped = await command(env, "stop_task")
    assert "error" not in stopped, stopped
    assert (git("rev-parse", "HEAD"), git("write-tree"), git("status", "--porcelain")) == (
        head, index, status_before,
    )
    saved = await env.db.get_task_meta("t", "manual_pause_checkpoint")
    assert git("show", f"{saved['commit']}:untracked.txt") == "untracked"
    assert git("show", f"{saved['index_tree']}:committed.txt") == "staged"
    # Reuse consumes no checkpoint: a later exact continuation restores both layers.
    git("reset", "--hard", "main")
    git("clean", "-fd")
    prepared = await prepare_checkpoint(env.db, env.orch.git, "t", str(repo))
    await restore_checkpoint(env.db, env.orch.git, "t", str(repo), saved=prepared)
    assert git("rev-parse", "HEAD") == head
    assert git("write-tree") == index
    assert (repo / "committed.txt").read_text() == "unstaged\n"
    assert (repo / "untracked.txt").read_text() == "untracked\n"


@pytest.mark.parametrize("proof", [
    "generation", "instance", "epoch", "claim", "sentinel", "job", "preparation_hold",
    "preparing", "provider", "process", "process_unknown", "checkpoint", "branch", "origin", "base",
])
async def test_retained_stop_refuses_unproved_release(env, tmp_path, monkeypatch, proof):
    from src.orchestrator import retained_workspace
    from unittest.mock import AsyncMock

    repo, git, provider, task = await retained_slot(env, tmp_path)
    (repo / "work.txt").write_text("irreplaceable\n")
    head, index = git("rev-parse", "HEAD"), git("write-tree")
    await command(env, "set_task_status", status="BLOCKED")
    if proof == "generation":
        await env.db.update_workspace("workspace", generation=8)
    elif proof == "instance":
        await env.db.update_session("s", instance_token="successor")
    elif proof == "epoch":
        await env.db.update_task("t", claim_epoch=task.claim_epoch + 1)
    elif proof == "claim":
        (repo / ".aq" / "claim.json").unlink()
    elif proof == "sentinel":
        from src.models import WORKTREE_SENTINEL_NAME
        (repo / WORKTREE_SENTINEL_NAME).unlink()
    elif proof == "job":
        await env.db.update_workspace("workspace", job_pin_count=1)
    elif proof == "preparation_hold":
        await env.db.set_task_meta("t", "prepare_cleanup_unproved", {"claim_epoch": task.claim_epoch})
    elif proof == "preparing":
        await env.db.update_session("s", claim_phase="preparing")
    elif proof == "provider":
        provider.confirm_stopped.return_value = None
    elif proof == "process":
        monkeypatch.setattr(retained_workspace, "workspace_quiescent", AsyncMock(return_value=False))
    elif proof == "process_unknown":
        monkeypatch.setattr(retained_workspace, "workspace_quiescent",
                            AsyncMock(side_effect=PermissionError("process inventory unavailable")))
    elif proof == "checkpoint":
        monkeypatch.setattr(retained_workspace, "capture_checkpoint",
                            AsyncMock(side_effect=RuntimeError("checkpoint unavailable")))
    elif proof == "branch":
        git("checkout", "--detach", "HEAD~0")
        other = git("commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "Other branch head")
        git("update-ref", "refs/heads/aq/t", other)
    elif proof == "origin":
        git("remote", "set-url", "origin", str(tmp_path / "foreign.git"))
    elif proof == "base":
        foreign, _ = make_git_repo(tmp_path, "foreign")
        await env.db.update_workspace("base", workspace_path=str(foreign))
    result = await command(env, "stop_task")
    assert "error" in result, result
    assert (await env.db.get_workspace("workspace")).locked_by_task_id == "t"
    assert (await env.db.get_session("s")).task_id == "t"
    assert (await env.db.get_task("t")).status == TaskStatus.BLOCKED
    assert (repo / "work.txt").read_text() == "irreplaceable\n"
    assert git("rev-parse", "HEAD") == head
    assert git("write-tree") == index
    assert await env.db.count_available_workspaces("p", worktree_slot_cap=1) == 0


async def test_retained_stop_retries_same_identity_after_reload(env, tmp_path):
    repo, git, provider, _ = await retained_slot(env, tmp_path)
    (repo / "work.txt").write_text("keep\n")
    await command(env, "set_task_status", status="FAILED")
    provider.confirm_stopped.return_value = None
    assert "error" in await command(env, "stop_task")
    identity = await env.db.get_task_meta("t", "retained_workspace_stop")
    reloaded = Orchestrator(env.config)
    reloaded.db = env.db
    reloaded.session_providers = env.orch.session_providers
    provider.confirm_stopped.return_value = True
    assert await reloaded.stop_task("t") is None
    receipt = await env.db.get_task_meta("t", "retained_workspace_stop")
    assert receipt["identity"] == identity["identity"]
    assert receipt["cleanup_pending"] is False
    assert (repo / "work.txt").read_text() == "keep\n"
    assert git("branch", "--show-current") == ""
    assert await reloaded.stop_task("t") is None  # Ambiguous response retry is harmless.


async def test_retained_stop_cannot_adopt_null_task_holder(env, tmp_path):
    repo, git, provider, _ = await retained_slot(env, tmp_path)
    (repo / "work.txt").write_text("unattributed work\n")
    await command(env, "set_task_status", status="BLOCKED")
    await env.db.update_workspace("workspace", locked_by_task_id=None)
    await env.db.update_session("s", task_id=None, claim_phase=None,
                                state="stopped", desired_state="stopped")
    await env.db.update_agent("agent", current_task_id=None)
    provider.running.discard("task-session")
    (repo / ".aq" / "claim.json").unlink()
    before = await env.db.get_workspace("workspace")
    refs = git("show-ref")

    result = await command(env, "stop_task")

    assert "exactly one retained registered workspace" in result.get("error", ""), result
    assert await env.db.get_workspace("workspace") == before
    assert before.locked_by_agent_id == "agent"
    assert await env.db.count_available_workspaces("p", worktree_slot_cap=1) == 0
    assert git("show-ref") == refs
    assert (repo / "work.txt").read_text() == "unattributed work\n"
    assert await env.db.get_task_meta("t", "retained_workspace_stop") is None


@pytest.mark.parametrize("claim_present", [True, False])
async def test_retained_stop_never_promotes_unproved_detached_head(env, tmp_path, claim_present):
    repo, git, provider, _ = await retained_slot(env, tmp_path)
    canonical = git("rev-parse", "refs/heads/aq/t")
    git("push", "origin", "aq/t")
    foreign = git("commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "Other task source")
    git("update-ref", "refs/aq/recovery/unattributed", foreign)
    git("switch", "--detach", foreign)
    (repo / "work.txt").write_text("keep foreign work\n")
    refs, index = git("show-ref"), git("write-tree")
    await command(env, "set_task_status", status="BLOCKED")
    await env.db.update_session("s", state="stopped", desired_state="stopped")
    provider.running.discard("task-session")
    if not claim_present:
        (repo / ".aq" / "claim.json").unlink()

    result = await command(env, "stop_task")

    reason = "HEAD differs" if claim_present else "Claim or source sentinel"
    assert reason in result.get("error", ""), result
    assert git("rev-parse", "HEAD") == foreign
    assert git("show-ref") == refs
    assert git("write-tree") == index
    assert git("ls-remote", "origin", "refs/heads/aq/t").split()[0] == canonical
    assert (repo / "work.txt").read_text() == "keep foreign work\n"
    assert await env.db.get_task_meta("t", "manual_pause_checkpoint") is None
    assert (await env.db.get_workspace("workspace")).locked_by_task_id == "t"
    assert await env.db.count_available_workspaces("p", worktree_slot_cap=1) == 0


async def test_retained_stop_preserves_interrupted_merge_until_full_capture(env, tmp_path):
    import subprocess

    repo, git, _, _ = await retained_slot(env, tmp_path)
    git("switch", "-c", "other")
    (repo / "base.txt").write_text("their change\n")
    git("commit", "-am", "Other branch")
    git("switch", "aq/t")
    (repo / "base.txt").write_text("our change\n")
    git("commit", "-am", "Task branch")
    with pytest.raises(subprocess.CalledProcessError):
        git("merge", "other")
    merge_head, refs = git("rev-parse", "MERGE_HEAD"), git("show-ref")
    stages = git("ls-files", "--stage")
    index_path = repo / git("rev-parse", "--git-path", "index")
    index, content = index_path.read_bytes(), (repo / "base.txt").read_bytes()
    claim = (repo / ".aq" / "claim.json").read_bytes()
    await command(env, "set_task_status", status="BLOCKED")

    result = await command(env, "stop_task")

    assert "error" in result, result
    assert git("rev-parse", "MERGE_HEAD") == merge_head
    assert git("show-ref") == refs
    assert git("ls-files", "--stage") == stages
    assert index_path.read_bytes() == index
    assert (repo / "base.txt").read_bytes() == content
    assert (repo / ".aq" / "claim.json").read_bytes() == claim
    assert await env.db.get_task_meta("t", "manual_pause_checkpoint") is None
    assert (await env.db.get_workspace("workspace")).locked_by_task_id == "t"
    assert await env.db.count_available_workspaces("p", worktree_slot_cap=1) == 0


async def test_retained_stop_old_snapshot_cannot_adopt_new_acquisition(env, tmp_path):
    import json

    repo, _, provider, task = await retained_slot(env, tmp_path)
    await command(env, "set_task_status", status="BLOCKED")
    provider.confirm_stopped.return_value = None
    assert "error" in await command(env, "stop_task")
    before = await env.db.get_task_meta("t", "retained_workspace_stop")
    # Even a complete new receipt cannot refresh the old stop into a successor.
    await env.db.update_task("t", claim_epoch=task.claim_epoch + 1)
    await env.db.update_session("s", last_claim_epoch=task.claim_epoch + 1)
    await env.db.update_workspace("workspace", generation=8)
    receipt = before["identity"]["claim"] | {"generation": 8, "claim_epoch": task.claim_epoch + 1}
    await env.db.set_task_meta("t", "claim_workspace", receipt)
    marker = {"task_id": "t", "session_id": "s", "claim_epoch": task.claim_epoch + 1}
    (repo / ".aq" / "claim.json").write_text(json.dumps(marker))
    provider.confirm_stopped.return_value = True
    result = await command(env, "stop_task")
    assert "successor" in result.get("error", ""), result
    assert json.loads((repo / ".aq" / "claim.json").read_text()) == marker
    assert (await env.db.get_workspace("workspace")).generation == 8
    assert await env.db.get_task_meta("t", "retained_workspace_stop") == before


@pytest.mark.parametrize("owner_role", ["worker", "repair", "verifier"])
async def test_retained_stop_hands_off_only_its_native_writer_owner(env, tmp_path, owner_role):
    from sqlalchemy import insert, select
    from src.database.tables import integration_branch_owners

    repo, _, _, _ = await retained_slot(env, tmp_path)
    (repo / "work.txt").write_text("unpublished\n")
    async with env.db.immediate() as conn:
        await conn.execute(insert(integration_branch_owners).values(
            id="owner", repository_id="repo", ref="aq/t", owner_id="t", owner_role=owner_role,
            fence_token=9, handoff_state="attached", session_id="s", workspace_id="workspace",
            created_at=1, updated_at=1,
        ))
    await command(env, "set_task_status", status="BLOCKED")
    result = await command(env, "stop_task")
    async with env.db.immediate() as conn:
        owner = (await conn.execute(select(integration_branch_owners))).mappings().one()
    if owner_role == "verifier":
        assert "error" in result
        assert owner["workspace_id"] == "workspace"
    else:
        assert "error" not in result, result
        assert owner["fence_token"] == 9
        assert owner["handoff_state"] == "released"
        assert owner["workspace_id"] is None
        assert owner["confirmed_workspace_id"] == "workspace"


async def test_retained_stop_waits_for_unmarked_detached_child(env, tmp_path):
    import asyncio
    import os
    import sys

    repo, _, _, _ = await retained_slot(env, tmp_path)
    await command(env, "set_task_status", status="BLOCKED")
    child = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(60)",
        cwd=repo, start_new_session=True, stdout=asyncio.subprocess.PIPE,
        env={k: v for k, v in os.environ.items() if not k.startswith("AQ_")},
    )
    try:
        assert await asyncio.wait_for(child.stdout.readline(), 5) == b"ready\n"
        (env.process_inventory / str(child.pid)).symlink_to(f"/proc/{child.pid}")
        result = await command(env, "stop_task")
        assert "process still uses" in result.get("error", ""), result
        assert (await env.db.get_workspace("workspace")).locked_by_task_id == "t"
        assert (repo / ".aq" / "claim.json").exists()
    finally:
        child.terminate()
        await child.wait()
    assert "error" not in await command(env, "stop_task")


@pytest.mark.parametrize("control", ["stop_task", "pause_task"])
@pytest.mark.parametrize("source_user", [False, True])
async def test_inaccessible_live_process_retains_source_until_fresh_proof(
    env, tmp_path, monkeypatch, control, source_user,
):
    """CI's PermissionError is unknown, even with an apparently unrelated cwd."""
    from pathlib import Path

    repo, git, _, _ = await retained_slot(env, tmp_path)
    (repo / "work.txt").write_text("staged\n")
    git("add", "work.txt")
    (repo / "work.txt").write_text("unstaged\n")
    (repo / "new.txt").write_text("untracked\n")
    before = (git("rev-parse", "HEAD"), git("write-tree"), git("show-ref"))
    claim = (repo / ".aq" / "claim.json").read_bytes()
    if control == "stop_task":
        await command(env, "set_task_status", status="BLOCKED")
    proc = env.process_inventory / "123456789"
    proc.mkdir()
    (proc / "stat").write_text("123456789 (inaccessible) S 1")
    (proc / "environ").write_bytes(b"")
    (proc / "cwd").symlink_to(repo if source_user else tmp_path)
    (proc / "fd").mkdir()
    read = Path.read_bytes

    def inaccessible(path):
        if path == proc / "environ":
            raise PermissionError("fixture process environment unavailable")
        return read(path)

    monkeypatch.setattr(Path, "read_bytes", inaccessible)
    result = await command(env, control)
    assert "error" in result, result
    assert await env.db.count_available_workspaces("p", worktree_slot_cap=1) == 0
    assert (repo / ".aq" / "claim.json").read_bytes() == claim
    assert (git("rev-parse", "HEAD"), git("write-tree"), git("show-ref")) == before
    assert (repo / "work.txt").read_text() == "unstaged\n"
    assert (repo / "new.txt").read_text() == "untracked\n"
    monkeypatch.setattr(Path, "read_bytes", read)
    if source_user:
        assert "error" in await command(env, control)
        assert await env.db.count_available_workspaces("p", worktree_slot_cap=1) == 0
        # Fresh terminal evidence, not the earlier PermissionError, permits reuse.
        (proc / "stat").write_text("123456789 (inaccessible) Z 1")
    assert "error" not in await command(env, control)
    assert await env.db.count_available_workspaces("p", worktree_slot_cap=1) == 1


async def test_managed_pause_returns_capacity_only_after_full_checkpoint(env, tmp_path):
    repo, git, provider, _ = await retained_slot(env, tmp_path)
    (repo / "work.txt").write_text("staged\n")
    git("add", "work.txt")
    index = git("write-tree")
    (repo / "work.txt").write_text("unstaged\n")
    result = await command(env, "pause_task")
    assert "error" not in result, result
    assert (await env.db.get_task("t")).status == TaskStatus.PAUSED
    assert await env.db.count_available_workspaces("p", worktree_slot_cap=1) == 1
    assert provider.confirm_stopped.await_count >= 1
    assert git("write-tree") == index
    assert (repo / "work.txt").read_text() == "unstaged\n"
    assert not (repo / ".aq" / "claim.json").exists()


@pytest.mark.parametrize("proof", [
    "generation", "receipt", "job", "preparation_hold", "provider", "checkpoint",
])
async def test_managed_pause_retains_source_on_missing_proof(env, tmp_path, monkeypatch, proof):
    from src.orchestrator import task_checkpoint
    from unittest.mock import AsyncMock

    repo, _, provider, task = await retained_slot(env, tmp_path)
    (repo / "work.txt").write_text("keep\n")
    await env.db.pause_task("t")
    if proof == "generation":
        await env.db.update_workspace("workspace", generation=8)
    elif proof == "receipt":
        await env.db.set_task_meta("t", "claim_workspace", {"generation": 8})
    elif proof == "job":
        await env.db.update_workspace("workspace", job_pin_count=1)
    elif proof == "preparation_hold":
        await env.db.set_task_meta("t", "prepare_cleanup_unproved", {"claim_epoch": task.claim_epoch})
    elif proof == "provider":
        provider.confirm_stopped.return_value = None
    else:
        monkeypatch.setattr(task_checkpoint, "capture_checkpoint",
                            AsyncMock(side_effect=RuntimeError("capture unavailable")))
    for _ in range(3):
        result = await command(env, "pause_task")
        assert "error" in result, result
    assert (await env.db.get_task_meta("t", "manual_pause"))["cleanup_pending"] is True
    assert (await env.db.get_workspace("workspace")).locked_by_task_id == "t"
    assert (repo / ".aq" / "claim.json").exists()
    assert (repo / "work.txt").read_text() == "keep\n"


@pytest.mark.parametrize("control", ["pause_task", "stop_task"])
@pytest.mark.parametrize("continuation", ["restart", "successor", "lost_checkpoint"])
async def test_claim_retirement_crash_keeps_reservation_until_fenced_retry(
    env, tmp_path, monkeypatch, control, continuation,
):
    import json
    from src import claim_file
    from src.orchestrator import retained_workspace

    repo, git, _, _ = await retained_slot(env, tmp_path)
    (repo / "work.txt").write_text("staged\n")
    git("add", "work.txt")
    index = git("write-tree")
    (repo / "work.txt").write_text("unstaged\n")
    if control == "stop_task":
        await command(env, "set_task_status", status="BLOCKED")
    module = retained_workspace if control == "stop_task" else claim_file
    remove = module.remove_claim_file_if_matches

    def crash_after_unlink(*args, **kwargs):
        remove(*args, **kwargs)
        raise RuntimeError("simulated restart after claim retirement")

    monkeypatch.setattr(module, "remove_claim_file_if_matches", crash_after_unlink)
    result = await command(env, control)
    assert "error" in result, result
    assert not (repo / ".aq" / "claim.json").exists()
    assert await env.db.count_available_workspaces("p", worktree_slot_cap=1) == 0
    checkpoint = await env.db.get_task_meta("t", "manual_pause_checkpoint")
    assert checkpoint
    assert git("show", f"{checkpoint['commit']}:work.txt") == "unstaged"
    monkeypatch.undo()
    assert proctable._PROC == env.process_inventory
    if continuation == "successor":
        await env.db.update_workspace("workspace", generation=8)
        (repo / ".aq" / "claim.json").write_text(json.dumps({
            "task_id": "peer", "claim_epoch": 99, "session_id": "successor",
        }))
    elif continuation == "lost_checkpoint":
        await env.db.delete_task_meta("t", "manual_pause_checkpoint")
    reloaded = Orchestrator(env.config)
    reloaded.db = env.db
    reloaded.session_providers = env.orch.session_providers
    handler = CommandHandler(reloaded, env.config)
    result = await handler.execute(control, {"task_id": "t"})
    # Pause can recapture its unchanged source from the durable pause snapshot;
    # stop's permission for a missing marker also requires its prior checkpoint.
    succeeds = continuation == "restart" or (
        continuation == "lost_checkpoint" and control == "pause_task"
    )
    assert ("error" not in result) is succeeds, result
    assert await env.db.count_available_workspaces("p", worktree_slot_cap=1) == int(succeeds)
    assert git("write-tree") == index
    assert (repo / "work.txt").read_text() == "unstaged\n"
    if continuation == "successor":
        assert claim_file.read_claim_file(str(repo))["session_id"] == "successor"


@pytest.mark.parametrize("pending", [False, True])
async def test_new_claim_retires_only_completed_stop_and_can_stop_again(env, tmp_path, pending):
    import time
    from src.database.queries import retained_workspace_queries as queries
    from src.models import AgentState, SessionRecord

    repo, git, _, prior = await retained_slot(env, tmp_path)
    (repo / "work.txt").write_text("staged\n")
    git("add", "work.txt")
    index = git("write-tree")
    (repo / "work.txt").write_text("unstaged\n")
    await command(env, "set_task_status", status="BLOCKED")
    assert "error" not in await command(env, "stop_task")
    old_stop = await env.db.get_task_meta("t", queries.STOP_META)
    assert old_stop["cleanup_pending"] is False
    if pending:
        old_stop = old_stop | {"cleanup_pending": True}
        await env.db.set_task_meta("t", queries.STOP_META, old_stop)
    await env.db.transition_task("t", TaskStatus.READY, force=True)
    await env.db.assign_task_to_agent("t", "agent")
    await env.db.transition_task("t", TaskStatus.IN_PROGRESS)
    await env.db.bump_claim_epoch("t")
    await env.db.update_agent("agent", state=AgentState.BUSY)
    await env.db.update_workspace("workspace", locked_by_task_id="t", locked_by_agent_id="agent",
                                  generation=8)
    task = await env.db.get_task("t")
    assert task.claim_epoch > prior.claim_epoch
    session = SessionRecord(
        id="next", task_id="t", agent_id="agent", project_id="p", profile_id="worker",
        harness="fake", provider="fake", name="next-session", lifecycle="pool",
        state="running", work_dir=str(repo), epoch="test", instance_token="instance",
        started_at=time.time(), claim_phase="preparing", last_claim_epoch=task.claim_epoch,
    )
    await env.db.create_session(session)
    await env.db.set_task_meta("t", "claim_workspace", {
        "workspace_id": "workspace", "generation": 8, "session_id": "next",
        "instance_token": "instance", "claim_epoch": task.claim_epoch,
    })
    result = await env.handler._prepare_and_activate(session, session, task)
    assert result["result"] == "claimed", result
    assert await env.db.get_task_meta("t", queries.STOP_META) == (old_stop if pending else None)
    assert await env.db.get_task_meta("t", "manual_pause_checkpoint") is None
    assert git("write-tree") == index
    assert (repo / "work.txt").read_text() == "unstaged\n"
    await command(env, "set_task_status", status="BLOCKED")
    async with env.db.immediate() as conn:
        with pytest.raises(queries.RetainedWorkspaceError):
            await queries.finish(env.db, conn, "t", old_stop)
    assert (await env.db.get_workspace("workspace")).locked_by_task_id == "t"
    if pending:
        assert "error" in await command(env, "stop_task")
        assert await env.db.get_task_meta("t", queries.STOP_META) == old_stop
        assert await env.db.count_available_workspaces("p", worktree_slot_cap=1) == 0
        return
    assert "error" not in await command(env, "stop_task")
    current = await env.db.get_task_meta("t", queries.STOP_META)
    assert current["identity"]["session"]["id"] == "next"
    assert current["identity"]["workspace"]["generation"] == 8
    assert current["cleanup_pending"] is False
    assert await env.db.count_available_workspaces("p", worktree_slot_cap=1) == 1


@pytest.mark.parametrize("control", ["pause_task", "stop_task"])
@pytest.mark.parametrize("stage", ["checkpoint", "provider", "adapter", "execution"])
async def test_cleanup_uncertainty_survives_control_retry_and_reload(
    env, tmp_path, monkeypatch, control, stage,
):
    import asyncio
    from unittest.mock import AsyncMock
    from src.git.process import ProcessCleanupError
    from src.orchestrator import retained_workspace, task_checkpoint

    repo, _, provider, _ = await retained_slot(env, tmp_path)
    (repo / "work.txt").write_text("keep\n")
    if control == "stop_task":
        await command(env, "set_task_status", status="BLOCKED")
    module = retained_workspace if control == "stop_task" else task_checkpoint
    if stage == "checkpoint":
        monkeypatch.setattr(module, "capture_checkpoint",
                            AsyncMock(side_effect=ProcessCleanupError("unknown")))
    elif stage == "provider":
        monkeypatch.setattr(provider, "stop", AsyncMock(side_effect=ProcessCleanupError("unknown")))
    elif stage == "adapter":
        env.orch._adapters["agent"] = SimpleNamespace(
            stop=AsyncMock(side_effect=ProcessCleanupError("unknown")),
        )
    else:
        async def execution():
            raise ProcessCleanupError("unknown")
        running = asyncio.create_task(execution())
        await asyncio.gather(running, return_exceptions=True)
        env.orch._running_tasks["t"] = running
    result = await command(env, control)
    assert "termination is unproved" in result.get("error", ""), result
    hold = await env.db.get_task_meta("t", "prepare_cleanup_unproved")
    assert hold
    monkeypatch.undo()
    reloaded = Orchestrator(env.config)
    reloaded.db = env.db
    reloaded.session_providers = env.orch.session_providers
    if control == "pause_task":
        with pytest.raises(ValueError, match="termination is unproved"):
            await reloaded.pause_task("t")
    else:
        assert "termination is unproved" in await reloaded.stop_task("t")
    assert await env.db.get_task_meta("t", "prepare_cleanup_unproved") == hold
    assert (await env.db.get_workspace("workspace")).locked_by_task_id == "t"
    assert (repo / ".aq" / "claim.json").exists()
    assert (repo / "work.txt").read_text() == "keep\n"


async def test_reserved_parent_pause_marker_cannot_be_forged(env):
    result = await command(env, "task_set", meta={"manual_pause_withholds_children": True})
    assert "reserved" in result.get("error", "")
    assert await env.db.get_task_meta("t", "manual_pause_withholds_children") is None


async def test_retained_stop_receipt_cannot_be_forged_through_task_set(env):
    result = await command(env, "task_set", meta={"retained_workspace_stop": {"cleanup_pending": False}})
    assert "reserved" in result.get("error", ""), result
    assert await env.db.get_task_meta("t", "retained_workspace_stop") is None


async def test_preparing_reused_pool_session_is_stopped_despite_previous_epoch(env, tmp_path):
    provider, task = await running_session(env, tmp_path)
    await env.db.update_session("s", claim_phase="preparing", last_claim_epoch=99)
    # Stopping the harness does not prove an untracked daemon preparation has
    # stopped writing. Retain the source while still fencing its activation.
    result = await command(env, "pause_task")
    assert "preparation termination is unproved" in result.get("error", ""), result
    assert "task-session" not in provider.running
    assert (await env.db.get_session("s")).state == "stopped"
    assert (await env.db.get_workspace("workspace")).locked_by_task_id == "t"
    assert (await env.db.get_task_meta("t", "manual_pause"))["cleanup_pending"] is True
    assert await env.db.activate_claim("s", "t", epoch=task.claim_epoch, now=1) is None


async def test_pause_keeps_workspace_owned_until_pool_preparation_finishes(
    env, tmp_path, monkeypatch
):
    import asyncio

    provider, task = await running_session(env, tmp_path)
    async with env.db.immediate() as conn:
        await env.db.record_holder(
            conn, session_id="s", task_id=task.id, claim_epoch=task.claim_epoch,
            agent_id="agent", work_dir=str(tmp_path), now=1.0,
        )
    session = await env.db.get_session("s")
    entered, proceed = asyncio.Event(), asyncio.Event()

    async def reset(slot, task):
        entered.set()
        await proceed.wait()
        return "branch"

    monkeypatch.setattr(
        env.orch, "_worktree_slots", lambda: SimpleNamespace(reset_slot_for_task=reset)
    )
    prepare = asyncio.create_task(env.handler._prepare_and_activate(session, session, task))
    await asyncio.wait_for(entered.wait(), 15)
    paused = asyncio.create_task(command(env, "pause_task"))
    await asyncio.wait({paused}, timeout=1.0)
    still_locked = (await env.db.get_workspace("workspace")).locked_by_task_id
    proceed.set()
    await asyncio.gather(prepare, paused)
    assert still_locked == "t"
    assert "task-session" not in provider.running
    assert (await env.db.get_task("t")).status == TaskStatus.PAUSED


async def test_reconciler_stopped_marker_is_not_proof_pause_process_has_stopped(env, tmp_path):
    from src.sessions.reconciler import SessionReconciler

    provider, _ = await running_session(env, tmp_path, lifecycle="task")
    provider.fail_stop = True
    result = await command(env, "pause_task")
    assert "error" in result
    session = await env.db.get_session("s")
    rec = SessionReconciler(env.db, env.config, env.orch.session_providers, orchestrator=env.orch)
    await rec._stop_session(provider, session, reason="orphaned")
    assert (await env.db.get_session("s")).state == "stopped"
    result = await command(env, "resume_task")
    assert "error" in result
    assert (await env.db.get_task("t")).status == TaskStatus.PAUSED
    assert (await env.db.get_workspace("workspace")).locked_by_task_id == "t"
    provider.fail_stop = False
    result = await command(env, "resume_task")
    assert "error" not in result, result
    assert "task-session" not in provider.running


async def test_activate_claim_rejects_pause_even_before_session_cleanup(env, tmp_path):
    _, task = await running_session(env, tmp_path)
    await env.db.update_session("s", claim_phase="preparing")
    await env.db.pause_task("t")
    assert await env.db.activate_claim("s", "t", epoch=task.claim_epoch, now=1) is None


async def test_manual_pause_serializes_with_pool_teardown_before_worker_reuse(env, tmp_path):
    import asyncio

    provider, _ = await running_session(env, tmp_path)
    entered, proceed = asyncio.Event(), asyncio.Event()
    calls = 0
    original_stop = provider.stop

    async def delayed_stop(handle, *, grace):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await proceed.wait()
        await original_stop(handle, grace=grace)

    provider.stop = delayed_stop
    session = await env.db.get_session("s")
    teardown = asyncio.create_task(env.orch._terminate_pool_session(session, reason="test"))
    await asyncio.wait_for(entered.wait(), 15)
    paused = asyncio.create_task(command(env, "pause_task"))
    await asyncio.wait({paused}, timeout=1.0)
    premature_release = paused.done()
    proceed.set()
    await asyncio.gather(teardown, paused, return_exceptions=True)
    assert not premature_release, "Pause freed the worker before the earlier teardown finished"
    assert "error" not in paused.result(), paused.result()
    assert await env.db.assign_task_to_agent("peer", "agent")
    await env.db.update_workspace("workspace", locked_by_task_id="peer", locked_by_agent_id="agent")
    await env.orch._terminate_pool_session(session, reason="late")
    assert (await env.db.get_agent("agent")).current_task_id == "peer"
    assert (await env.db.get_workspace("workspace")).locked_by_task_id == "peer"


async def test_resume_restores_commits_and_dirty_files_after_slot_reuse(env, tmp_path):
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-b", "main")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    (repo / "base.txt").write_text("base")
    (repo / ".gitignore").write_text("secret.env\n")
    git("add", ".")
    git("commit", "-m", "base")
    git("switch", "-c", "aq/t")
    (repo / "progress.txt").write_text("committed progress")
    git("add", ".")
    git("commit", "-m", "progress")
    head = git("rev-parse", "HEAD")
    (repo / "progress.txt").write_text("staged progress")
    git("add", "progress.txt")
    (repo / "progress.txt").write_text("dirty tracked progress")
    (repo / "new.bin").write_bytes(bytes(range(256)) * 100)
    (repo / "secret.env").write_text("ignored secret")
    index_before = (repo / ".git" / "index").read_bytes()
    await running_session(env, repo)
    await env.db.update_task("t", branch_name="aq/t")
    await pause(env)
    assert (repo / ".git" / "index").read_bytes() == index_before
    assert git("rev-parse", "HEAD") == head
    manager = env.orch._worktree_slots()
    ws = await env.db.get_workspace("workspace")
    await manager.reset_slot_for_task(ws, await env.db.get_task("peer"))
    assert not (repo / "progress.txt").exists()
    assert not (repo / "new.bin").exists()
    result = await command(env, "resume_task")
    assert "error" not in result, result
    await manager.reset_slot_for_task(ws, await env.db.get_task("t"))
    # A failed provider launch retries preparation without discarding the continuation.
    await manager.reset_slot_for_task(ws, await env.db.get_task("t"))
    assert git("show", ":progress.txt") == "staged progress"
    assert git("rev-parse", "HEAD") == head
    assert (repo / "progress.txt").read_text() == "dirty tracked progress"
    assert (repo / "new.bin").read_bytes() == bytes(range(256)) * 100
    assert "secret.env" not in git("ls-tree", "-r", "HEAD")
    assert git("status", "--porcelain")


async def test_push_preparation_from_stale_task_cannot_reacquire_paused_workspace(env, tmp_path):
    _, stale = await running_session(env, tmp_path, lifecycle="task")
    agent = await env.db.get_agent("agent")
    await pause(env)
    assert await env.orch._prepare_workspace(stale, agent) is None
    assert (await env.db.get_workspace("workspace")).locked_by_task_id is None


async def test_pause_waits_for_push_workspace_preparation(env, tmp_path, monkeypatch):
    import asyncio
    from src.models import Workspace, RepoSourceType

    env.config.worktrees.enabled = False  # Exercise the legacy push preparation path.
    from src.models import WorkspaceKind, SYSTEM_KIND_SCOPE

    await env.db.upsert_workspace_kind(
        WorkspaceKind(
            project_id=SYSTEM_KIND_SCOPE,
            id="project-repo",
            is_git_repo=True,
            lockable=True,
            default_lock_mode="exclusive",
        )
    )
    await env.db.assign_task_to_agent("t", "agent")
    await env.db.transition_task("t", TaskStatus.IN_PROGRESS)
    await env.db.create_workspace(
        Workspace(
            id="workspace",
            project_id="p",
            workspace_path=str(tmp_path),
            source_type=RepoSourceType.LINK,
        )
    )
    entered, proceed = asyncio.Event(), asyncio.Event()

    async def default_branch(*_):
        entered.set()
        await proceed.wait()
        return "main"

    monkeypatch.setattr(env.orch, "_get_default_branch", default_branch)
    preparation = asyncio.create_task(
        env.orch._prepare_workspace(
            await env.db.get_task("t"),
            await env.db.get_agent("agent"),
        )
    )
    await asyncio.wait_for(entered.wait(), 15)
    paused = asyncio.create_task(command(env, "pause_task"))
    await asyncio.wait({paused}, timeout=1.0)
    prematurely_paused = paused.done()
    proceed.set()
    await asyncio.gather(preparation, paused)
    assert not prematurely_paused
    assert (await env.db.get_task("t")).status == TaskStatus.PAUSED


@pytest.mark.parametrize("operation", ["database", "cascade", "project"])
async def test_pending_pause_cannot_be_deleted_and_release_a_live_process(env, tmp_path, operation):
    provider, _ = await running_session(env, tmp_path)
    provider.fail_stop = True
    assert "error" in await command(env, "pause_task")
    # No agent FK should accidentally provide the stop safety guarantee.
    await env.db.update_agent("agent", current_task_id=None)
    try:
        if operation == "project":
            await env.db.delete_project("p")
        else:
            await env.db.delete_task("t", cascade=operation == "cascade")
    except Exception as exc:
        assert "paus" in str(exc).lower()
    assert (await env.db.get_task("t")).status == TaskStatus.PAUSED
    assert (await env.db.get_workspace("workspace")).locked_by_task_id == "t"
    assert "task-session" in provider.running


async def test_multiple_owned_workspaces_refuse_pause_before_stopping_or_changing_state(
    env, tmp_path
):
    from src.models import Workspace, RepoSourceType

    provider, _ = await running_session(env, tmp_path)
    auxiliary = tmp_path / "auxiliary"
    auxiliary.mkdir()
    (auxiliary / "artifact.txt").write_text("keep")
    await env.db.create_workspace(
        Workspace(
            id="auxiliary",
            project_id="p",
            workspace_path=str(auxiliary),
            source_type=RepoSourceType.LINK,
            locked_by_task_id="t",
        )
    )
    result = await command(env, "pause_task")
    assert "multiple" in result.get("error", "").lower()
    assert "task-session" in provider.running
    assert (await env.db.get_task("t")).status == TaskStatus.IN_PROGRESS
    assert all(ws.locked_by_task_id == "t" for ws in await env.db.list_workspaces())
    assert (auxiliary / "artifact.txt").read_text() == "keep"
    assert await env.db.get_task_meta("t", "manual_pause_checkpoint") is None


@pytest.mark.parametrize("error", [RuntimeError("late failure"), TimeoutError("late timeout")])
async def test_late_execution_error_does_not_release_paused_workspace(
    env, tmp_path, monkeypatch, error
):
    from src.scheduler import AssignAction

    await running_session(env, tmp_path)

    async def fail(_):
        await env.db.pause_task("t")
        raise error

    monkeypatch.setattr(env.orch, "_execute_task", fail)
    try:
        await env.orch._execute_task_safe_inner(AssignAction("agent", "t", "p"))
    except Exception:
        pass
    assert (await env.db.get_workspace("workspace")).locked_by_task_id == "t"
    assert (await env.db.get_agent("agent")).current_task_id == "t"


async def test_stale_legacy_worktree_cleanup_cannot_delete_paused_work(env, tmp_path):
    from src.models import RepoSourceType

    await running_session(env, tmp_path)
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("keep")
    await env.db.update_workspace("workspace", source_type=RepoSourceType.WORKTREE.value)
    await env.db.pause_task("t")
    await env.orch._release_workspaces_for_task("t")
    assert artifact.read_text() == "keep"
    assert (await env.db.get_workspace("workspace")).locked_by_task_id == "t"


def make_git_repo(root, name):
    import subprocess

    repo = root / name
    repo.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-b", "main")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    (repo / f"{name}.txt").write_text(name)
    git("add", ".")
    git("commit", "-m", name)
    return repo, git


async def test_pause_checkpoint_ignores_slot_metadata(env, tmp_path):
    from pathlib import Path

    repo, git = make_git_repo(tmp_path, "repo")
    exclude = git("rev-parse", "--git-path", "info/exclude")
    exclude_path = Path(exclude)
    if not exclude_path.is_absolute():
        exclude_path = repo / exclude_path
    with exclude_path.open("a") as handle:
        handle.write("/.aq-worktree.json\n")
    (repo / ".aq-worktree.json").write_text('{"task_id": "t"}\n')
    (repo / "progress.txt").write_text("dirty progress")

    await running_session(env, repo)
    await pause(env)

    saved = await env.db.get_task_meta("t", "manual_pause_checkpoint")
    assert saved
    tree = git("ls-tree", "-r", "--name-only", saved["commit"]).splitlines()
    assert "progress.txt" in tree
    assert ".aq-worktree.json" not in tree


@pytest.mark.parametrize("detached", [False, True])
@pytest.mark.parametrize("ignored_lock", [False, True])
async def test_pause_preserves_exact_git_layers_with_attached_or_detached_head(
    env, tmp_path, detached, ignored_lock
):
    from src.git.manager import GitError
    from src.orchestrator.task_checkpoint import prepare_checkpoint

    repo, git = make_git_repo(tmp_path, "layers")
    if detached:
        git("switch", "--detach")
    if ignored_lock:
        (repo / ".git" / "info" / "exclude").write_text("/.agent-queue-lock\n")
    (repo / "layers.txt").write_text("staged\n")
    git("add", "layers.txt")
    (repo / "layers.txt").write_text("unstaged\n")
    (repo / "new.bin").write_bytes(b"\x00untracked\xff")
    (repo / ".agent-queue-lock").write_text("t\nagent\n")
    index = (repo / ".git" / "index").read_bytes()
    head = git("rev-parse", "HEAD")
    await running_session(env, repo)
    await env.db.set_task_meta("t", "needs_attention", "integration_prepare_failed_attached")
    await env.db.set_task_meta("t", "supervisor_recovery_incident", {"hold": "human"})

    await pause(env)

    saved = await env.db.get_task_meta("t", "manual_pause_checkpoint")
    assert saved["branch"] == (None if detached else "main")
    assert saved["head"] == git("rev-parse", "HEAD") == head
    assert (repo / ".git" / "index").read_bytes() == index
    assert git("show", f"{saved['commit']}^:layers.txt") == "staged"
    assert git("show", f"{saved['commit']}:layers.txt") == "unstaged"
    assert "new.bin" in git("ls-tree", "--name-only", saved["commit"])
    assert ".agent-queue-lock" not in git("ls-tree", "--name-only", saved["commit"])
    assert git("rev-parse", saved["ref"]) == saved["commit"]
    assert (repo / "new.bin").read_bytes() == b"\x00untracked\xff"
    assert (await env.db.get_task("t")).status == TaskStatus.PAUSED
    assert await env.db.get_task_meta("t", "needs_attention") == "integration_prepare_failed_attached"
    assert await env.db.get_task_meta("t", "supervisor_recovery_incident") == {"hold": "human"}
    if detached:
        assert git("rev-parse", "--symbolic-full-name", "HEAD") == "HEAD"
        with pytest.raises(GitError, match="detached HEAD.*unchanged"):
            await prepare_checkpoint(env.db, env.orch.git, "t", str(repo))
        assert (repo / ".git" / "index").read_bytes() == index


async def test_pause_fallback_salvage_keeps_live_index_and_excludes_lock(env, tmp_path, monkeypatch):
    from src.orchestrator import task_checkpoint

    repo, git = make_git_repo(tmp_path, "partial")
    (repo / "partial.txt").write_text("staged\n")
    git("add", "partial.txt")
    (repo / "partial.txt").write_text("unstaged\n")
    (repo / "new.bin").write_bytes(b"\x00untracked\xff")
    (repo / ".agent-queue-lock").write_text("t\nagent\n")
    index = (repo / ".git" / "index").read_bytes()
    head = git("rev-parse", "HEAD")
    await running_session(env, repo)

    async def unavailable(*_args, **_kwargs):
        raise RuntimeError("object store unavailable")

    monkeypatch.setattr(task_checkpoint, "capture_checkpoint", unavailable)
    for _ in range(3):
        await command(env, "pause_task")
        assert (repo / ".git" / "index").read_bytes() == index
        assert git("rev-parse", "HEAD") == head
    contexts = {row["type"]: row["content"] for row in await env.db.get_task_contexts("t")}
    assert "Partial salvage recorded: True" in contexts["manual_pause_no_checkpoint"]
    assert "Partial salvage" in contexts["worktree_salvage"]
    assert "GIT binary patch" in contexts["worktree_salvage"]
    assert ".agent-queue-lock" not in contexts["worktree_salvage"]
    assert (await env.db.get_task_meta("t", "manual_pause"))["cleanup_pending"] is False


async def test_pause_salvage_receipt_is_fenced_against_late_reassignment(env, tmp_path, monkeypatch):
    from src.orchestrator import task_checkpoint

    repo, _ = make_git_repo(tmp_path, "late-salvage")
    (repo / "dirty.txt").write_text("preserve me\n")
    await running_session(env, repo)

    async def unavailable(*_args, **_kwargs):
        raise RuntimeError("capture unavailable")

    salvage = task_checkpoint.salvage_checkpoint

    async def reassigned(*args, save, **kwargs):
        async def guarded_save(*save_args, **context):
            await env.db.update_workspace("workspace", locked_by_task_id="peer")
            await save(*save_args, **context)
        return await salvage(*args, save=guarded_save, **kwargs)

    monkeypatch.setattr(task_checkpoint, "capture_checkpoint", unavailable)
    monkeypatch.setattr(task_checkpoint, "salvage_checkpoint", reassigned)
    for _ in range(2):
        await command(env, "pause_task")
    result = await command(env, "pause_task")
    assert "changed" in result.get("error", ""), result
    assert (await env.db.get_workspace("workspace")).locked_by_task_id == "peer"
    assert (repo / "dirty.txt").read_text() == "preserve me\n"
    assert (await env.db.get_task_meta("t", "manual_pause"))["cleanup_pending"] is True
    assert not {"worktree_salvage", "manual_pause_no_checkpoint"}.intersection(
        row["type"] for row in await env.db.get_task_contexts("t")
    )


@pytest.mark.parametrize("changed", ["workspace", "session", "owner"])
async def test_pending_pause_fails_closed_on_changed_resource_identity(env, tmp_path, changed):
    from sqlalchemy import insert, update
    from src.database.tables import integration_branch_owners

    repo, _ = make_git_repo(tmp_path, "fenced")
    provider, _ = await running_session(env, repo)
    if changed == "owner":
        async with env.db.immediate() as conn:
            await conn.execute(insert(integration_branch_owners).values(
                id="owner", repository_id="repo", ref="aq/t", owner_id="t", owner_role="task",
                fence_token=1, handoff_state="attached", session_id="s", workspace_id="workspace",
                created_at=1, updated_at=1,
            ))
    await env.db.pause_task("t")
    if changed == "workspace":
        await env.db.update_workspace("workspace", locked_by_task_id="peer")
    elif changed == "session":
        await env.db.update_session("s", instance_token="replacement")
    else:
        async with env.db.immediate() as conn:
            await conn.execute(update(integration_branch_owners).where(
                integration_branch_owners.c.id == "owner"
            ).values(fence_token=2))
    for _ in range(3):
        result = await command(env, "pause_task")
        assert "changed" in result.get("error", ""), result
    assert "task-session" in provider.running
    assert (await env.db.get_task_meta("t", "manual_pause"))["cleanup_pending"] is True
    assert await env.db.get_task_meta("t", "manual_pause_checkpoint") is None
    assert await env.db.get_task_meta("t", "manual_pause_checkpoint_retry") is None


async def test_pause_checks_reassignment_before_publishing_checkpoint(env, tmp_path, monkeypatch):
    repo, _ = make_git_repo(tmp_path, "late-reassignment")
    await running_session(env, repo)
    run = env.orch.git._arun

    async def reassign(args, **kwargs):
        result = await run(args, **kwargs)
        if "Paused task worktree" in args:
            await env.db.update_workspace("workspace", locked_by_task_id="peer")
        return result

    monkeypatch.setattr(env.orch.git, "_arun", reassign)
    result = await command(env, "pause_task")
    assert "changed" in result.get("error", ""), result
    assert await env.db.get_task_meta("t", "manual_pause_checkpoint") is None
    assert (await env.db.get_workspace("workspace")).locked_by_task_id == "peer"


@pytest.mark.parametrize(
    ("is_slot", "branch_name", "expected"),
    [(True, None, "aq/t"), (True, "aq/own", "aq/own"), (False, None, None)],
)
async def test_slot_pause_without_recorded_branch_is_held_to_the_task_branch(
    is_slot, branch_name, expected
):
    ws = SimpleNamespace(is_slot=is_slot)
    branch = Orchestrator._paused_checkpoint_branch("t", ws, {"branch_name": branch_name})
    assert branch == expected


async def test_pause_refuses_attached_foreign_task_branch_without_salvage(env, tmp_path):
    repo, git = make_git_repo(tmp_path, "foreign-branch")
    provider, _ = await running_session(env, repo)
    git("switch", "-c", "aq/peer")
    (repo / "foreign-branch.txt").write_text("staged\n")
    git("add", ".")
    (repo / "foreign-branch.txt").write_text("unstaged\n")
    (repo / "new.txt").write_text("untracked\n")
    head, index = git("rev-parse", "HEAD"), (repo / ".git" / "index").read_bytes()
    for _ in range(3):
        result = await command(env, "pause_task")
        assert "branch does not match its task" in result.get("error", ""), result
    assert "task-session" not in provider.running
    assert git("symbolic-ref", "--short", "HEAD") == "aq/peer"
    assert git("rev-parse", "HEAD") == head
    assert (repo / ".git" / "index").read_bytes() == index
    assert (repo / "foreign-branch.txt").read_text() == "unstaged\n"
    assert (repo / "new.txt").read_text() == "untracked\n"
    assert not git("for-each-ref", "refs/aq/task-pauses/")
    assert await env.db.get_task_meta("t", "manual_pause_checkpoint") is None
    assert await env.db.get_task_meta("t", "manual_pause_checkpoint_retry") is None
    assert not await env.db.get_task_contexts("t")
    assert (await env.db.get_workspace("workspace")).locked_by_task_id == "t"
    assert (await env.db.get_session("s")).task_id == "t"
    assert (await env.db.get_task_meta("t", "manual_pause"))["cleanup_pending"] is True


async def test_pause_refuses_previous_claim_session_before_checkpoint(env, tmp_path):
    repo, git = make_git_repo(tmp_path, "previous-claim")
    await running_session(env, repo)
    await env.db.update_session("s", last_claim_epoch=99)
    result = await command(env, "pause_task")
    assert "session identity changed" in result.get("error", ""), result
    assert not git("for-each-ref", "refs/aq/task-pauses/")
    assert await env.db.get_task_meta("t", "manual_pause_checkpoint") is None
    assert (await env.db.get_workspace("workspace")).locked_by_task_id == "t"


@pytest.mark.parametrize("late_change", [False, True])
async def test_pause_rejects_foreign_workspace_claim_before_publication(
    env, tmp_path, monkeypatch, late_change
):
    from src.claim_file import write_claim_file

    repo, git = make_git_repo(tmp_path, "foreign-claim")
    await running_session(env, repo)
    task = await env.db.get_task("t")
    write_claim_file(str(repo), {
        "task_id": "t" if late_change else "peer", "session_id": "s",
        "claim_epoch": task.claim_epoch,
    })
    if late_change:
        run = env.orch.git._arun

        async def reassign(args, **kwargs):
            result = await run(args, **kwargs)
            if "Paused task worktree" in args:
                write_claim_file(str(repo), {
                    "task_id": "peer", "session_id": "s", "claim_epoch": task.claim_epoch,
                })
            return result

        monkeypatch.setattr(env.orch.git, "_arun", reassign)
    result = await command(env, "pause_task")
    assert "workspace claim changed" in result.get("error", ""), result
    assert not git("for-each-ref", "refs/aq/task-pauses/")
    assert await env.db.get_task_meta("t", "manual_pause_checkpoint") is None
    assert (await env.db.get_workspace("workspace")).locked_by_task_id == "t"
    assert (await env.db.get_task_meta("t", "manual_pause"))["cleanup_pending"] is True


async def test_pause_rechecks_slot_sentinel_before_checkpoint_publication(env, tmp_path, monkeypatch):
    from src.models import WorktreeSentinel

    repo, git = make_git_repo(tmp_path, "changed-sentinel")
    await running_session(env, repo)
    run = env.orch.git._arun
    manager = env.orch._worktree_slots()

    async def reassign(args, **kwargs):
        result = await run(args, **kwargs)
        if "Paused task worktree" in args:
            manager.write_sentinel(repo, WorktreeSentinel(
                slot="slot-0", slot_index=0, base_workspace_id="", project_id="p",
                workspace_id="workspace", task_id="peer", branch="aq/peer", created_at=1,
                daemon_epoch="test", setup_hash="",
            ))
        return result

    monkeypatch.setattr(env.orch.git, "_arun", reassign)
    result = await command(env, "pause_task")
    assert "workspace sentinel changed" in result.get("error", ""), result
    assert not git("for-each-ref", "refs/aq/task-pauses/")
    assert await env.db.get_task_meta("t", "manual_pause_checkpoint") is None
    assert (await env.db.get_workspace("workspace")).locked_by_task_id == "t"
    assert manager.read_sentinel(repo).task_id == "peer"


async def test_pause_rechecks_task_branch_before_checkpoint_publication(env, tmp_path, monkeypatch):
    repo, _ = make_git_repo(tmp_path, "changed-task-branch")
    await running_session(env, repo)
    run = env.orch.git._arun

    async def change_branch(args, **kwargs):
        result = await run(args, **kwargs)
        if "Paused task worktree" in args:
            await env.db.update_task("t", branch_name="aq/peer")
        return result

    monkeypatch.setattr(env.orch.git, "_arun", change_branch)
    result = await command(env, "pause_task")
    assert "Task pause identity changed" in result.get("error", ""), result
    assert await env.db.get_task_meta("t", "manual_pause_checkpoint") is None
    assert (await env.db.get_workspace("workspace")).locked_by_task_id == "t"


async def test_pause_rechecks_foreign_branch_before_partial_salvage_receipt(
    env, tmp_path, monkeypatch
):
    from src.orchestrator import task_checkpoint

    repo, git = make_git_repo(tmp_path, "changed-salvage-branch")
    await running_session(env, repo)
    git("branch", "aq/peer")
    (repo / "new.txt").write_text("untracked\n")
    index = (repo / ".git" / "index").read_bytes()

    async def unavailable(*args, **kwargs):
        raise RuntimeError("checkpoint unavailable")

    add = task_checkpoint._add_worktree

    async def change_branch(*args):
        await add(*args)
        git("symbolic-ref", "HEAD", "refs/heads/aq/peer")

    monkeypatch.setattr(task_checkpoint, "capture_checkpoint", unavailable)
    monkeypatch.setattr(task_checkpoint, "_add_worktree", change_branch)
    for _ in range(3):
        result = await command(env, "pause_task")
    assert "branch does not match its task" in result.get("error", ""), result
    assert (repo / ".git" / "index").read_bytes() == index
    assert not await env.db.get_task_contexts("t")
    assert await env.db.get_task_meta("t", "manual_pause_checkpoint") is None
    assert (await env.db.get_task_meta("t", "manual_pause"))["cleanup_pending"] is True


async def test_pause_rechecks_stopped_session_before_checkpoint_publication(env, tmp_path, monkeypatch):
    repo, _ = make_git_repo(tmp_path, "restarted-session")
    await running_session(env, repo)
    run = env.orch.git._arun

    async def restart(args, **kwargs):
        result = await run(args, **kwargs)
        if "Paused task worktree" in args:
            await env.db.update_session("s", desired_state="running")
        return result

    monkeypatch.setattr(env.orch.git, "_arun", restart)
    result = await command(env, "pause_task")
    assert "session identity changed" in result.get("error", ""), result
    assert (await env.db.get_workspace("workspace")).locked_by_task_id == "t"
    assert (await env.db.get_session("s")).task_id == "t"
    assert (await env.db.get_task_meta("t", "manual_pause"))["cleanup_pending"] is True
    assert await env.db.get_task_meta("t", "manual_pause_checkpoint") is None


@pytest.mark.parametrize("retry_capture", [False, True])
async def test_pause_waits_for_cancelled_claim_preparation_cleanup(
    env, tmp_path, monkeypatch, retry_capture
):
    import asyncio
    from src.orchestrator import task_checkpoint

    repo, git = make_git_repo(tmp_path, "cancelled")
    _, task = await running_session(env, repo)
    await env.db.update_session("s", claim_phase="preparing")
    started = asyncio.Event()

    async def prepare():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            (repo / "late.txt").write_text("last preparation write\n")

    preparation = asyncio.create_task(prepare())
    await started.wait()
    env.orch.claim_preparations[("s", "t", task.claim_epoch)] = preparation
    if retry_capture:
        capture = task_checkpoint.capture_checkpoint

        async def unavailable(*args, **kwargs):
            monkeypatch.setattr(task_checkpoint, "capture_checkpoint", capture)
            raise RuntimeError("transient object store failure")

        monkeypatch.setattr(task_checkpoint, "capture_checkpoint", unavailable)
        result = await command(env, "pause_task")
        assert "Repeat Pause" in result.get("error", ""), result
        # The request map disappears when the cancelled request unwinds.
        env.orch.claim_preparations.clear()
    await pause(env)
    assert preparation.done()
    saved = await env.db.get_task_meta("t", "manual_pause_checkpoint")
    assert git("show", f"{saved['commit']}:late.txt") == "last preparation write"
    snapshot = await env.db.get_task_meta("t", "manual_pause")
    assert snapshot["preparations_stopped"] == [{
        "session_id": "s", "claim_epoch": task.claim_epoch, "instance_token": "instance",
    }]


async def test_pause_retains_source_until_preparation_cancellation_really_finishes(env, tmp_path):
    import asyncio

    repo, _ = make_git_repo(tmp_path, "still-writing")
    _, task = await running_session(env, repo)
    await env.db.update_session("s", claim_phase="preparing")
    started, finish = asyncio.Event(), asyncio.Event()

    async def prepare():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await finish.wait()  # Model a slow, cancellation-resistant cleanup.

    preparation = asyncio.create_task(prepare())
    await started.wait()
    env.orch.claim_preparations[("s", "t", task.claim_epoch)] = preparation
    try:
        result = await command(env, "pause_task")
        assert "preparation cleanup is still running" in result.get("error", ""), result
        assert not preparation.done()
        assert (await env.db.get_workspace("workspace")).locked_by_task_id == "t"
        assert (await env.db.get_task_meta("t", "manual_pause"))["cleanup_pending"] is True
        assert await env.db.get_task_meta("t", "manual_pause_checkpoint") is None
    finally:
        finish.set()
        await preparation
    await pause(env)


async def test_pause_retains_attached_owner_and_hold_after_checkpoint(env, tmp_path):
    from sqlalchemy import insert, select
    from src.database.tables import integration_branch_owners

    repo, git = make_git_repo(tmp_path, "attached-owner")
    git("switch", "--detach")
    provider, _ = await running_session(env, repo)
    async with env.db.immediate() as conn:
        await conn.execute(insert(integration_branch_owners).values(
            id="owner", repository_id="repo", ref="aq/t", owner_id="t", owner_role="task",
            fence_token=1, handoff_state="attached", session_id="s", workspace_id="workspace",
            created_at=1, updated_at=1,
        ))
    result = await command(env, "pause_task")
    assert "guarded handoff" in result.get("error", ""), result
    assert "task-session" not in provider.running
    assert (await env.db.get_session("s")).task_id == "t"
    assert (await env.db.get_task_meta("t", "manual_pause"))["cleanup_pending"] is True
    assert (await env.db.get_task_meta("t", "manual_pause_checkpoint"))["branch"] is None
    assert (await env.db.get_workspace("workspace")).locked_by_task_id == "t"
    async with env.db.immediate() as conn:
        owner = (await conn.execute(select(integration_branch_owners))).mappings().one()
    assert (owner["fence_token"], owner["session_id"], owner["handoff_state"]) == (1, "s", "attached")


@pytest.mark.parametrize("failure", ["unborn", "unavailable"])
async def test_bounded_git_capture_failure_has_honest_receipt_and_keeps_index(
    env, tmp_path, monkeypatch, failure
):
    import subprocess

    if failure == "unborn":
        repo = tmp_path / "unborn-layers"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)

        def git(*args):
            return subprocess.run(
                ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
            ).stdout.strip()
    else:
        repo, git = make_git_repo(tmp_path, "unavailable")

        async def unavailable(_):
            return False

        monkeypatch.setattr(env.orch.git, "avalidate_checkout", unavailable)
    (repo / "progress.txt").write_text("staged\n")
    git("add", "progress.txt")
    (repo / "progress.txt").write_text("unstaged\n")
    index = (repo / ".git" / "index").read_bytes()
    await running_session(env, repo)
    for _ in range(3):
        await command(env, "pause_task")
        assert (repo / ".git" / "index").read_bytes() == index
        assert (repo / "progress.txt").read_text() == "unstaged\n"
    assert await env.db.get_task_meta("t", "manual_pause_checkpoint") is None
    assert (await env.db.get_task_meta("t", "manual_pause"))["cleanup_pending"] is False
    contexts = {row["type"]: row["content"] for row in await env.db.get_task_contexts("t")}
    assert "no checkpoint captured:" in contexts["manual_pause_no_checkpoint"]
    assert "unstaged" in contexts["worktree_salvage"]


async def test_pause_rejects_workspace_from_an_unrelated_common_repository(env, tmp_path):
    from src.models import Workspace, RepoSourceType

    base, _ = make_git_repo(tmp_path, "expected-base")
    unrelated, _ = make_git_repo(tmp_path, "unrelated-slot")
    (unrelated / "keep.txt").write_text("unrelated work\n")
    await running_session(env, unrelated)
    await env.db.create_workspace(Workspace(
        id="base", project_id="p", workspace_path=str(base), source_type=RepoSourceType.LINK,
    ))
    await env.db.update_workspace("workspace", base_workspace_id="base", slot_index=0)
    result = await command(env, "pause_task")
    assert "different repository" in result.get("error", ""), result
    assert (unrelated / "keep.txt").read_text() == "unrelated work\n"
    assert (await env.db.get_workspace("workspace")).locked_by_task_id == "t"
    assert (await env.db.get_task_meta("t", "manual_pause"))["cleanup_pending"] is True
    assert await env.db.get_task_meta("t", "manual_pause_checkpoint") is None


async def test_legacy_pending_pause_does_not_infer_source_identity(env, tmp_path):
    repo, _ = make_git_repo(tmp_path, "legacy-pause")
    provider, _ = await running_session(env, repo)
    snapshot = await env.db.pause_task("t")
    snapshot.pop("workspaces")
    await env.db.set_task_meta("t", "manual_pause", snapshot)
    result = await command(env, "pause_task")
    assert "Legacy pause lacks source identity proof" in result.get("error", ""), result
    assert "task-session" in provider.running
    assert (await env.db.get_workspace("workspace")).locked_by_task_id == "t"
    assert (await env.db.get_task_meta("t", "manual_pause"))["cleanup_pending"] is True
    assert await env.db.get_task_meta("t", "manual_pause_checkpoint") is None


async def test_detached_pause_preserves_shared_repo_with_branch_and_lock_in_sibling(env, tmp_path):
    from pathlib import Path
    from src.models import Workspace, RepoSourceType

    base, git = make_git_repo(tmp_path, "shared-base")
    slot = tmp_path / "detached-slot"
    git("worktree", "add", "--detach", str(slot), "HEAD")
    (slot / "staged.txt").write_text("staged\n")
    git("-C", str(slot), "add", "staged.txt")
    (slot / "staged.txt").write_text("unstaged\n")
    (slot / "new.txt").write_text("untracked\n")
    slot_index = Path(git("-C", str(slot), "rev-parse", "--git-path", "index"))
    index_bytes = slot_index.read_bytes()
    base_index = (base / ".git" / "index").read_bytes()
    sibling_lock = base / ".git" / "index.lock"
    sibling_lock.write_text("unknown sibling holder\n")
    await running_session(env, slot)
    await env.db.create_workspace(Workspace(
        id="base", project_id="p", workspace_path=str(base), source_type=RepoSourceType.LINK,
    ))
    await env.db.update_workspace("workspace", base_workspace_id="base", slot_index=0)

    await pause(env)

    saved = await env.db.get_task_meta("t", "manual_pause_checkpoint")
    assert saved["branch"] is None
    assert git("symbolic-ref", "--short", "HEAD") == "main"
    assert git("-C", str(slot), "rev-parse", "--symbolic-full-name", "HEAD") == "HEAD"
    assert git("show", f"{saved['commit']}^:staged.txt") == "staged"
    assert git("show", f"{saved['commit']}:staged.txt") == "unstaged"
    assert git("show", f"{saved['commit']}:new.txt") == "untracked"
    assert slot_index.read_bytes() == index_bytes
    assert (base / ".git" / "index").read_bytes() == base_index
    assert sibling_lock.read_text() == "unknown sibling holder\n"


async def test_pause_rechecks_real_common_dir_after_late_git_source_change(env, tmp_path, monkeypatch):
    base, git = make_git_repo(tmp_path, "original-source")
    slot, unrelated = tmp_path / "slot", tmp_path / "unrelated-source"
    git("worktree", "add", "--detach", str(slot), "HEAD")
    git("clone", "--no-hardlinks", str(base), str(unrelated))
    git("-C", str(unrelated), "switch", "--detach")
    (slot / "keep.txt").write_text("source must remain\n")
    unrelated_index = (unrelated / ".git" / "index").read_bytes()
    await running_session(env, slot)
    run = env.orch.git._arun

    async def change_source(args, **kwargs):
        result = await run(args, **kwargs)
        if "Paused task worktree" in args:
            # Same HEAD and detached identity, but a different common repository.
            (slot / ".git").write_text(f"gitdir: {unrelated / '.git'}\n")
        return result

    monkeypatch.setattr(env.orch.git, "_arun", change_source)
    result = await command(env, "pause_task")
    assert "Git identity changed" in result.get("error", ""), result
    assert (slot / "keep.txt").read_text() == "source must remain\n"
    assert (unrelated / ".git" / "index").read_bytes() == unrelated_index
    assert (await env.db.get_workspace("workspace")).locked_by_task_id == "t"
    assert (await env.db.get_task_meta("t", "manual_pause"))["cleanup_pending"] is True
    assert await env.db.get_task_meta("t", "manual_pause_checkpoint") is None
    assert await env.db.get_task_meta("t", "manual_pause_checkpoint_retry") is None


async def test_checkpoint_ref_survives_original_slot_removal(env, tmp_path):
    from dataclasses import replace
    import subprocess

    base, git = make_git_repo(tmp_path, "base")
    slot = tmp_path / "slot"
    git("worktree", "add", "-b", "aq/t", str(slot))
    (slot / "new.txt").write_text("dirty progress")
    await running_session(env, slot)
    await env.db.update_task("t", branch_name="aq/t")
    await pause(env)
    saved = await env.db.get_task_meta("t", "manual_pause_checkpoint")
    assert saved["source"] == str(base / ".git")
    git("worktree", "remove", "--force", str(slot))
    assert not slot.exists()
    assert "error" not in await command(env, "resume_task")
    ws = replace(await env.db.get_workspace("workspace"), workspace_path=str(base))
    await env.orch._worktree_slots().reset_slot_for_task(ws, await env.db.get_task("t"))
    assert (base / "new.txt").read_text() == "dirty progress"
    assert (
        subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=base,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == "aq/t"
    )


@pytest.mark.parametrize("conflict", ["different_repository", "changed_branch"])
async def test_checkpoint_validation_precedes_destructive_reset(env, tmp_path, conflict):
    from dataclasses import replace
    from src.git.manager import GitError

    source, git = make_git_repo(tmp_path, "source")
    await running_session(env, source)
    await pause(env)
    assert "error" not in await command(env, "resume_task")
    if conflict == "different_repository":
        destination, _ = make_git_repo(tmp_path, "destination")
    else:
        destination = source
        (source / "later.txt").write_text("new committed work")
        git("add", ".")
        git("commit", "-m", "after pause")
    (destination / "dirty.txt").write_text("must not clean")
    ws = replace(await env.db.get_workspace("workspace"), workspace_path=str(destination))
    with pytest.raises(GitError, match="unchanged"):
        await env.orch._worktree_slots().reset_slot_for_task(ws, await env.db.get_task("t"))
    assert (destination / "dirty.txt").read_text() == "must not clean"
    assert await env.db.get_task_meta("t", "manual_pause_checkpoint")


@pytest.mark.parametrize("conflict", ["foreign_task", "foreign_project"])
async def test_checkpoint_task_identity_precedes_all_git_changes(env, tmp_path, monkeypatch, conflict):
    from src.git.manager import GitError
    from src.orchestrator.task_checkpoint import restore_checkpoint

    repo, git = make_git_repo(tmp_path, "identity")
    (repo / "identity.txt").write_text("staged\n")
    git("add", "identity.txt")
    (repo / "identity.txt").write_text("unstaged\n")
    (repo / "untracked.bin").write_bytes(b"\x00keep\xff")
    await running_session(env, repo)
    await pause(env)
    saved = await env.db.get_task_meta("t", "manual_pause_checkpoint")
    assert saved["task_id"] == "t"
    assert saved["pause_project_id"] == "p"
    assert saved["pause_workspace_id"] == "workspace"
    if conflict == "foreign_task":
        saved["task_id"] = "peer"
    else:
        saved["pause_project_id"] = "other"
    await env.db.set_task_meta("t", "manual_pause_checkpoint", saved)
    assert "error" not in await command(env, "resume_task")
    index = (repo / ".git" / "index").read_bytes()
    head = git("rev-parse", "HEAD")
    lock = repo / ".git" / "index.lock"
    lock.write_text("unknown writer: leave alone\n")

    async def unexpected_git(*args, **kwargs):
        pytest.fail("invalid checkpoint reached Git mutation or lock cleanup")

    monkeypatch.setattr(env.orch.git, "_arun", unexpected_git)
    monkeypatch.setattr(env.orch.git, "aclear_stale_locks", unexpected_git)
    ws = await env.db.get_workspace("workspace")
    with pytest.raises(GitError, match="unchanged"):
        await env.orch._worktree_slots().reset_slot_for_task(ws, await env.db.get_task("t"))
    # Supplying a previously prepared plan must not bypass task validation.
    with pytest.raises(GitError, match="unchanged"):
        await restore_checkpoint(env.db, env.orch.git, "t", str(repo), saved=saved)
    assert (repo / ".git" / "index").read_bytes() == index
    assert git("rev-parse", "HEAD") == head
    assert lock.read_text() == "unknown writer: leave alone\n"
    assert (repo / "identity.txt").read_text() == "unstaged\n"
    assert (repo / "untracked.bin").read_bytes() == b"\x00keep\xff"
    assert await env.db.get_task_meta("t", "manual_pause_checkpoint") == saved
    assert (await env.db.get_task("t")).branch_name == "main"


@pytest.mark.parametrize("legacy", [True, False])
@pytest.mark.parametrize("owned", [False, True])
async def test_foreign_branch_checkpoint_is_discarded_with_audit_before_git_changes(
    env, tmp_path, monkeypatch, legacy, owned
):
    """A pause that captured the slot's other checkout no longer blocks preparation."""
    from src.git.manager import GitError
    from src.orchestrator.task_checkpoint import (
        _foreign_checkpoint_reason,
        prepare_checkpoint,
        restore_checkpoint,
    )

    repo, git = make_git_repo(tmp_path, "foreign")
    (repo / "foreign.txt").write_text("unstaged\n")
    await running_session(env, repo)
    await pause(env)
    saved = await env.db.get_task_meta("t", "manual_pause_checkpoint")
    if legacy:  # Captured before checkpoints carried task identity (ME 10-20).
        saved.pop("task_id")
    if legacy or not owned:
        saved["branch"] = "aq/peer"
    else:  # An owned continuation proves a renamed own branch; only labels differ.
        saved["branch"] = "aq/renamed"
    await env.db.set_task_meta("t", "manual_pause_checkpoint", saved)
    head, index = git("rev-parse", "HEAD"), (repo / ".git" / "index").read_bytes()

    async def unexpected_git(*args, **kwargs):
        pytest.fail("a discarded checkpoint reached Git")

    real_git = env.orch.git._arun
    monkeypatch.setattr(env.orch.git, "_arun", unexpected_git)
    if owned and not legacy:
        # Not foreign: identity proves this task's own capture; reachability decides.
        assert await _foreign_checkpoint_reason(
            env.db, "t", saved, owned_continuation=True,
        ) is None
        return
    assert await prepare_checkpoint(
        env.db, env.orch.git, "t", str(repo), owned_branch="main" if owned else None,
    ) is None
    assert await env.db.get_task_meta("t", "manual_pause_checkpoint") is None
    audit = [ctx for ctx in await env.db.get_task_contexts("t")
             if ctx["type"] == "manual_pause_checkpoint_discarded"]
    assert len(audit) == 1
    record = json.loads(audit[0]["content"])
    assert record["actor"] == "checkpoint-preparation"
    assert "'aq/peer'" in record["reason"] and "'main'" in record["reason"]
    assert record["checkpoint"] == saved
    monkeypatch.setattr(env.orch.git, "_arun", real_git)
    assert git("rev-parse", saved["ref"]) == saved["commit"]  # The work stays reachable.
    assert git("rev-parse", "HEAD") == head
    assert (repo / ".git" / "index").read_bytes() == index
    assert (repo / "foreign.txt").read_text() == "unstaged\n"
    # An explicitly supplied stale plan still never switches to the foreign branch.
    with pytest.raises(GitError, match="unchanged"):
        await restore_checkpoint(env.db, env.orch.git, "t", str(repo), saved=saved)


async def test_foreign_checkpoint_discard_refuses_a_changed_checkpoint(env, tmp_path):
    repo, _git = make_git_repo(tmp_path, "changed")
    await running_session(env, repo)
    await pause(env)
    saved = await env.db.get_task_meta("t", "manual_pause_checkpoint")
    newer = {**saved, "head": "0" * 40}
    await env.db.set_task_meta("t", "manual_pause_checkpoint", newer)
    with pytest.raises(ValueError, match="changed during discard"):
        await env.db.discard_task_pause_checkpoint(
            "t", reason="stale", actor="test", expected=saved,
        )
    assert await env.db.get_task_meta("t", "manual_pause_checkpoint") == newer
    assert not [ctx for ctx in await env.db.get_task_contexts("t")
                if ctx["type"] == "manual_pause_checkpoint_discarded"]


async def test_discard_pause_checkpoint_command_audits_and_keeps_ref(env, tmp_path):
    repo, git = make_git_repo(tmp_path, "command")
    await running_session(env, repo)
    await pause(env)
    saved = await env.db.get_task_meta("t", "manual_pause_checkpoint")

    assert "reason is required" in (await command(env, "discard_pause_checkpoint"))["error"]
    result = await command(env, "discard_pause_checkpoint", reason="captured aq/peer")
    assert result == {
        "success": True, "task_id": "t", "discarded": True, "branch": saved["branch"],
        "ref": saved["ref"], "commit": saved["commit"],
    }
    assert await env.db.get_task_meta("t", "manual_pause_checkpoint") is None
    record = json.loads(next(
        ctx for ctx in await env.db.get_task_contexts("t")
        if ctx["type"] == "manual_pause_checkpoint_discarded"
    )["content"])
    assert (record["actor"], record["reason"]) == ("local-operator", "captured aq/peer")
    assert record["checkpoint"] == saved
    assert git("rev-parse", saved["ref"]) == saved["commit"]
    again = await command(env, "discard_pause_checkpoint", reason="again")
    assert again["success"] is False
    assert "no paused checkpoint" in again["error"]


@pytest.mark.parametrize("outcome", ["activate", "block_before_activation", "foreign_checkpoint",
    "foreign_branch_checkpoint"])
async def test_checkpoint_capture_restore_and_pool_activation_fences(
    env, tmp_path, monkeypatch, outcome
):
    import time
    from src.claim_file import read_claim_file
    from src.models import AgentState, SessionRecord, WorktreeSentinel

    repo, git = make_git_repo(tmp_path, "pipeline")
    (repo / "pipeline.txt").write_text("staged\n")
    git("add", "pipeline.txt")
    staged_tree = git("write-tree")
    (repo / "pipeline.txt").write_text("unstaged\n")
    (repo / "new.bin").write_bytes(b"\x00untracked\xff")
    original_head = git("rev-parse", "HEAD")
    original_index = (repo / ".git" / "index").read_bytes()
    await running_session(env, repo)
    await pause(env)
    saved = await env.db.get_task_meta("t", "manual_pause_checkpoint")
    assert (repo / ".git" / "index").read_bytes() == original_index
    assert "error" not in await command(env, "resume_task")

    # A proven, completed predecessor can use this physical slot between
    # capture and restoration. Its current branch is not the incoming task.
    git("reset", "--hard")
    git("clean", "-fd")
    git("switch", "-c", "aq/peer")
    (repo / "peer.txt").write_text("predecessor committed work\n")
    git("add", "peer.txt")
    git("commit", "-m", "completed predecessor")
    peer_head = git("rev-parse", "HEAD")
    await env.db.transition_task("peer", TaskStatus.COMPLETED, force=True)
    manager = env.orch._worktree_slots()
    manager.write_sentinel(repo, WorktreeSentinel(
        slot="slot-0", slot_index=0, base_workspace_id="", project_id="p",
        workspace_id="workspace", task_id="peer", branch="aq/peer", created_at=time.time(),
        daemon_epoch="test", setup_hash="",
    ))
    await env.db.assign_task_to_agent("t", "agent")
    await env.db.transition_task("t", TaskStatus.IN_PROGRESS)
    await env.db.update_agent("agent", state=AgentState.BUSY)
    await env.db.update_workspace(
        "workspace", locked_by_task_id="t", locked_by_agent_id="agent"
    )
    task = await env.db.get_task("t")
    assert task.claim_epoch > saved["pause_claim_epoch"]
    session = SessionRecord(
        id="next", task_id="t", agent_id="agent", project_id="p", profile_id="worker",
        harness="fake", provider="fake", name="next-session", lifecycle="pool",
        state="running", work_dir=str(repo), epoch="test", instance_token="instance",
        started_at=time.time(), claim_phase="preparing", last_claim_epoch=task.claim_epoch,
    )
    await env.db.create_session(session)
    workspace = await env.db.get_workspace("workspace")
    await env.db.set_task_meta("t", "claim_workspace", {
        "workspace_id": "workspace", "generation": workspace.generation,
        "session_id": "next", "instance_token": "instance", "claim_epoch": task.claim_epoch,
    })
    if outcome == "foreign_checkpoint":
        await env.db.set_task_meta("t", "manual_pause_checkpoint", {**saved, "task_id": "peer"})
    elif outcome == "foreign_branch_checkpoint":
        # ME 10-20: a pre-identity pause captured the predecessor's checkout.
        foreign = {k: v for k, v in saved.items() if k != "task_id"} | {"branch": "aq/peer"}
        await env.db.set_task_meta("t", "manual_pause_checkpoint", foreign)
    elif outcome == "block_before_activation":
        reset = manager.reset_slot_for_task

        async def block_after_restore(*args, **kwargs):
            branch = await reset(*args, **kwargs)
            # Preparation holds the workspace acquisition lock. A native pause
            # serializes behind that lock (covered separately); a task-only
            # status edit can still invalidate activation at this boundary.
            await env.db.transition_task("t", TaskStatus.BLOCKED, force=True)
            return branch

        monkeypatch.setattr(manager, "reset_slot_for_task", block_after_restore)
        monkeypatch.setattr(env.orch, "_worktree_slots", lambda: manager)
    result = await env.handler._prepare_and_activate(session, session, task)
    current = await env.db.get_task("t")
    current_session = await env.db.get_session("next")
    assert current.branch_name == "main"
    assert git("rev-parse", "aq/peer") == peer_head
    if outcome == "foreign_checkpoint":
        assert result["result"] == "prepare_failed"
        assert current_session.claims == 0
        assert git("branch", "--show-current") == "aq/peer"
        assert git("rev-parse", "HEAD") == peer_head
        assert (repo / "peer.txt").read_text() == "predecessor committed work\n"
        assert await env.db.get_task_meta("t", "manual_pause_checkpoint")
        assert read_claim_file(str(repo)) is None
        return
    if outcome == "foreign_branch_checkpoint":
        # Discarded with an audit record; the task prepares its own branch fresh.
        assert result["result"] == "claimed", result
        assert current_session.claims == 1
        assert git("branch", "--show-current") == "main"
        assert await env.db.get_task_meta("t", "manual_pause_checkpoint") is None
        audit = [ctx for ctx in await env.db.get_task_contexts("t")
                 if ctx["type"] == "manual_pause_checkpoint_discarded"]
        assert json.loads(audit[0]["content"])["checkpoint"] == foreign
        assert git("rev-parse", saved["ref"]) == saved["commit"]
        return
    assert git("branch", "--show-current") == "main"
    assert git("rev-parse", "HEAD") == original_head
    assert git("write-tree") == staged_tree
    assert (repo / "pipeline.txt").read_text() == "unstaged\n"
    assert (repo / "new.bin").read_bytes() == b"\x00untracked\xff"
    if outcome == "activate":
        assert result["result"] == "claimed"
        assert current_session.claims == 1
        assert current_session.claim_phase == "active"
        assert await env.db.get_task_meta("t", "manual_pause_checkpoint") is None
        claim = read_claim_file(str(repo))
        assert (claim["task_id"], claim["claim_epoch"], claim["session_id"]) == (
            "t", task.claim_epoch, "next"
        )
    else:
        assert result["result"] == "prepare_failed"
        assert current.status == TaskStatus.BLOCKED
        assert current_session.claims == 0
        assert await env.db.get_task_meta("t", "manual_pause_checkpoint") == saved
        assert (await env.db.get_workspace("workspace")).locked_by_task_id == "t"
        assert read_claim_file(str(repo)) is None


async def test_unborn_repository_pause_keeps_work_and_lock_with_useful_error(env, tmp_path):
    import subprocess

    repo = tmp_path / "unborn"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    (repo / "new.txt").write_text("keep")
    provider, _ = await running_session(env, repo)
    result = await command(env, "pause_task")
    assert "workspace could not be preserved" in result.get("error", "")
    assert (await env.db.get_task("t")).status == TaskStatus.PAUSED
    assert (await env.db.get_workspace("workspace")).locked_by_task_id == "t"
    assert "task-session" not in provider.running
    assert (repo / "new.txt").read_text() == "keep"


async def test_late_session_cleanup_cannot_idle_worker_before_pause_stop_confirmation(
    env, tmp_path
):
    provider, _ = await running_session(env, tmp_path)
    sentinel = tmp_path / ".agent-queue-lock"
    sentinel.write_text("t\nagent\n")
    provider.fail_stop = True
    assert "error" in await command(env, "pause_task")
    await env.orch.release_session_task_resources("t", agent_id="agent")
    assert (await env.db.get_agent("agent")).current_task_id == "t"
    assert (await env.db.get_workspace("workspace")).locked_by_task_id == "t"
    assert sentinel.exists()
    assert "task-session" in provider.running


async def test_reconciler_old_cleanup_cannot_release_a_resumed_claim(env, tmp_path):
    from src.sessions.reconciler import SessionReconciler

    _, stale = await running_session(env, tmp_path, lifecycle="task")
    old_session = await env.db.get_session("s")
    await pause(env)
    assert "error" not in await command(env, "resume_task")
    assert await env.db.assign_task_to_agent("t", "agent")
    await env.db.transition_task("t", TaskStatus.IN_PROGRESS)
    await env.db.update_workspace("workspace", locked_by_task_id="t", locked_by_agent_id="agent")
    rec = SessionReconciler(env.db, env.config, env.orch.session_providers, orchestrator=env.orch)
    await rec._release_task(stale, old_session)
    assert (await env.db.get_agent("agent")).current_task_id == "t"
    assert (await env.db.get_workspace("workspace")).locked_by_task_id == "t"


# ── Orphaned pauses ───────────────────────────────────────────────────────
#
# ``PAUSED`` with ``resume_after IS NULL`` is the operator-hold sentinel:
# ``_not_manually_paused`` fences every lifecycle write on it, so a task in
# that state can only leave PAUSED through ``resume_task``.  A genuine hold
# always carries the ``manual_pause`` snapshot, written in ``pause_task``'s
# own transaction.  One *without* the snapshot is a wedge — a backoff pause
# whose timer never landed, or a partial write — and nothing in the daemon
# used to look at it again.
#
# Observed live (calm-flare, 2026-09-01): PAUSED, ``resume_after=None``, and
# still there across a daemon restart.


async def _wedge(env, task_id="t"):
    """PAUSE a task with no timer and no hold — the unreachable state."""
    await env.db.transition_task(
        task_id, TaskStatus.PAUSED, force=True, resume_after=None, assigned_agent_id=None
    )
    task = await env.db.get_task(task_id)
    assert task.status == TaskStatus.PAUSED and task.resume_after is None
    assert await env.db.get_task_meta(task_id, "manual_pause") is None


async def test_pause_with_no_timer_and_no_hold_is_recovered(env):
    await _wedge(env)
    await env.orch._resume_paused_tasks()
    assert (await env.db.get_task("t")).status == TaskStatus.READY


async def test_wedged_pause_is_recovered_after_a_restart(env):
    """The cascade runs every cycle, so a fresh daemon picks it up too."""
    await _wedge(env)
    reloaded = Orchestrator(env.config)
    reloaded.db = env.db
    await reloaded._resume_paused_tasks()
    task = await env.db.get_task("t")
    assert task.status == TaskStatus.READY
    assert task.assigned_agent_id is None


async def test_recovered_pause_is_schedulable_again(env):
    """The point of the recovery: the manual-pause fence stops rejecting it."""
    await _wedge(env)
    await env.orch._resume_paused_tasks()
    assert await env.db.assign_task_to_agent("t", "agent") is True


async def test_operator_hold_is_never_recovered(env):
    """A real hold looks identical apart from the snapshot — leave it alone."""
    await pause(env)
    await env.orch._resume_paused_tasks()
    assert (await env.db.get_task("t")).status == TaskStatus.PAUSED
    assert await env.db.get_task_meta("t", "manual_pause") is not None


async def test_recovery_leaves_other_paused_tasks_alone(env):
    """One wedge does not disturb a peer waiting out a real backoff."""
    import time as _time

    await env.db.transition_task(
        "peer", TaskStatus.PAUSED, force=True, resume_after=_time.time() + 3600
    )
    await _wedge(env)
    await env.orch._resume_paused_tasks()
    assert (await env.db.get_task("t")).status == TaskStatus.READY
    assert (await env.db.get_task("peer")).status == TaskStatus.PAUSED


async def test_pause_with_missing_workspace_finishes_instead_of_retrying_forever(env, tmp_path):
    """A slot that is already gone has nothing to retry for — converge on the first pass."""
    import shutil

    missing = tmp_path / "reassigned-slot"
    missing.mkdir()
    provider, _ = await running_session(env, missing)
    shutil.rmtree(missing)
    await pause(env)
    assert "task-session" not in provider.running
    assert (await env.db.get_task_meta("t", "manual_pause"))["cleanup_pending"] is False
    assert await env.db.get_task_meta("t", "manual_pause_checkpoint_retry") is None
    assert (await env.db.get_workspace("workspace")).locked_by_task_id is None
    notes = [
        row
        for row in await env.db.get_task_contexts("t")
        if row["type"] == "manual_pause_no_checkpoint"
    ]
    assert notes and "no checkpoint captured:" in notes[0]["content"]
    await env.orch._resume_paused_tasks()  # nothing pending left for the cascade to retry
    assert (await env.db.get_task("t")).status == TaskStatus.PAUSED
    assert "error" not in await command(env, "resume_task")


async def test_pause_with_clean_worktree_captures_checkpoint_and_finishes(env, tmp_path):
    repo, git = make_git_repo(tmp_path, "clean")
    provider, _ = await running_session(env, repo)
    await pause(env)
    assert "task-session" not in provider.running
    saved = await env.db.get_task_meta("t", "manual_pause_checkpoint")
    assert saved and saved["head"] == git("rev-parse", "HEAD")
    assert (await env.db.get_task_meta("t", "manual_pause"))["cleanup_pending"] is False
    assert await env.db.get_task_meta("t", "manual_pause_checkpoint_retry") is None
    assert (await env.db.get_workspace("workspace")).locked_by_task_id is None
    assert "error" not in await command(env, "resume_task")


async def test_repeated_checkpoint_failure_converges_and_salvages_dirty_work(
    env, tmp_path, monkeypatch
):
    from src.orchestrator import task_checkpoint

    repo, _ = make_git_repo(tmp_path, "stuck")
    (repo / "progress.txt").write_text("dirty progress")
    await running_session(env, repo)

    async def unusable(*_args, **_kwargs):
        raise RuntimeError("slot was reassigned")

    monkeypatch.setattr(task_checkpoint, "capture_checkpoint", unusable)

    for attempt in (1, 2):
        result = await command(env, "pause_task")
        assert "workspace could not be preserved" in result.get("error", ""), result
        state = await env.db.get_task_meta("t", "manual_pause_checkpoint_retry")
        assert state["attempts"] == attempt
        assert (await env.db.get_task_meta("t", "manual_pause"))["cleanup_pending"] is True
        assert (await env.db.get_workspace("workspace")).locked_by_task_id == "t"

    assert "error" not in await command(env, "pause_task")
    assert (await env.db.get_task_meta("t", "manual_pause"))["cleanup_pending"] is False
    assert await env.db.get_task_meta("t", "manual_pause_checkpoint_retry") is None
    assert (await env.db.get_workspace("workspace")).locked_by_task_id is None
    contexts = {row["type"]: row["content"] for row in await env.db.get_task_contexts("t")}
    assert "no checkpoint captured: slot was reassigned" in contexts["manual_pause_no_checkpoint"]
    assert "dirty progress" in contexts["worktree_salvage"]
    assert "error" not in await command(env, "resume_task")


async def test_pause_cleanup_cascade_backs_off_then_stops_retrying(env, tmp_path, monkeypatch):
    from src.orchestrator import core, task_checkpoint

    repo, _ = make_git_repo(tmp_path, "cascade")
    await running_session(env, repo)
    # Stop and database I/O can outlast the two-second retry window on CI.
    # Pin only the checkpoint clock so backoff assertions do not depend on host speed.
    monkeypatch.setattr(core, "time", SimpleNamespace(time=lambda: 1_800_000_000.0))
    calls = []

    async def unusable(*_args, **_kwargs):
        calls.append(1)
        raise RuntimeError("slot was reassigned")

    monkeypatch.setattr(task_checkpoint, "capture_checkpoint", unusable)

    assert "error" in await command(env, "pause_task")
    assert len(calls) == 1
    await env.orch._resume_paused_tasks()  # inside the backoff window: no retry at all
    assert len(calls) == 1
    for _ in range(4):
        state = await env.db.get_task_meta("t", "manual_pause_checkpoint_retry")
        if state is None:
            break
        await env.db.set_task_meta(
            "t", "manual_pause_checkpoint_retry", {**state, "next_attempt_at": 0}
        )
        await env.orch._resume_paused_tasks()
    assert len(calls) == 3
    assert (await env.db.get_task_meta("t", "manual_pause"))["cleanup_pending"] is False
    await env.orch._resume_paused_tasks()
    assert len(calls) == 3
    assert (await env.db.get_task("t")).status == TaskStatus.PAUSED


async def test_backoff_resume_clears_needs_attention(env):
    """A backoff resume puts the task back in flight, so the flag must go.

    The session reconciler pauses an exit-without-close with
    ``needs_attention=session_exited_open`` (PR #49).  Every other writer of
    that key pairs it with BLOCKED, where the supervisor's retry decision
    deletes it before the task returns to READY.  The automatic resume has
    to do the same: a stale flag makes ``aq task explain`` report a session
    exit on a task that is running fine, excludes the task from BLOCKED
    re-promotion if a dependency later flips it, and turns any later
    BLOCKED into a false ``session_exited_open`` recovery incident.
    """
    import time as _time

    await env.db.set_task_meta("t", "needs_attention", "session_exited_open")
    await env.db.transition_task(
        "t",
        TaskStatus.PAUSED,
        force=True,
        resume_after=_time.time() - 1,
        assigned_agent_id=None,
    )
    await env.orch._resume_paused_tasks()
    assert (await env.db.get_task("t")).status == TaskStatus.READY
    assert await env.db.get_task_meta("t", "needs_attention") is None


async def test_completion_and_operator_edit_clear_or_set_needs_attention(env):
    await env.db.set_task_meta("t", "needs_attention", "slot_reset_failed")
    await env.db.transition_task("t", TaskStatus.COMPLETED, force=True)
    assert await env.db.get_task_meta("t", "needs_attention") is None

    set_result = await command(env, "edit_task", needs_attention="operator_review")
    assert "error" not in set_result, set_result
    assert await env.db.get_task_meta("t", "needs_attention") == "operator_review"
    clear_result = await command(env, "edit_task", clear_needs_attention=True)
    assert "error" not in clear_result, clear_result
    assert await env.db.get_task_meta("t", "needs_attention") is None


# ── The recovery write is guarded under the row lock ──────────────────────
#
# The cascade reads the status and the ``manual_pause`` snapshot in separate
# transactions.  An operator hold that commits between the scan and the
# recovery write looks exactly like a wedge from the orchestrator, and an
# unconditional ``resume_task`` would tear that fresh hold down.  So the
# write is ``recover_orphaned_pause``: it re-checks all three conditions
# after taking the task-row lock, inside the transaction that transitions.


async def test_hold_landing_after_the_scan_is_not_recovered(env, monkeypatch):
    """The regression: a pause committing after the scan decided 'wedge'."""
    await _wedge(env)
    real = env.db.recover_orphaned_pause

    async def hold_lands_first(task_id):
        # The operator's pause commits after the cascade's reads and before
        # the recovery write — the window the old two-step check had.
        await env.db.pause_task(task_id)
        return await real(task_id)

    monkeypatch.setattr(env.db, "recover_orphaned_pause", hold_lands_first)
    await env.orch._resume_paused_tasks()
    task = await env.db.get_task("t")
    assert task.status == TaskStatus.PAUSED and task.resume_after is None
    assert await env.db.get_task_meta("t", "manual_pause") is not None
    assert await env.db.assign_task_to_agent("t", "agent") is False


async def test_recover_orphaned_pause_resumes_a_wedge(env):
    await _wedge(env)
    task = await env.db.recover_orphaned_pause("t")
    assert task is not None
    assert task.status == TaskStatus.READY
    assert task.assigned_agent_id is None
    assert (await env.db.get_task("t")).status == TaskStatus.READY


async def test_recover_orphaned_pause_declines_an_operator_hold(env):
    await pause(env)
    assert await env.db.recover_orphaned_pause("t") is None
    task = await env.db.get_task("t")
    assert task.status == TaskStatus.PAUSED and task.resume_after is None
    assert await env.db.get_task_meta("t", "manual_pause") is not None
    # The hold is still the operator's to lift.
    assert (await env.orch.resume_task("t")).status == TaskStatus.READY


async def test_recover_orphaned_pause_declines_a_backoff_timer(env):
    import time as _time

    later = _time.time() + 3600
    await env.db.transition_task("t", TaskStatus.PAUSED, force=True, resume_after=later)
    assert await env.db.recover_orphaned_pause("t") is None
    task = await env.db.get_task("t")
    assert task.status == TaskStatus.PAUSED and task.resume_after == pytest.approx(later)


async def test_recover_orphaned_pause_declines_anything_not_paused(env):
    assert (await env.db.get_task("t")).status == TaskStatus.READY
    assert await env.db.recover_orphaned_pause("t") is None
    assert await env.db.recover_orphaned_pause("no-such-task") is None
    assert (await env.db.get_task("t")).status == TaskStatus.READY
