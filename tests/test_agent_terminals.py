"""Explicit terminal starts use fake providers and a disposable database only."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.api.auth import SessionTokenStore
from src.commands.handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.event_bus import EventBus
from src.messages.session_lens import SessionLens
from src.models import Agent, AgentProfile, AgentState, Project, SessionRecord, Task, TaskStatus
from src.sessions import SessionProviderRegistry
from src.sessions.fake import FakeProvider
from src.sessions.harness_parser import Harness, ResumeSpec
from src.sessions.harness_registry import HarnessRegistry
from src.sessions.provider import Cap, SessionHandle
from src.sessions.spec import SessionSpecBuilder


class InteractiveFake(FakeProvider):
    capabilities = FakeProvider.capabilities | {Cap.INPUT}


@pytest.fixture
async def handler(tmp_path):
    db = Database(str(tmp_path / "terminals.db"))
    await db.initialize()
    config = AppConfig(
        discord=DiscordConfig(bot_token="test", guild_id="1"),
        workspace_dir=str(tmp_path / "work"),
        data_dir=str(tmp_path / "data"),
    )
    config.sessions.provider = "fake"
    config.sessions.enabled = True
    registry = HarnessRegistry()
    registry.upsert(
        Harness(
            id="claude",
            command="claude",
            model_flag="--model",
            session_id_flag="--session-id",
            resume=ResumeSpec(style="flag"),
        )
    )
    builder = SessionSpecBuilder(config, registry)
    orch = SimpleNamespace(
        db=db,
        config=config,
        bus=EventBus(validate_events=False),
        harness_registry=registry,
        session_spec_builder=builder,
        session_providers=SessionProviderRegistry({"fake": InteractiveFake}, config=config),
        token_store=SessionTokenStore(db),
        daemon_epoch="test-epoch",
    )
    for profile_id in ("coder", "supervisor"):
        await db.create_profile(
            AgentProfile(
                id=profile_id,
                name=profile_id,
                harness="claude",
                model="profile-model",
                wake_mode="resume",
                lifecycle="named",
            )
        )
    await db.create_agent(Agent(id="worker-a", name="Ada", profile_id="coder", model="my-model"))
    orch.session_lens = SessionLens(
        db=db,
        providers=orch.session_providers,
        spec_builder=builder,
        harness_registry=registry,
        config=config,
        profiles_loader=db.get_profile,
        token_store=orch.token_store,
    )
    result = CommandHandler(orch, config)
    yield result
    result._current_scope = None
    await db.close()


async def start(handler, agent_id="worker-a"):
    command = getattr(handler, "_cmd_start_agent_terminal", None)
    assert command is not None, "an explicit agent terminal start command is required"
    return await command({"agent_id": agent_id})


def provider(handler):
    return handler.orchestrator.session_providers.create("fake")


async def test_worker_starts_private_named_terminal_with_scoped_token(handler):
    assert (await handler._cmd_get_agent({"agent_id": "worker-a"}))["session_id"] is None
    assert provider(handler).starts == []
    result = await start(handler)
    assert "error" not in result, result
    row = await handler.db.get_session(result["session_id"])
    spec = provider(handler).starts[0]
    assert row.agent_id == "worker-a"
    assert row.lifecycle == "named" and row.project_id is None and row.task_id is None
    assert row.model == "my-model" and row.llm_provider == "anthropic"
    assert row.name.startswith("n-agent-") and spec.session_name == row.name
    assert spec.env["AQ_SESSION_NAME"] == row.name
    assert spec.env["AQ_AGENT_ID"] == "worker-a"
    assert spec.env["AQ_PROJECT_ID"] == "" and not spec.env.get("AQ_TASK_ID")
    assert Path(row.work_dir).is_relative_to(Path(handler.config.data_dir))
    assert Path(row.work_dir).is_dir()
    scope = await handler.orchestrator.token_store.validate(spec.env["AQ_API_TOKEN"])
    assert scope.session_id == row.id and not scope.elevated
    assert scope.project_id is None and scope.task_id is None
    assert "aq task claim" not in spec.prompt and "aq inbox" not in spec.prompt
    assert await handler.db.list_projects() == [] and await handler.db.list_tasks() == []
    assert (await handler.db.get_agent("worker-a")).state == AgentState.IDLE


async def test_concurrent_starts_reuse_the_same_live_terminal(handler):
    first, second = await asyncio.gather(start(handler), start(handler))
    assert "error" not in first and "error" not in second, (first, second)
    assert first["session_id"] == second["session_id"]
    assert len(provider(handler).starts) == 1
    assert await handler.db.reserve_idle_agent("worker-a") is False


@pytest.mark.parametrize("change", [{"enabled": False}, {"state": AgentState.BUSY}])
async def test_disabled_or_busy_worker_cannot_start(handler, change):
    await handler.db.update_agent("worker-a", **change)
    assert "error" in await start(handler)
    assert provider(handler).starts == []


async def test_deleted_worker_cannot_start(handler):
    assert await handler.db.soft_delete_agent("worker-a")
    assert "error" in await start(handler)
    assert provider(handler).starts == []


async def test_non_admin_cannot_start(handler):
    handler._current_scope = {"kind": "session", "project_id": "p", "elevated": True}
    assert "scope" in (await start(handler))["error"]
    assert provider(handler).starts == []


@pytest.mark.parametrize("mode", ["missing", "raises", "empty"])
async def test_token_failure_never_starts_and_releases_reservation(handler, mode):
    if mode == "missing":
        handler.orchestrator.token_store = None
    else:

        async def mint(**kwargs):
            if mode == "raises":
                raise RuntimeError("store unavailable")
            return ""

        handler.orchestrator.token_store.mint = mint
    assert "error" in await start(handler)
    assert provider(handler).starts == []
    assert (await handler.db.get_agent("worker-a")).state == AgentState.IDLE


async def test_unsupported_interactive_provider_is_rejected(handler):
    provider(handler).capabilities = frozenset()
    assert "error" in await start(handler)
    assert provider(handler).starts == []


async def test_start_failure_stops_only_own_launch_and_revokes_token(handler, monkeypatch):
    fake = provider(handler)
    original = fake.start
    attempted = []

    async def partial_start(spec):
        attempted.append(spec)
        await original(spec)
        raise RuntimeError("partial start failed")

    monkeypatch.setattr(fake, "start", partial_start)
    assert "error" in await start(handler)
    assert not fake.sessions
    assert await handler.orchestrator.token_store.validate(attempted[0].env["AQ_API_TOKEN"]) is None
    assert (await handler.db.get_agent("worker-a")).state == AgentState.IDLE
    assert await handler.db.list_sessions() == []


async def test_cleanup_does_not_release_newer_reservation(handler):
    assert await handler.db.reserve_idle_agent("worker-a")
    old = await handler.db.get_agent("worker-a")
    release = getattr(handler.db, "release_agent_reservation", None)
    assert release is not None, "reservation cleanup must be compare-and-set guarded"
    await handler.db.update_agent("worker-a", last_heartbeat=old.last_heartbeat + 1)
    assert not await release("worker-a", expected_heartbeat=old.last_heartbeat)
    assert (await handler.db.get_agent("worker-a")).state == AgentState.BUSY


@pytest.mark.parametrize("wake", ["resume", "fresh"])
async def test_resume_uses_only_own_named_history(handler, wake):
    result = await start(handler)
    assert "error" not in result, result
    old = await handler.db.get_session(result["session_id"])
    await provider(handler).stop(SessionHandle(old.name, old.provider, old.instance_token))
    await handler.db.update_session(old.id, state="stopped", desired_state="stopped")
    await handler.db.update_profile("coder", wake_mode=wake)
    await handler.db.create_agent(Agent(id="other", name="Other", profile_id="coder"))
    await handler.db.create_session(
        SessionRecord(
            id="other-history",
            agent_id="other",
            project_id=None,
            profile_id="coder",
            harness="claude",
            name="n-other",
            lifecycle="named",
            state="stopped",
            provider="fake",
            work_dir="/tmp/other",
            epoch="",
            instance_token="other",
            session_key="someone-elses-conversation",
            started_at=old.started_at + 100,
        )
    )
    result = await start(handler)
    assert "error" not in result, result
    new = await handler.db.get_session(result["session_id"])
    spec = provider(handler).starts[-1]
    assert new.id != old.id
    if wake == "resume":
        assert new.session_key == old.session_key and old.session_key in spec.command
    else:
        assert new.session_key == new.id and "--resume" not in spec.command
    assert "someone-elses-conversation" not in spec.command
    assert (await handler.db.get_session(old.id)).state == "stopped"


async def test_active_task_session_is_reused_without_new_launch(handler):
    await handler.db.create_project(Project(id="p", name="P"))
    await handler.db.create_task(
        Task(
            id="t",
            project_id="p",
            title="Task",
            description="",
            status=TaskStatus.IN_PROGRESS,
            assigned_agent_id="worker-a",
        )
    )
    await handler.db.update_agent("worker-a", state=AgentState.BUSY, current_task_id="t")
    profile = await handler.db.get_profile("coder")
    spec = handler.orchestrator.session_spec_builder.build_task_spec(
        task=await handler.db.get_task("t"),
        profile=profile,
        harness=handler.orchestrator.harness_registry.get("claude"),
        work_dir="/tmp/test",
        session_id="task-session",
        instance_token="task-instance",
    )
    await provider(handler).start(spec)
    await handler.db.create_session(
        SessionRecord(
            id="task-session",
            agent_id="worker-a",
            task_id="t",
            project_id="p",
            profile_id="coder",
            harness="claude",
            provider="fake",
            name=spec.session_name,
            state="running",
            instance_token="task-instance",
            work_dir="/tmp/test",
            epoch="",
            lifecycle="task",
            started_at=1,
        )
    )
    assert (await start(handler))["session_id"] == "task-session"
    assert len(provider(handler).starts) == 1


async def test_supervisor_button_and_message_wake_share_one_start_and_resume(handler):
    await handler.db.create_agent(
        Agent(
            id="supervisor-global",
            name="Supervisor",
            profile_id="supervisor",
            role="supervisor",
        )
    )
    lens = handler.orchestrator.session_lens
    result, woke = await asyncio.gather(
        start(handler, "supervisor-global"),
        lens.ensure_started(kind="session", target_id="supervisor-global", project_id=None),
    )
    assert "error" not in result and woke, result
    assert len(provider(handler).starts) == 1
    row = await handler.db.get_session(result["session_id"])
    await provider(handler).stop(SessionHandle(row.name, row.provider, row.instance_token))
    await handler.db.update_session(row.id, state="sleeping", desired_state="sleeping")
    result = await start(handler, "supervisor-global")
    assert "error" not in result, result
    new = await handler.db.get_session(result["session_id"])
    assert new.id != row.id and new.session_key == row.session_key
    assert row.session_key in provider(handler).starts[-1].command
    assert await handler.db.list_messages() == []


async def test_active_assignment_without_current_pointer_cannot_start(handler):
    await handler.db.create_project(Project(id="p", name="P"))
    await handler.db.create_task(
        Task(
            id="orphan-assignment",
            project_id="p",
            title="Task",
            description="",
            status=TaskStatus.IN_PROGRESS,
            assigned_agent_id="worker-a",
        )
    )
    assert "error" in await start(handler)
    assert provider(handler).starts == []


async def test_uncertain_stop_keeps_reservation_and_revokes_token(handler, monkeypatch):
    fake = provider(handler)
    original = fake.start

    async def broken_start(spec):
        await original(spec)
        raise RuntimeError("started before failure")

    async def broken_stop(handle, **kwargs):
        raise RuntimeError("provider disconnected")

    monkeypatch.setattr(fake, "start", broken_start)
    monkeypatch.setattr(fake, "stop", broken_stop)
    assert "error" in await start(handler)
    spec = fake.starts[0]
    assert await handler.orchestrator.token_store.validate(spec.env["AQ_API_TOKEN"]) is None
    assert (await handler.db.get_agent("worker-a")).state == AgentState.BUSY


async def test_disabled_supervisor_does_not_wake(handler):
    await handler.db.create_agent(
        Agent(
            id="supervisor-global",
            name="Supervisor",
            profile_id="supervisor",
            role="supervisor",
            enabled=False,
        )
    )
    assert "error" in await start(handler, "supervisor-global")
    assert provider(handler).starts == []


async def test_supervisor_token_mint_failure_never_launches(handler):
    await handler.db.create_agent(
        Agent(
            id="supervisor-global",
            name="Supervisor",
            profile_id="supervisor",
            role="supervisor",
        )
    )

    async def broken_mint(**kwargs):
        raise RuntimeError("token store unavailable")

    handler.orchestrator.token_store.mint = broken_mint
    assert "error" in await start(handler, "supervisor-global")
    assert provider(handler).starts == []


async def test_private_terminal_root_cannot_redirect_outside_data_dir(handler, tmp_path):
    root = Path(handler.config.data_dir) / "agent-terminals"
    root.parent.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    root.symlink_to(outside, target_is_directory=True)
    assert "error" in await start(handler)
    assert list(outside.iterdir()) == []
    assert provider(handler).starts == []


@pytest.mark.parametrize("agent_id", ["worker-a", "supervisor-global"])
async def test_native_conversation_key_is_not_invented_for_codex(handler, agent_id):
    handler.orchestrator.harness_registry.upsert(
        Harness(
            id="codex",
            command="codex",
            resume=ResumeSpec(style="subcommand"),
        )
    )
    if agent_id == "supervisor-global":
        await handler.db.create_agent(
            Agent(
                id=agent_id,
                name="Supervisor",
                profile_id="supervisor",
                role="supervisor",
            )
        )
    await handler.db.update_agent(agent_id, harness="codex")
    result = await start(handler, agent_id)
    assert "error" not in result, result
    row = await handler.db.get_session(result["session_id"])
    assert row.session_key is None
    await provider(handler).stop(SessionHandle(row.name, row.provider, row.instance_token))
    # TranscriptWatcher learns the native ID; only that ID is valid for resume.
    await handler.db.update_session(
        row.id,
        state="stopped",
        desired_state="stopped",
        session_key="native-codex-id",
    )
    result = await start(handler, agent_id)
    assert "error" not in result, result
    new = await handler.db.get_session(result["session_id"])
    assert new.session_key == "native-codex-id"
    assert "native-codex-id" in provider(handler).starts[-1].command


async def test_manual_terminal_never_spends_automatic_supervisor_restart_budget(handler):
    from src.sessions.reconciler import SessionReconciler

    result = await start(handler)
    assert "error" not in result, result
    row = await handler.db.get_session(result["session_id"])
    await provider(handler).stop(SessionHandle(row.name, row.provider, row.instance_token))
    await handler.db.update_session(row.id, state="stopped", desired_state="running")
    orch = handler.orchestrator
    reconciler = SessionReconciler(
        handler.db,
        handler.config,
        orch.session_providers,
        harnesses=orch.harness_registry,
        spec_builder=orch.session_spec_builder,
        orchestrator=orch,
    )
    reconciler.starter = orch.session_lens
    for tick in range(1, 10):
        await reconciler._converge_named_up(row.started_at + tick * 3600)
    current = await handler.db.get_session(row.id)
    assert current.state == "stopped" and current.restarts == 0
    assert len(provider(handler).starts) == 1


async def test_unknown_latest_conversation_does_not_resume_older_history(handler):
    result = await start(handler)
    old = await handler.db.get_session(result["session_id"])
    await provider(handler).stop(SessionHandle(old.name, old.provider, old.instance_token))
    await handler.db.update_session(old.id, state="stopped", desired_state="stopped")
    from dataclasses import replace

    await handler.db.create_session(
        replace(
            old,
            id="newer-unknown",
            session_key=None,
            state="stopped",
            desired_state="stopped",
            started_at=old.started_at + 1,
        )
    )
    result = await start(handler)
    assert "error" not in result, result
    assert "--resume" not in provider(handler).starts[-1].command


@pytest.mark.parametrize("paused", ["sessions", "orchestrator"])
async def test_runtime_pause_blocks_new_terminal_start(handler, paused):
    if paused == "sessions":
        handler.config.sessions.enabled = False
    else:
        handler.orchestrator._paused = True
    assert "error" in await start(handler)
    assert provider(handler).starts == []
    assert (await handler.db.get_agent("worker-a")).state == AgentState.IDLE


async def test_typed_terminal_start_enforces_scope_and_returns_flock_summary(handler):
    import httpx
    from fastapi import FastAPI
    from src.api.auth import LOCAL_SCOPE, RequestScope
    from src.api.codegen import build_category_routers
    from src.api.dependencies import get_command_handler

    app = FastAPI()
    for router in build_category_routers():
        if router.prefix == "/api/agent":
            app.include_router(router)
    app.dependency_overrides[get_command_handler] = lambda: handler
    scope = RequestScope(kind="session", session_id="project-supervisor", project_id="p", elevated=True)

    @app.middleware("http")
    async def bind_scope(request, call_next):
        request.state.scope = scope
        return await call_next(request)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        denied = await client.post("/api/agent/start-terminal", json={"agent_id": "worker-a"})
        assert denied.status_code == 403
        assert provider(handler).starts == []
        scope = LOCAL_SCOPE
        started = await client.post("/api/agent/start-terminal", json={"agent_id": "worker-a"})
        assert started.status_code == 200, started.text
        body = started.json()
        assert body["id"] == "worker-a" and body["session_id"]
        assert body["session_state"] == "running"
        assert body["settings"]["model"] == "my-model"
        assert len(provider(handler).starts) == 1
