"""Behavior of the global, persistent agent roster and individual settings."""
from types import SimpleNamespace

import pytest

from src.api.auth import RequestScope
from src.api.scope import check_command_scope
from src.commands.handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.event_bus import EventBus
from src.intelligence_classes import IntelligenceClass
from src.models import Agent, AgentProfile, Project
from src.sessions.harness_parser import Harness
from src.sessions.harness_registry import HarnessRegistry
from src.sessions.spec import SessionSpecBuilder


@pytest.fixture
async def handler(tmp_path):
    db = Database(str(tmp_path / "flock.db"))
    await db.initialize()
    config = AppConfig(
        discord=DiscordConfig(bot_token="test", guild_id="1"),
        workspace_dir=str(tmp_path / "work"),
        data_dir=str(tmp_path / "data"),
    )
    registry = HarnessRegistry()
    registry.upsert(Harness(id="claude", name="Claude", command="claude"))
    registry.upsert(Harness(id="codex", name="Codex", command="codex"))
    # A profile carries an intelligence class, never a model pin: the class and
    # the harness resolve the launch model.
    classes = {
        "standard-medium": IntelligenceClass(
            id="standard-medium", name="Standard", description="",
            mapping={"anthropic": {"model": "profile-model"}},
        )
    }
    orchestrator = SimpleNamespace(
        db=db, bus=EventBus(validate_events=False), harness_registry=registry,
        session_spec_builder=SessionSpecBuilder(config, intelligence_classes=classes),
    )
    await db.create_profile(AgentProfile(
        id="coder", name="Coder", harness="claude", default_class="standard-medium",
    ))
    result = CommandHandler(orchestrator, config)
    result.set_active_project(None)
    yield result
    result.set_active_project(None)
    result._current_scope = None
    await db.close()


async def test_list_is_global_without_any_project(handler):
    await handler.db.create_agent(Agent(id="worker-a", name="Ada", profile_id="coder"))
    result = await handler._cmd_list_agents({})
    assert "error" not in result
    assert [row["id"] for row in result["agents"]] == ["worker-a"]
    assert result["agents"][0]["provider"] == "anthropic"
    assert result["agents"][0]["model"] == "profile-model"
    assert result["agents"][0]["session_id"] is None
    assert await handler.db.list_projects() == []


async def test_active_project_does_not_hide_global_roster(handler):
    await handler.db.create_project(Project(id="p", name="Project"))
    await handler.db.create_agent(Agent(id="worker-a", name="Ada", profile_id="coder"))
    handler.set_active_project("p")
    result = await handler._cmd_list_agents({})
    assert [row["id"] for row in result["agents"]] == ["worker-a"]


async def test_create_defines_a_worker_without_launching_a_session(handler):
    create = getattr(handler, "_cmd_create_agent", None)
    assert create is not None, "global workers need a create command"
    result = await create({
        "name": "Lin", "profile_id": "coder", "harness": "codex",
        "model": "worker-model",
    })
    assert "error" not in result
    assert result["provider"] == "openai"
    assert result["model"] == "worker-model"
    assert result["settings"]["harness"] == "codex"
    assert await handler.db.list_sessions() == []
    assert (await handler.db.get_profile("coder")).harness == "claude"


async def test_edit_does_not_mutate_shared_profile(handler):
    edit = getattr(handler, "_cmd_edit_agent", None)
    assert edit is not None, "each worker needs independent settings"
    await handler.db.create_agent(Agent(id="a", name="Ada", profile_id="coder"))
    result = await edit({"agent_id": "a", "model": "personal-model", "name": "Ada 2"})
    assert "error" not in result
    assert result["model"] == "personal-model"
    assert result["name"] == "Ada 2"
    assert (await handler.db.get_profile("coder")).default_class == "standard-medium"


@pytest.mark.parametrize("command", ["create_agent", "edit_agent", "delete_agent", "start_agent_terminal"])
def test_project_supervisor_cannot_change_global_worker_settings(command):
    scope = RequestScope(kind="session", session_id="s", project_id="p", elevated=True)
    error = check_command_scope(command, {"agent_id": "a"}, scope)
    assert error and "global" in error


