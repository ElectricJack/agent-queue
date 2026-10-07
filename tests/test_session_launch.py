"""The flock registration invariant, including failure and concurrent observation."""
from __future__ import annotations

import ast
import asyncio
import importlib
import time
from collections import Counter
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.agents.sessions import flock_session_rows
from src.config import AppConfig
from src.database import Database
from src.doctor.models import DoctorContext, Severity
from src.event_bus import EventBus
from src.models import Agent, AgentState, Project, SessionRecord, Task, TaskStatus
from src.sessions import SessionProviderRegistry, default_session_registry
from src.sessions.fake import FakeProvider
from src.sessions.launch import SessionLaunchUncertain, launch_session
from src.sessions.proctable import ProcEntry
from src.sessions.provider import SessionHandle, SessionSpec
from src.sessions.reconciler import SessionReconciler
from tests.db_fixtures import lease_dsn


@pytest.fixture
async def db():
    database = Database(lease_dsn("flock-launch"))
    await database.initialize()
    await database.create_project(Project(id="p", name="Project"))
    yield database
    await database.close()


def registration(*, profile="worker", lifecycle="task", harness="codex", project="p"):
    record = SessionRecord(
        id="session", project_id=project, profile_id=profile, harness=harness,
        provider="fake", name="s-session", lifecycle=lifecycle, work_dir="/tmp",
        epoch="epoch", instance_token="secret-fence", started_at=time.time(),
        llm_provider="openai", model="launch-model", intelligence_class="standard-high",
    )
    spec = SessionSpec(
        session_name=record.name, work_dir=record.work_dir, command=(harness,),
        instance_token=record.instance_token, env={"AQ_SESSION_ID": record.id},
    )
    return record, spec


@pytest.mark.parametrize(("role", "lifecycle", "harness", "project"), [
    ("worker", "task", "claude", "p"), ("worker", "pool", "codex", "p"),
    ("supervisor", "named", "gemini", "p"), ("supervisor", "named", "codex", None),
    ("reviewer", "task", "claude", "p"), ("repair", "task", "codex", "p"),
    ("verifier", "task", "gemini", "p"), ("other", "named", "opencode", None),
])
async def test_all_roles_are_visible_before_provider_start(db, role, lifecycle, harness, project):
    record, spec = registration(profile=role, lifecycle=lifecycle, harness=harness, project=project)
    registry = SessionProviderRegistry({"fake": FakeProvider}, enforce_registered_launches=True)
    provider = registry.create("fake")
    original = provider.start
    bus = EventBus()
    events = []
    bus.subscribe("*", lambda payload: events.append(payload["_event_type"]))

    async def observe_start(spec):
        # A separate connection sees the committed row before the primitive
        # is invoked. No agent definition or current task ownership is needed.
        rows = flock_session_rows(await db.list_sessions(), [])
        assert len(rows) == 1
        assert rows[0]["role"] == role and rows[0]["state"] == "starting"
        assert rows[0]["scope"] == (project or "global")
        assert rows[0]["model"] == "launch-model"
        assert events == ["session.registered"]
        return await original(spec)

    provider.start = observe_start
    await launch_session(db, provider, spec, record, bus=bus)
    assert (await db.get_session(record.id)).state == "running"
    assert events == ["session.registered", "session.started"]


async def test_production_registry_rejects_direct_spawn(db):
    record, spec = registration()
    provider = default_session_registry().create("fake")
    with pytest.raises(PermissionError, match="flock registration"):
        await provider.start(spec)
    assert provider.starts == []
    await launch_session(db, provider, spec, record)
    assert len(provider.starts) == 1
    # Authorization is bounded to one exact specification and await.
    with pytest.raises(PermissionError):
        await provider.start(replace(spec, session_name="bypass"))


