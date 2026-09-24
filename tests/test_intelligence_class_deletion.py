"""Retiring an intelligence class preserves the vault source and live routing safety."""

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from src.api.codegen import build_category_routers
from src.api.dependencies import get_command_handler
from src.api.scope import check_command_scope
from src.api.auth import RequestScope
from src.commands.handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.event_bus import EventBus
from src.intelligence_classes import load_intelligence_classes
from src.intelligence_classes.registry import IntelligenceClassRegistry
from src.models import Agent, AgentProfile, Project, Task, TaskStatus
from src.sessions.spec import SessionSpecBuilder
from src.vault import ensure_default_intelligence_classes
from tests.db_fixtures import lease_dsn


@pytest.fixture
async def env(tmp_path):
    config = AppConfig(
        discord=DiscordConfig(bot_token="test", guild_id="1"), data_dir=str(tmp_path / "data")
    )
    ensure_default_intelligence_classes(config.data_dir)
    db = Database(lease_dsn("class-delete.db"))
    await db.initialize()
    registry = IntelligenceClassRegistry(load_intelligence_classes(config.data_dir))
    builder = SessionSpecBuilder(config, intelligence_classes=registry)
    orch = SimpleNamespace(
        db=db, bus=EventBus(validate_events=False), intelligence_classes=registry,
        session_spec_builder=builder,
    )
    handler = CommandHandler(orch, config)
    handler.set_active_project(None)
    yield handler, db, registry
    await db.close()


def class_path(handler, class_id="fast-low"):
    return Path(handler.config.data_dir) / "vault" / "intelligence-classes" / f"{class_id}.md"


async def test_unreferenced_class_retires_and_reload_removes_it_without_restart(env):
    handler, _, registry = env
    path = class_path(handler)
    original = path.read_bytes()
    revision = registry["fast-low"].revision
    result = await handler._cmd_delete_intelligence_class(
        {"class_id": "fast-low", "expected_revision": revision}
    )
    assert result == {"success": True, "class_id": "fast-low", "retired_file": "fast-low.md.retired"}
    assert not path.exists()
    assert path.with_name("fast-low.md.retired").read_bytes() == original
    assert "fast-low" not in registry
    assert "fast-low" not in handler.orchestrator.session_spec_builder._intelligence_classes
    assert "fast-low" not in {row["id"] for row in (await handler._cmd_list_intelligence_classes({}))["classes"]}
    assert "fast-low.md" in ensure_default_intelligence_classes(handler.config.data_dir)["skipped"]
    assert not path.exists(), "default seeding must not revive a retired bundled class"


async def test_every_active_reference_is_named_and_terminal_tasks_do_not_block(env):
    handler, db, registry = env
    await db.create_project(Project(id="project", name="Project"))
    await db.create_profile(AgentProfile(
        id="pool-worker", name="Pool worker", lifecycle="pool", default_class="fast-low",
    ))
    await db.create_profile(AgentProfile(
        id="task-worker", name="Task worker", lifecycle="task", default_class="fast-low",
    ))
    await db.create_agent(Agent(
        id="agent-one", name="Agent one", profile_id="pool-worker", intelligence_class="fast-low",
    ))
    await db.create_task(Task(
        id="pending", project_id="project", title="Pending work", description="",
        intelligence_class="fast-low",
    ))
    await db.create_task(Task(
        id="done", project_id="project", title="Finished work", description="",
        status=TaskStatus.COMPLETED, intelligence_class="fast-low",
    ))
    result = await handler._cmd_delete_intelligence_class({"class_id": "fast-low"})
    assert result["error_code"] == "class_referenced"
    assert {(r["kind"], r["id"]) for r in result["references"]} == {
        ("agent", "agent-one"), ("profile", "pool-worker"),
        ("profile", "task-worker"), ("task", "pending"),
    }
    assert {r.get("lifecycle") for r in result["references"] if r["kind"] == "profile"} == {"pool", "task"}
    assert "agent-one" in result["error"] and "pending" in result["error"]
    assert "aq agent edit-profile" in result["error"] and "aq task edit" in result["error"]
    assert class_path(handler).exists() and "fast-low" in registry


async def test_revision_conflict_and_retired_copy_preserve_source(env):
    handler, _, registry = env
    path = class_path(handler)
    stale = await handler._cmd_delete_intelligence_class(
        {"class_id": "fast-low", "expected_revision": "0" * 64}
    )
    assert stale["error_code"] == "revision_conflict"
    assert stale["current_revision"] == registry["fast-low"].revision
    retired = path.with_name("fast-low.md.retired")
    retired.write_text("do not overwrite")
    collision = await handler._cmd_delete_intelligence_class({"class_id": "fast-low"})
    assert "retired copy" in collision["error"].lower()
    assert path.exists() and retired.read_text() == "do not overwrite"


async def test_unsynced_vault_profile_blocks_deletion(env):
    handler, _, registry = env
    profile = Path(handler.config.data_dir) / "vault" / "agent-types" / "unsynced" / "profile.md"
    profile.parent.mkdir(parents=True)
    profile.write_text(
        '---\nid: unsynced\nname: Unsynced worker\n---\n## Config\n```json\n'
        '{"default_class":"fast-low","lifecycle":"pool"}\n```\n'
    )
    result = await handler._cmd_delete_intelligence_class({"class_id": "fast-low"})
    assert result["error_code"] == "class_referenced"
    assert result["references"] == [{
        "kind": "profile", "id": "unsynced", "name": "Unsynced worker",
        "lifecycle": "pool", "source": "agent-types/unsynced/profile.md",
    }]
    assert class_path(handler).exists() and "fast-low" in registry


async def test_api_and_scope_use_the_same_delete_command(env):
    handler, db, _ = env
    worker = RequestScope(kind="session", session_id="worker", project_id="project")
    assert "global admin" in check_command_scope(
        "delete_intelligence_class", {"class_id": "fast-low"}, worker,
    )
    app = FastAPI()
    for router in build_category_routers():
        if router.prefix == "/api/system":
            app.include_router(router)
    app.dependency_overrides[get_command_handler] = lambda: handler
    spec = app.openapi()
    assert "/api/system/delete-intelligence-class" in spec["paths"]
    assert "409" in spec["paths"]["/api/system/delete-intelligence-class"]["post"]["responses"]
    await db.create_profile(AgentProfile(
        id="held-profile", name="Held profile", default_class="fast-low",
    ))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        blocked = await client.post(
            "/api/system/delete-intelligence-class", json={"class_id": "fast-low"},
        )
        assert blocked.status_code == 409
        assert blocked.json()["references"][0]["id"] == "held-profile"
        await db.update_profile("held-profile", default_class="")
        response = await client.post(
            "/api/system/delete-intelligence-class", json={"class_id": "fast-low"},
        )
    assert response.status_code == 200 and response.json()["class_id"] == "fast-low"