@pytest.mark.parametrize("command", ["create_agent", "edit_agent", "delete_agent", "start_agent_terminal"])
def test_global_supervisor_can_change_worker_settings(command):
    scope = RequestScope(kind="session", session_id="s", project_id=None, elevated=True)
    assert check_command_scope(command, {}, scope) is None

def test_cli_preserves_the_global_agent_id():
    from src.cli.adapters import agent_proxy
    proxy = agent_proxy({"id": "worker-a", "workspace_id": None, "name": "Ada", "state": "idle"})
    assert proxy.id == "worker-a"


async def test_invalid_agent_settings_do_not_change_worker(handler):
    await handler.db.create_agent(Agent(id="a", name="Ada", profile_id="coder"))
    result = await handler._cmd_edit_agent({"agent_id": "a", "harness": "missing"})
    assert "error" in result
    assert (await handler.db.get_agent("a")).harness is None
    result = await handler._cmd_edit_agent({"agent_id": "a", "intelligence_class": "typo"})
    assert "error" in result
    assert (await handler.db.get_agent("a")).intelligence_class is None


async def test_direct_command_enforces_global_settings_scope(handler):
    await handler.db.create_agent(Agent(id="a", name="Ada", profile_id="coder"))
    handler._current_scope = {"kind": "session", "elevated": True, "project_id": "p"}
    result = await handler._cmd_edit_agent({"agent_id": "a", "name": "Changed"})
    assert "global admin" in result["error"]
    assert (await handler.db.get_agent("a")).name == "Ada"


async def test_supervisor_registration_is_idempotent_and_does_not_create_project(handler):
    from src.agents.configuration import ensure_supervisor_agent
    one = await ensure_supervisor_agent(handler.db)
    two = await ensure_supervisor_agent(handler.db)
    assert one.id == two.id == "supervisor-global"
    assert len(await handler.db.list_agents()) == 1
    assert one.role == "supervisor"
    assert await handler.db.list_projects() == []


def test_worker_override_controls_real_launch_and_keeps_source_profile(tmp_path):
    from src.agents.configuration import apply_agent_overrides, resolve_launch_settings
    from src.intelligence_classes import IntelligenceClass
    profile = AgentProfile(id="coder", name="Coder", harness="claude", model="profile-model")
    agent = Agent(
        id="a", name="Ada", profile_id="coder", model="worker-model",
        intelligence_class="deep", harness="codex",
    )
    cfg = AppConfig(discord=DiscordConfig(bot_token="t", guild_id="1"), data_dir=str(tmp_path))
    builder = SessionSpecBuilder(cfg, intelligence_classes={
        "fast": IntelligenceClass(id="fast", name="Fast", description="", mapping={"openai": {"model": "fast-model"}}),
        "deep": IntelligenceClass(id="deep", name="Deep", description="", mapping={"openai": {"model": "deep-model"}}),
    })
    effective = apply_agent_overrides(profile, agent)
    harness = Harness(id="codex", name="Codex", command="codex", model_flag="--model")
    argv = builder._compose_argv(
        harness=harness, profile=effective, session_id="s", resume_key=None,
        prompt=None, session_name="s", files=[], task_intelligence_class="fast",
    )
    assert argv[argv.index("--model") + 1] == "worker-model"
    assert resolve_launch_settings(effective, harness, builder, "fast") == {
        "llm_provider": "openai", "model": "worker-model", "intelligence_class": "deep",
    }
    assert profile.model == "profile-model"
    assert profile.harness == "claude"
    agent.model = None
    effective = apply_agent_overrides(profile, agent)
    assert builder._resolve_model(effective, harness, "fast") == "deep-model"


async def test_settings_edit_keeps_live_snapshot_and_exact_session(handler):
    from src.models import SessionRecord
    await handler.db.create_agent(Agent(id="a", name="Ada", profile_id="coder"))
    await handler.db.create_agent(Agent(id="b", name="Adaline", profile_id="coder"))
    await handler.db.create_session(SessionRecord(
        id="s", project_id=None, profile_id="coder", harness="claude", provider="tmux",
        name="not-related-to-agent-name", lifecycle="named", work_dir="/tmp", epoch="e",
        instance_token="token", started_at=1, agent_id="a", state="running",
        llm_provider="anthropic", model="running-model", intelligence_class="standard",
    ))
    result = await handler._cmd_edit_agent({"agent_id": "a", "model": "next-model"})
    assert result["session_id"] == "s"
    assert result["model"] == "running-model"
    assert result["settings"]["model"] == "next-model"
    other = await handler._cmd_get_agent({"agent_id": "b"})
    assert other["session_id"] is None