async def test_copied_task_context_cannot_reuse_expired_launch_authorization(db):
    record, spec = registration()
    provider = default_session_registry().create("fake")
    original = provider.start
    release = asyncio.Event()
    deferred = []

    async def schedule_start(spec):
        async def later():
            await release.wait()
            return await original(spec)
        deferred.append(asyncio.create_task(later()))
        return await original(spec)

    provider.start = schedule_start
    await launch_session(db, provider, spec, record)
    release.set()
    with pytest.raises(PermissionError):
        await deferred[0]
    assert len(provider.starts) == 1


@pytest.mark.parametrize("registered", [False, True])
async def test_registration_failure_never_spawns(db, monkeypatch, registered):
    record, spec = registration()
    provider = FakeProvider()
    if not registered:
        monkeypatch.setattr(db, "create_session", AsyncMock(side_effect=RuntimeError("db down")))
    with pytest.raises((RuntimeError, ValueError)):
        await launch_session(db, provider, spec, record, registered=registered)
    assert provider.starts == []


async def test_partial_failure_is_recorded_and_stopped(db):
    record, spec = registration()
    provider = FakeProvider()
    original = provider.start

    async def partial_start(spec):
        await original(spec)
        raise RuntimeError("partial launch")

    provider.start = partial_start
    with pytest.raises(RuntimeError, match="partial launch"):
        await launch_session(db, provider, spec, record)
    failed = await db.get_session(record.id)
    assert failed.state == failed.desired_state == "stopped"
    assert failed.end_reason == "launch_failed" and failed.ended_at
    assert not provider.sessions
    assert flock_session_rows([failed], []) == []
    assert len(flock_session_rows([failed], [], include_stopped=True)) == 1


async def test_uncertain_stop_keeps_starting_registration(db):
    record, spec = registration()
    provider = FakeProvider()
    provider.script_start_error(record.name, RuntimeError("partial launch"))
    provider.stop = AsyncMock(side_effect=RuntimeError("provider unavailable"))
    with pytest.raises(SessionLaunchUncertain):
        await launch_session(db, provider, spec, record)
    assert (await db.get_session(record.id)).state == "starting"
    assert len(flock_session_rows(await db.list_sessions(), [])) == 1


async def test_reconciliation_does_not_reap_launch_awaiting_provider(db):
    record, spec = registration(lifecycle="named")
    provider = FakeProvider()
    registry = SessionProviderRegistry({"fake": FakeProvider})
    registry._instances["fake"] = provider
    entered, proceed = asyncio.Event(), asyncio.Event()
    original = provider.start

    async def slow_start(spec):
        entered.set()
        await proceed.wait()
        return await original(spec)

    provider.start = slow_start
    launch = asyncio.create_task(launch_session(db, provider, spec, record))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        reconciler = SessionReconciler(db, AppConfig(), registry)
        await reconciler._step_exits(await db.list_sessions(), time.time())
        assert (await db.get_session(record.id)).state == "starting"
        proceed.set()
        await launch
        assert (await db.get_session(record.id)).state == "running"
    finally:
        proceed.set()
        if not launch.done():
            launch.cancel()
        await asyncio.gather(launch, return_exceptions=True)


