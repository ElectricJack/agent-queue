"""Operator task controls: persistence, race fences and existing work-state safety."""
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from src.api.auth import RequestScope
from src.commands.handler import CommandHandler
from src.config import AppConfig
from src.database import Database
from src.models import Agent, AgentProfile, Project, Task, TaskStatus
from src.orchestrator import Orchestrator
from tests.pg_dsn import ensure_worker_postgres_dsn

pytestmark = pytest.mark.asyncio
POSTGRES_TEST_DSN = ensure_worker_postgres_dsn()


@pytest.fixture(params=["sqlite", "postgres"])
async def env(tmp_path, request):
    if request.param == "postgres":
        if not POSTGRES_TEST_DSN:
            pytest.skip("POSTGRES_TEST_DSN not set")
        from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter
        db = PostgreSQLDatabaseAdapter(POSTGRES_TEST_DSN)
        await db.initialize()
        await db.reset_for_tests()
    else:
        db = Database(str(tmp_path / "controls.db"))
        await db.initialize()
    for pid in ("p", "other"):
        await db.create_project(Project(id=pid, name=pid))
    await db.create_profile(AgentProfile(id="worker", name="Worker", needs_workspace=False))
    await db.create_agent(Agent(id="agent", name="Worker", profile_id="worker"))
    for tid in ("t", "peer"):
        await db.create_task(Task(
            id=tid, project_id="p", title=tid, description="Original requirements",
            status=TaskStatus.READY, profile_id="worker", retry_count=2,
            branch_name="preserved", pr_url="https://example.invalid/pr/1",
        ))
    config = AppConfig(data_dir=str(tmp_path / "data"), workspace_dir=str(tmp_path / "ws"))
    orch = Orchestrator(config)
    orch.db = db
    yield SimpleNamespace(db=db, orch=orch, handler=CommandHandler(orch, config), config=config)
    await db.close()


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


@pytest.mark.parametrize("prior", [
    TaskStatus.DEFINED, TaskStatus.READY, TaskStatus.BLOCKED,
    TaskStatus.AWAITING_APPROVAL, TaskStatus.AWAITING_PLAN_APPROVAL, TaskStatus.WAITING_INPUT,
])
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


@pytest.mark.parametrize("name,extra", [
    ("restart_task", {}), ("set_task_status", {"status": "READY"}),
    ("edit_task", {"status": "READY", "description": "Must not write"}),
    ("task_set", {"meta": {"manual_pause": None}, "description": "Must not write"}),
])
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
    edit = await command(env, "task_set", description="Revised", expected_description="Original requirements")
    assert "error" not in edit, edit
    conflict = await command(env, "task_set", description="Stale", expected_description="Original requirements")
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
            "t", target, force=True, context="late_completion",
            retry_count=99, resume_after=1,
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


async def running_session(env, tmp_path, *, lifecycle="pool"):
    import time
    from src.models import AgentState, RepoSourceType, SessionRecord, Workspace

    await env.db.assign_task_to_agent("t", "agent")
    await env.db.transition_task("t", TaskStatus.IN_PROGRESS)
    await env.db.update_agent("agent", state=AgentState.BUSY)
    task = await env.db.get_task("t")
    await env.db.create_workspace(Workspace(
        id="workspace", project_id="p", workspace_path=str(tmp_path),
        source_type=RepoSourceType.LINK,
        locked_by_task_id="t", locked_by_agent_id="agent",
    ))
    await env.db.create_session(SessionRecord(
        id="s", task_id="t", agent_id="agent", project_id="p",
        profile_id="worker", harness="fake", provider="fake", name="task-session",
        lifecycle=lifecycle, state="running", work_dir=str(tmp_path),
        epoch="test", instance_token="instance", started_at=time.time(),
        claim_phase="active", last_claim_epoch=task.claim_epoch,
    ))
    provider = FakeProvider()
    env.orch.session_providers = SimpleNamespace(create=lambda *_: provider)
    return provider, task


@pytest.mark.parametrize("lifecycle", ["task", "pool"])
async def test_running_pause_stops_owned_session_and_preserves_work_then_resume(env, tmp_path, lifecycle):
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
        candidates = (await conn.execute(select(tasks.c.id).where(_frontier_where("p")))).scalars().all()
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