async def test_stale_session_cannot_claim_another_workers_current_task(handler):
    from src.models import AgentState, SessionRecord, Task, TaskStatus
    await handler.db.create_project(Project(id="p", name="Project"))
    await handler.db.create_agent(Agent(id="a", name="Ada", profile_id="coder"))
    await handler.db.create_agent(Agent(id="b", name="Bea", profile_id="coder"))
    await handler.db.create_task(Task(
        id="task", project_id="p", title="Bea's work", description="Work",
        status=TaskStatus.IN_PROGRESS, assigned_agent_id="b",
    ))
    await handler.db.update_agent("b", state=AgentState.BUSY, current_task_id="task")
    await handler.db.create_session(SessionRecord(
        id="stale", project_id="p", profile_id="coder", harness="claude", provider="tmux",
        name="old", lifecycle="task", work_dir="/tmp", epoch="e", instance_token="t",
        started_at=1, agent_id="a", task_id="task", state="running",
    ))
    row = await handler._cmd_get_agent({"agent_id": "a"})
    assert row["current_task_id"] is None
    assert row["session_id"] is None


async def test_active_legacy_session_wins_over_linked_history(handler):
    from src.models import AgentState, SessionRecord, Task, TaskStatus
    await handler.db.create_project(Project(id="p", name="Project"))
    await handler.db.create_agent(Agent(id="a", name="Ada", profile_id="coder"))
    await handler.db.create_task(Task(
        id="task", project_id="p", title="Current work", description="Work",
        status=TaskStatus.IN_PROGRESS, assigned_agent_id="a",
    ))
    await handler.db.update_agent("a", state=AgentState.BUSY, current_task_id="task")
    common = dict(project_id="p", profile_id="coder", harness="claude", provider="tmux",
                  lifecycle="task", work_dir="/tmp", epoch="e", instance_token="t")
    await handler.db.create_session(SessionRecord(
        id="history", name="old", started_at=1, agent_id="a", state="stopped", **common,
    ))
    await handler.db.create_session(SessionRecord(
        id="current", name="active", started_at=2, task_id="task", state="running", **common,
    ))
    row = await handler._cmd_get_agent({"agent_id": "a"})
    assert row["session_id"] == "current"
    assert row["current_task_id"] == "task"


def _flock_api(handler, scope=None):
    from fastapi import FastAPI
    from src.api.auth import LOCAL_SCOPE
    from src.api.codegen import build_category_routers
    from src.api.dependencies import get_command_handler

    app = FastAPI()
    for router in build_category_routers():
        if router.prefix == "/api/agent":
            app.include_router(router)
    app.dependency_overrides[get_command_handler] = lambda: handler

    @app.middleware("http")
    async def bind_scope(request, call_next):
        request.state.scope = scope or LOCAL_SCOPE
        return await call_next(request)

    return app


async def test_typed_api_creates_edits_and_lists_global_agents(handler):
    import httpx
    app = _flock_api(handler)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post("/api/agent/create", json={"name": "Ada", "profile_id": "coder"})
        assert created.status_code == 200, created.text
        agent_id = created.json()["id"]
        edited = await client.post("/api/agent/edit", json={"agent_id": agent_id, "model": "new-model"})
        assert edited.status_code == 200, edited.text
        assert edited.json()["settings"]["model"] == "new-model"
        listed = await client.post("/api/agent/list", json={})
        assert listed.status_code == 200, listed.text
        assert [row["id"] for row in listed.json()["agents"]] == [agent_id]
        assert listed.json()["agents"][0]["model"] == "new-model"