async def test_cancelled_launch_cleans_up_registration(db):
    record, spec = registration()
    provider = FakeProvider()
    provider.start = AsyncMock(side_effect=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        await launch_session(db, provider, spec, record)
    assert (await db.get_session(record.id)).state == "stopped"


async def test_running_acknowledgement_preserves_first_claim(db):
    await db.create_agent(Agent(id="a", name="Agent", profile_id="worker"))
    await db.create_task(Task(id="t", project_id="p", title="Work", description="",
                              status=TaskStatus.IN_PROGRESS, assigned_agent_id="a"))
    await db.update_agent("a", state=AgentState.BUSY, current_task_id="t")
    record, _ = registration(lifecycle="pool")
    await db.create_session(replace(record, agent_id="a", task_id="t"))
    await db.publish_session_running(record.id, record.instance_token, release_agent_reservation=True)
    agent = await db.get_agent("a")
    assert agent.state == AgentState.BUSY and agent.current_task_id == "t"


async def test_audit_reports_untracked_terminal_and_marked_processes(db, monkeypatch):
    audit = importlib.import_module("src.sessions.flock_audit")
    registry = SessionProviderRegistry({"fake": FakeProvider})
    record, spec = registration(lifecycle="named")
    await launch_session(db, registry.create("fake"), spec, record)
    provider = registry.create("fake")
    await provider.start(replace(spec, session_name="n-hidden", instance_token="hidden-token"))
    await provider.start(replace(spec, session_name="aq-host-shell-1", instance_token=""))
    await provider.start(replace(spec, session_name="human-terminal", instance_token=""))
    monkeypatch.setattr(audit, "_instance_processes", AsyncMock(return_value=[
        ProcEntry(10, 1, "codex", 1, record.instance_token),
        ProcEntry(20, 1, "claude", 1, "hidden-token"),
        ProcEntry(21, 20, "sh", 2, "hidden-token"),
    ]))
    findings = await audit.audit_flock(db, registry, AppConfig())
    assert {item["kind"] for item in findings} == {"provider_untracked", "process_untracked"}
    assert next(item for item in findings if "pids" in item)["pids"] == [20, 21]
    assert "secret-fence" not in str(findings) and "hidden-token" not in str(findings)
    ctx = DoctorContext(config=AppConfig(), db=db, handler=SimpleNamespace(
        orchestrator=SimpleNamespace(session_providers=registry, config=AppConfig()),
    ))
    check = importlib.import_module("src.doctor.session_checks").CHECKS["sessions.untracked"]
    result = await check.run(ctx)
    assert result.severity == Severity.ERROR and result.data["count"] == 2
    await provider.stop(SessionHandle("n-hidden", "fake", "hidden-token"))
    monkeypatch.setattr(audit, "_instance_processes", AsyncMock(return_value=[]))
    assert (await check.run(ctx)).severity == Severity.OK


async def test_audit_never_treats_incomplete_probe_as_zero(db):
    from src.sessions.flock_audit import audit_flock

    registry = SessionProviderRegistry({"fake": FakeProvider})
    registry.create("fake").script_partial_list(RuntimeError("listing incomplete"))
    findings = await audit_flock(db, registry, AppConfig(), scan_processes=False)
    assert [item["kind"] for item in findings] == ["probe_failed"]


async def test_audit_reports_hidden_rows_and_live_stopped_history(db, monkeypatch):
    audit = importlib.import_module("src.sessions.flock_audit")
    record, spec = registration()
    registry = SessionProviderRegistry({"fake": FakeProvider})
    await launch_session(db, registry.create("fake"), spec, record)
    await db.update_session(record.id, state="stopped", desired_state="stopped")
    monkeypatch.setattr(audit, "flock_session_rows", lambda *_args, **_kwargs: [])
    findings = await audit.audit_flock(db, registry, AppConfig(), scan_processes=False)
    assert {item["kind"] for item in findings} == {"row_hidden", "terminal_live"}


async def test_instance_audit_ignores_another_daemon_on_the_same_host(monkeypatch):
    audit = importlib.import_module("src.sessions.flock_audit")
    entries = [ProcEntry(i, 1, "codex", 1, f"token-{i}") for i in range(1, 5)]
    monkeypatch.setattr(audit, "scan_by_env_marker", AsyncMock(return_value=entries))
    environments = {
        1: {"AQ_API_URL": "http://127.0.0.1:8081/"},
        2: {"AQ_API_URL": "http://127.0.0.1:8099"}, 3: {}, 4: None,
    }
    monkeypatch.setattr(audit, "read_environ_sync", environments.get)
    assert await audit._instance_processes(AppConfig()) == entries[:1] + entries[2:3]


async def test_periodic_audit_logs_error_and_throttles(db, monkeypatch, caplog):
    audit = importlib.import_module("src.sessions.flock_audit")
    probe = AsyncMock(return_value=[{"kind": "provider_untracked", "detail": "hidden session"}])
    monkeypatch.setattr(audit, "audit_flock", probe)
    reconciler = SessionReconciler(db, AppConfig(), SessionProviderRegistry({"fake": FakeProvider}))
    await reconciler._step_flock_audit([], 100)
    await reconciler._step_flock_audit([], 101)
    await reconciler._step_flock_audit([], 131)
    assert probe.await_count == 2
    assert any(record.levelname == "ERROR" and "Flock invariant" in record.message
               for record in caplog.records)


# All .start calls are enumerated, even non-awaited calls scheduled as tasks.
# The service starts and regular-expression offsets below do not spawn agents.
_START_SITES = {
    ("src/api/app.py", "health_monitor.start"): 1,
    ("src/api/app.py", "ws_manager.start"): 2,
    ("src/api/pull_requests.py", "inbox.start"): 1,
    ("src/cli/dashboard.py", "process.start"): 1,
    ("src/commands/integration_commands.py", "self._integration_repair_service().start"): 1,
    ("src/commands/profile_commands.py", "match.start"): 3,
    ("src/commands/provider_commands.py", "match.start"): 1,
    ("src/config_secrets.py", "match.start"): 2,
    ("src/conversations/render.py", "match.start"): 2,
    ("src/dashboard_server/app.py", "self.proxy.start"): 1,
    ("src/dashboard_server/proxy.py", "exchange.start"): 1,
    ("src/dashboard_server/proxy.py", "self.start"): 1,
    ("src/discord/adapter.py", "self._bot.start"): 1,
    ("src/integration/candidates.py", "self.repair.start"): 1,
    # Freezes an epic refresh batch; the train visits it without launching an agent.
    ("src/integration/train_sources.py", "EpicRefresh(self.db, clock=self.clock).start"): 1,
    # Changelog section boundaries use re.Match offsets, never agent launches.
    ("src/integration/promotion_notes.py", "following.start"): 1,
    ("src/integration/promotion_notes.py", "match.start"): 3,
    ("src/intelligence_classes/editing.py", "block.start"): 1,
    ("src/main.py", "adapter.start"): 1,
    ("src/main.py", "metrics_sampler.start"): 1,
    ("src/metrics/loop_watchdog.py", "self._thread.start"): 1,
    ("src/metrics/perf.py", "self._watchdog.start"): 1,
    ("src/metrics/sampler.py", "self._probe.start"): 1,
    ("src/orchestrator/core.py", "self._config_watcher.start"): 1,
    ("src/orchestrator/core.py", "self.integration_service.start"): 1,
    ("src/orchestrator/core.py", "self.knowledge_generation_loop.start"): 1,
    # Runs the async mailbox consumer; session starts still use launch_session.
    ("src/orchestrator/core.py", "self.message_delivery_service.start"): 1,
    ("src/orchestrator/core.py", "self.record_outbox.start"): 1,
    ("src/orchestrator/core.py", "self.timer_service.start"): 1,
    ("src/orchestrator/sync_workflow.py", "adapter.start"): 1,
    ("src/playbooks/explanation.py", "match.start"): 1,
    ("src/playbooks/pipeline_lowering.py", "match.start"): 3,
    ("src/plugins/internal/inbox/plugin.py", "poller.start"): 1,
    ("src/profiles/class_retirement.py", "match.start"): 1,
    ("src/profiles/drift.py", "fence.start"): 1,
    ("src/profiles/drift.py", "match.start"): 2,
    ("src/profiles/drift.py", "next_heading.start"): 1,
    ("src/profiles/model_pin_migration.py", "match.start"): 1,
    ("src/profiles/parser.py", "match.start"): 1,
    ("src/sessions/harness_parser.py", "m.start"): 1,
    ("src/sessions/launch.py", "provider.start"): 1,
    ("src/task_graph/parser.py", "close.start"): 1,
    ("src/vault_glossary.py", "m.start"): 3,
    ("src/vault_glossary.py", "match.start"): 2,
}

# Raw process creation is frozen by containing function and call count: a
# new spawn in an existing file also fails. These are daemon/dashboard/test
# jobs, git and tool probes, shell/setup commands, the two provider primitives,
# and PTY attachment. ask_cli is a tool-free one-shot inference (no AQ tools,
# task or session); probe_claude_usage only queries account status.
_PROCESS_SITES = {
    ("src/cli/daemon.py", "_start_dashboard", "Popen"): 1,
    ("src/cli/daemon.py", "_start_docker_desktop", "Popen"): 1,
    ("src/cli/daemon.py", "start_daemon", "Popen"): 1,
    ("src/cli/test_runner.py", "_run_forwarding_signals", "Popen"): 1,
    ("src/commands/helpers.py", "_run_subprocess", "create_subprocess_exec"): 1,
    ("src/commands/helpers.py", "_run_subprocess_shell", "create_subprocess_shell"): 1,
    ("src/commands/profile_commands.py", "ProfileCommandsMixin._cmd_check_profile", "create_subprocess_exec"): 1,
    ("src/commands/profile_commands.py", "ProfileCommandsMixin._install_manifest", "create_subprocess_exec"): 3,
    # The detached operator updater runs aq update with a scrubbed environment.
    ("src/commands/system_commands.py", "SystemCommandsMixin._cmd_update_and_restart", "create_subprocess_exec"): 1,
    # PostgreSQL clients, Docker probes and tmux session listing never launch agents.
    ("src/database/backup.py", "_run", "create_subprocess_exec"): 1,
    ("src/doctor/builtin.py", "_probe_binary", "create_subprocess_exec"): 1,
    ("src/doctor/dashboard_server_checks.py", "_run_aq", "create_subprocess_exec"): 1,
    ("src/doctor/db_checks.py", "_git", "create_subprocess_exec"): 1,
    ("src/doctor/service_checks.py", "_run", "create_subprocess_exec"): 1,
    ("src/doctor/stall_checks.py", "_command", "create_subprocess_exec"): 1,
    ("src/git/github_cli.py", "GhRunner.run", "create_subprocess_exec"): 1,
    # Whole-source proof pipes git diff into git patch-id; neither starts an agent.
    ("src/git/manager.py", "GitManager.apatch_id", "create_subprocess_exec"): 2,
    ("src/git/manager.py", "GitManager._apush_refs_with_app_auth_to_url", "create_subprocess_exec"): 1,
    ("src/git/manager.py", "GitManager._arun_authenticated_git_once", "create_subprocess_exec"): 1,
    ("src/git/manager.py", "GitManager._arun_git_result_unlocked", "create_subprocess_exec"): 1,
    ("src/git/manager.py", "GitManager._arun_subprocess", "create_subprocess_exec"): 1,
    ("src/git/manager.py", "GitManager._arun_unlocked", "create_subprocess_exec"): 1,
    ("src/git/manager.py", "GitManager._run_isolated_import_git", "create_subprocess_exec"): 1,
    ("src/integration/regeneration.py", "_run", "create_subprocess_exec"): 1,
    ("src/jobs/e2e.py", "main", "create_subprocess_exec"): 1,
    ("src/jobs/matter.py", "native_status", "create_subprocess_exec"): 1,
    ("src/jobs/matter.py", "windows_path", "create_subprocess_exec"): 1,
    ("src/jobs/runner.py", "supervise", "create_subprocess_exec"): 1,
    ("src/jobs/service.py", "JobService.launch", "create_subprocess_exec"): 1,
    ("src/jobs/windows_owner.py", "run", "spawn"): 1,
    ("src/llm/cli.py", "ask_cli", "create_subprocess_exec"): 1,
    ("src/orchestrator/worktree_manager.py", "WorktreeSlotManager._run_setup", "create_subprocess_exec"): 1,
    ("src/plugins/internal/files.py", "_run_subprocess", "create_subprocess_exec"): 1,
    ("src/plugins/internal/vibecop.py", "VibeCopRunner._run", "create_subprocess_exec"): 1,
    ("src/plugins/internal/vibecop.py", "VibeCopRunner.status", "create_subprocess_exec"): 3,
    ("src/providers/probe.py", "probe_claude_usage", "create_subprocess_exec"): 1,
    ("src/remote_links.py", "probe_tailnet._run", "create_subprocess_exec"): 1,
    ("src/sessions/state_cache.py", "TmuxStateCache.procs", "create_subprocess_exec"): 1,
    ("src/sessions/subprocess.py", "SubprocessProvider.start", "create_subprocess_exec"): 1,
    ("src/sessions/terminal_pty.py", "PtyTmuxClient.attach", "create_subprocess_exec"): 1,
    ("src/sessions/tmux.py", "TmuxProvider._tmux", "create_subprocess_exec"): 1,
    ("src/test_selection/static_impact.py", "PytestImpactedAdapter._run", "create_subprocess_exec"): 1,
}


def _launch_inventory(sources):
    starts, processes, launches, tmux = Counter(), Counter(), Counter(), Counter()

    def walk(node, path, scope=""):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            scope = f"{scope}.{node.name}" if scope else node.name
        if isinstance(node, ast.Call):
            name = (node.func.attr if isinstance(node.func, ast.Attribute)
                    else node.func.id if isinstance(node.func, ast.Name) else "")
            if name == "start" and isinstance(node.func, ast.Attribute):
                starts[path, ast.unparse(node.func)] += 1
            if name in {"create_subprocess_exec", "create_subprocess_shell", "Popen", "fork",
                        "posix_spawn", "execve", "spawn", "spawnv", "new_session"}:
                processes[path, scope, name] += 1
            if name == "launch_session":
                launches[path, scope] += 1
        if isinstance(node, ast.Constant) and node.value == "new-session":
            tmux[path, scope] += 1
        for child in ast.iter_child_nodes(node):
            walk(child, path, scope)

    for path, source in sources:
        walk(ast.parse(source), path)
    return starts, processes, launches, tmux


def test_all_agent_launch_paths_use_the_chokepoint():
    root = Path(__file__).resolve().parents[1]
    paths = sorted((root / "src").rglob("*.py")) + sorted(root.glob("*.py"))
    starts, processes, launches, tmux = _launch_inventory(
        [(str(path.relative_to(root)), path.read_text()) for path in paths]
    )
    assert starts == Counter(_START_SITES), "New .start path: route agents through launch_session"
    assert processes == Counter(_PROCESS_SITES), "Unreviewed OS spawn could bypass the flock"
    # Task launches cover workers, reviews, repairs, verifiers and playbook
    # delegates; pool and named launches cover workers and both supervisors.
    assert launches == Counter({
        ("src/orchestrator/execution.py", "ExecutionMixin._launch_session_for_task_locked"): 2,
        ("src/orchestrator/pools.py", "PoolsMixin._launch_pool_session_inner"): 1,
        ("src/agents/terminals.py", "_start_locked"): 1,
        ("src/messages/session_lens.py", "SessionLens._ensure_started_locked"): 1,
    })
    # Host shells strip AQ/harness markers and are explicitly human terminals.
    assert tmux == Counter({
        ("src/sessions/tmux.py", "TmuxProvider.start"): 1,
        ("src/sessions/host_shell.py", "HostShellManager.open"): 1,
    })


@pytest.mark.parametrize("source", [
    "async def bypass():\n await runner.start(spec)",
    "def bypass():\n asyncio.create_task(provider.start(spec))",
    "async def bypass():\n await asyncio.create_subprocess_exec('tmux', 'new-session')",
    "def bypass():\n subprocess.Popen(['codex'])",
])
def test_bypass_inventory_detects_new_launches(source):
    starts, processes, _, tmux = _launch_inventory([("src/bypass.py", source)])
    assert starts - Counter(_START_SITES) or processes - Counter(_PROCESS_SITES) or tmux