@pytest.mark.parametrize("fields", [
    {"status": TaskStatus.READY}, {"resume_after": 1}, {"retry_count": 99},
    {"assigned_agent_id": "agent"}, {"claim_epoch": 0},
])
async def test_raw_lifecycle_writes_cannot_bypass_pause(env, fields):
    await pause(env)
    before = await env.db.get_task("t")
    try:
        await env.db.update_task("t", **fields)
    except Exception as exc:
        assert "paus" in str(exc).lower()
    after = await env.db.get_task("t")
    assert (after.status, after.resume_after, after.retry_count, after.assigned_agent_id, after.claim_epoch) == (
        before.status, None, 2, None, before.claim_epoch,
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
    launch = asyncio.create_task(env.orch._launch_session_for_task(
        AssignAction("agent", "t", "p"), task, await env.db.get_profile("worker"), str(tmp_path)
    ))
    await asyncio.wait_for(started.wait(), 15)
    paused = asyncio.create_task(command(env, "pause_task"))
    await asyncio.sleep(0.05)
    proceed.set()
    await asyncio.wait_for(asyncio.gather(launch, paused), 20)
    assert "error" not in paused.result(), paused.result()
    sessions = await env.db.list_sessions()
    assert sessions and all(s.state == "stopped" for s in sessions)
    assert await env.db.get_task_meta("t", "manual_pause_checkpoint") is None
    assert (await env.db.get_task("t")).status == TaskStatus.PAUSED


async def test_pause_does_not_release_workspace_during_inflight_completion(env, tmp_path, monkeypatch):
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
    completion = asyncio.create_task(env.orch.complete_session_task(
        task, outcome="pass", expect_claim_epoch=task.claim_epoch,
    ))
    await asyncio.wait_for(entered.wait(), 15)
    paused = asyncio.create_task(command(env, "pause_task"))
    await asyncio.sleep(0.05)
    still_locked = (await env.db.get_workspace("workspace")).locked_by_task_id
    proceed.set()
    await asyncio.gather(completion, paused, return_exceptions=True)
    assert still_locked == "t"
    assert (await env.db.get_task("t")).status == TaskStatus.COMPLETED
    assert "COMPLETED" in paused.result().get("error", "")


async def test_pausing_unapproved_container_does_not_release_its_children(env):
    from src.models import DepType
    await env.db.transition_task("t", TaskStatus.AWAITING_PLAN_APPROVAL, force=True)
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


async def test_reserved_parent_pause_marker_cannot_be_forged(env):
    result = await command(env, "task_set", meta={"manual_pause_withholds_children": True})
    assert "reserved" in result.get("error", "")
    assert await env.db.get_task_meta("t", "manual_pause_withholds_children") is None


async def test_preparing_reused_pool_session_is_stopped_despite_previous_epoch(env, tmp_path):
    provider, task = await running_session(env, tmp_path)
    await env.db.update_session("s", claim_phase="preparing", last_claim_epoch=99)
    await pause(env)
    assert "task-session" not in provider.running
    assert (await env.db.get_session("s")).state == "stopped"
    assert await env.db.activate_claim("s", "t", epoch=task.claim_epoch, now=1) is None


async def test_pause_keeps_workspace_owned_until_pool_preparation_finishes(env, tmp_path, monkeypatch):
    import asyncio
    provider, task = await running_session(env, tmp_path)
    await env.db.update_session("s", claim_phase="preparing")
    session = await env.db.get_session("s")
    entered, proceed = asyncio.Event(), asyncio.Event()
    async def reset(slot, task):
        entered.set()
        await proceed.wait()
        return "branch"
    monkeypatch.setattr(env.orch, "_worktree_slots", lambda: SimpleNamespace(reset_slot_for_task=reset))
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
        return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
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
    await env.db.upsert_workspace_kind(WorkspaceKind(
        project_id=SYSTEM_KIND_SCOPE, id="project-repo", is_git_repo=True,
        lockable=True, default_lock_mode="exclusive",
    ))
    await env.db.assign_task_to_agent("t", "agent")
    await env.db.transition_task("t", TaskStatus.IN_PROGRESS)
    await env.db.create_workspace(Workspace(
        id="workspace", project_id="p", workspace_path=str(tmp_path),
        source_type=RepoSourceType.LINK,
    ))
    entered, proceed = asyncio.Event(), asyncio.Event()
    async def default_branch(*_):
        entered.set()
        await proceed.wait()
        return "main"
    monkeypatch.setattr(env.orch, "_get_default_branch", default_branch)
    preparation = asyncio.create_task(env.orch._prepare_workspace(
        await env.db.get_task("t"), await env.db.get_agent("agent"),
    ))
    await asyncio.wait_for(entered.wait(), 15)
    paused = asyncio.create_task(command(env, "pause_task"))
    await asyncio.wait({paused}, timeout=1.0)
    prematurely_paused = paused.done()
    proceed.set()
    await asyncio.gather(preparation, paused)
    assert not prematurely_paused
    assert (await env.db.get_task("t")).status == TaskStatus.PAUSED


@pytest.mark.parametrize("operation", ["command", "database", "cascade", "project"])
async def test_pending_pause_cannot_be_deleted_and_release_a_live_process(env, tmp_path, operation):
    provider, _ = await running_session(env, tmp_path)
    provider.fail_stop = True
    assert "error" in await command(env, "pause_task")
    # No agent FK should accidentally provide the stop safety guarantee.
    await env.db.update_agent("agent", current_task_id=None)
    try:
        if operation == "command":
            assert "error" in await command(env, "delete_task")
        elif operation == "project":
            await env.db.delete_project("p")
        else:
            await env.db.delete_task("t", cascade=operation == "cascade")
    except Exception as exc:
        assert "paus" in str(exc).lower()
    assert (await env.db.get_task("t")).status == TaskStatus.PAUSED
    assert (await env.db.get_workspace("workspace")).locked_by_task_id == "t"
    assert "task-session" in provider.running


async def test_multiple_owned_workspaces_refuse_pause_before_stopping_or_changing_state(env, tmp_path):
    from src.models import Workspace, RepoSourceType
    provider, _ = await running_session(env, tmp_path)
    auxiliary = tmp_path / "auxiliary"
    auxiliary.mkdir()
    (auxiliary / "artifact.txt").write_text("keep")
    await env.db.create_workspace(Workspace(
        id="auxiliary", project_id="p", workspace_path=str(auxiliary),
        source_type=RepoSourceType.LINK, locked_by_task_id="t",
    ))
    result = await command(env, "pause_task")
    assert "multiple" in result.get("error", "").lower()
    assert "task-session" in provider.running
    assert (await env.db.get_task("t")).status == TaskStatus.IN_PROGRESS
    assert all(ws.locked_by_task_id == "t" for ws in await env.db.list_workspaces())
    assert (auxiliary / "artifact.txt").read_text() == "keep"
    assert await env.db.get_task_meta("t", "manual_pause_checkpoint") is None


@pytest.mark.parametrize("error", [RuntimeError("late failure"), TimeoutError("late timeout")])
async def test_late_execution_error_does_not_release_paused_workspace(env, tmp_path, monkeypatch, error):
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
        return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
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


async def test_checkpoint_ref_survives_original_slot_removal(env, tmp_path):
    from dataclasses import replace
    import subprocess

    base, git = make_git_repo(tmp_path, "base")
    slot = tmp_path / "slot"
    git("worktree", "add", "-b", "aq/t", str(slot))
    (slot / "new.txt").write_text("dirty progress")
    await running_session(env, slot)
    await pause(env)
    saved = await env.db.get_task_meta("t", "manual_pause_checkpoint")
    assert saved["source"] == str(base / ".git")
    git("worktree", "remove", "--force", str(slot))
    assert not slot.exists()
    assert "error" not in await command(env, "resume_task")
    ws = replace(await env.db.get_workspace("workspace"), workspace_path=str(base))
    await env.orch._worktree_slots().reset_slot_for_task(ws, await env.db.get_task("t"))
    assert (base / "new.txt").read_text() == "dirty progress"
    assert subprocess.run(["git", "branch", "--show-current"], cwd=base, check=True, capture_output=True, text=True).stdout.strip() == "aq/t"


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


async def test_late_session_cleanup_cannot_idle_worker_before_pause_stop_confirmation(env, tmp_path):
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