async def test_project_scoped_api_cannot_read_another_projects_assignment(handler):
    import httpx
    from src.models import AgentState, Task, TaskStatus
    for project_id in ("p", "q"):
        await handler.db.create_project(Project(id=project_id, name=project_id))
    await handler.db.create_agent(Agent(id="q-worker", name="Q Worker", profile_id="coder"))
    await handler.db.create_task(Task(
        id="q-task", project_id="q", title="Private Q task", description="Private",
        status=TaskStatus.IN_PROGRESS, assigned_agent_id="q-worker",
    ))
    await handler.db.update_agent("q-worker", state=AgentState.BUSY, current_task_id="q-task")
    scope = RequestScope(kind="session", session_id="s", project_id="p", elevated=True)
    app = _flock_api(handler, scope)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        listed = await client.post("/api/agent/list", json={})
        assert listed.status_code == 200, listed.text
        assert listed.json()["agents"] == []
        detail = await client.post("/api/agent/get", json={"agent_id": "q-worker"})
        assert detail.status_code == 422
        assert "Private Q task" not in detail.text
        edited = await client.post("/api/agent/edit", json={"agent_id": "q-worker", "name": "No"})
        assert edited.status_code == 403
    assert (await handler.db.get_agent("q-worker")).name == "Q Worker"



@pytest.mark.parametrize("settings", [{"enabled": False}, {"role": "supervisor"}])
async def test_pool_cannot_claim_new_work_for_ineligible_agent(handler, settings):
    from src.models import Task, TaskStatus
    await handler.db.create_project(Project(id="p", name="Project"))
    await handler.db.create_agent(Agent(id="a", name="Ada", profile_id="coder", **settings))
    await handler.db.create_task(Task(
        id="next", project_id="p", title="Next task", description="",
        status=TaskStatus.READY,
    ))
    async with handler.db.immediate() as conn:
        taken = await handler.db.take_task(conn, "next", agent_id="a", now=1)
    assert taken is None
    assert (await handler.db.get_task("next")).status == TaskStatus.READY
    assert (await handler.db.get_task("next")).assigned_agent_id is None


async def test_disabled_but_live_worker_keeps_its_runtime_state(handler):
    from src.models import SessionRecord
    await handler.db.create_agent(Agent(id="a", name="Ada", profile_id="coder", enabled=False))
    await handler.db.create_session(SessionRecord(
        id="pool-a", project_id=None, profile_id="coder", agent_id="a",
        harness="claude", provider="tmux", name="pool-a", lifecycle="pool",
        state="running", work_dir="/work", epoch="e", instance_token="test",
        started_at=1,
    ))
    row = await handler._cmd_get_agent({"agent_id": "a"})
    assert row["state"] == "running"
    assert row["enabled"] is False
    assert row["session_id"] == "pool-a"


async def test_delete_removes_idle_agent_from_flock_but_retains_identity(handler):
    await handler.db.create_agent(Agent(id="a", name="Ada", profile_id="coder"))
    delete = getattr(handler, "_cmd_delete_agent", None)
    assert delete is not None, "the flock needs an explicit delete command"
    result = await delete({"agent_id": "a"})
    assert result == {"deleted": "a", "name": "Ada"}
    assert (await handler.db.get_agent("a")).deleted_at is not None
    assert (await handler._cmd_list_agents({}))["agents"] == []
    assert "error" in await handler._cmd_get_agent({"agent_id": "a"})
    assert "error" in await handler._cmd_edit_agent({"agent_id": "a", "enabled": True})


async def test_delete_protects_supervisor_and_busy_worker(handler):
    from src.models import AgentState
    from src.agents.configuration import ensure_supervisor_agent
    await ensure_supervisor_agent(handler.db)
    await handler.db.create_agent(Agent(id="a", name="Ada", profile_id="coder", state=AgentState.BUSY))
    delete = getattr(handler, "_cmd_delete_agent", None)
    assert delete is not None
    assert "supervisor" in (await delete({"agent_id": "supervisor-global"}))["error"].lower()
    assert "idle" in (await delete({"agent_id": "a"}))["error"].lower()


async def test_typed_delete_requires_global_scope_and_returns_agent_identity(handler):
    import httpx
    await handler.db.create_agent(Agent(id="a", name="Ada", profile_id="coder"))
    scoped_app = _flock_api(handler, RequestScope(
        kind="session", session_id="project-supervisor", project_id="p", elevated=True,
    ))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=scoped_app), base_url="http://test") as client:
        denied = await client.post("/api/agent/delete", json={"agent_id": "a"})
        assert denied.status_code == 403
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_flock_api(handler)), base_url="http://test") as client:
        deleted = await client.post("/api/agent/delete", json={"agent_id": "a"})
        assert deleted.status_code == 200
        assert deleted.json() == {"deleted": "a", "name": "Ada"}
