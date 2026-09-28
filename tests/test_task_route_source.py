"""Route source, class hint, route record, router binding and the design/art kinds.

Revision 1 of the mandatory-task-routing spec (2026-09-28 §11, Task 1): every
profile write records who made it, a writer that declares no source is
stamped ``role`` or ``legacy`` by the query layer (§9.2), every new project is
bound to a router (§8), and ``design`` / ``art`` are task kinds everywhere a
kind is accepted.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import sqlalchemy as sa

from src.api.models.task import GetTaskResponse
from src.commands.handler import CommandHandler
from src.config import AppConfig, RoutingConfig, load_config
from src.database import Database
from src.database.tables import tasks
from src.models import (
    TASK_TYPE_VALUES,
    AgentProfile,
    Project,
    RepoSourceType,
    Task,
    TaskType,
    Workspace,
)
from src.routing.sources import (
    DEFAULT_ROUTER_PLAYBOOK_ID,
    LEGACY,
    ROLE,
    ROLE_PROFILE_IDS,
    ROUTE_SOURCES,
    ROUTER,
    UNROUTED,
    stamped_route_source,
)
from src.task_graph.parser import parse_graph
from src.task_graph.validator import _check_task_types
from tests.db_fixtures import lease_dsn


@pytest.fixture
async def db():
    database = Database(lease_dsn("route_source.db"))
    await database.initialize()
    await database.create_project(Project(id="p", name="P"))
    for profile_id in ("worker", "other-worker", "triage", "reviewer"):
        await database.create_profile(AgentProfile(id=profile_id, name=profile_id))
    yield database
    await database.close()


async def _source(db, task_id: str) -> str:
    async with db._engine.begin() as conn:
        return await conn.scalar(sa.select(tasks.c.route_source).where(tasks.c.id == task_id))


def _task(task_id: str, profile_id: str | None = None, **extra) -> Task:
    return Task(
        id=task_id, project_id="p", title=task_id, description=task_id,
        profile_id=profile_id, **extra,
    )


# -- the stamping rule --------------------------------------------------------


def test_the_value_set_matches_the_check_constraint():
    [check] = [
        c for c in tasks.constraints
        if isinstance(c, sa.CheckConstraint) and c.name == "ck_tasks_route_source"
    ]
    assert str(check.sqltext) == (
        "route_source IN (" + ",".join(f"'{s}'" for s in ROUTE_SOURCES) + ")"
    )
    assert ROLE_PROFILE_IDS == {"triage", "spec-ingest", "reviewer", "final-reviewer"}


@pytest.mark.parametrize(
    ("profile_id", "declared", "stored"),
    [
        (None, None, UNROUTED),
        (None, ROUTER, UNROUTED),
        ("worker", None, LEGACY),
        ("worker", UNROUTED, LEGACY),
        ("triage", None, ROLE),
        ("final-reviewer", UNROUTED, ROLE),
        ("worker", ROUTER, ROUTER),
        ("worker", "override", "override"),
    ],
)
def test_stamped_route_source(profile_id, declared, stored):
    assert stamped_route_source(profile_id, declared) == stored


# -- the query layer stamps a profile written with no source -------------------


async def test_create_task_stamps_legacy_role_or_unrouted(db):
    created = {
        "none": _task("none", intelligence_class="standard-high"),
        "worker": _task("worker-task", "worker"),
        "triage": _task("triage-task", "triage"),
        "reviewer": _task("reviewer-task", "reviewer"),
        "router": _task("router-task", "worker", route_source=ROUTER),
    }
    for task in created.values():
        await db.create_task(task)

    expected = {"none": UNROUTED, "worker": LEGACY, "triage": ROLE,
                "reviewer": ROLE, "router": ROUTER}
    for key, task in created.items():
        assert task.route_source == expected[key], key  # the caller's model follows
        assert await _source(db, task.id) == expected[key], key
        assert (await db.get_task(task.id)).route_source == expected[key], key


async def test_class_hint_and_route_round_trip_and_default_to_null(db):
    route = {"rule": "kinds.design", "candidates": ["worker"], "scores": [1.0]}
    await db.create_task(
        _task("hinted", "worker", route_source=ROUTER, class_hint="deep-high", route=route)
    )
    await db.create_task(_task("bare"))

    hinted = await db.get_task("hinted")
    assert (hinted.class_hint, hinted.route) == ("deep-high", route)
    bare = await db.get_task("bare")
    assert (bare.class_hint, bare.route, bare.route_source) == (None, None, UNROUTED)
    async with db._engine.begin() as conn:
        # A missing record is SQL NULL, not the JSON literal null.
        assert await conn.scalar(
            sa.select(sa.func.count()).select_from(tasks).where(tasks.c.route.is_(None))
        ) == 1


async def test_update_task_routing_stamps_every_write(db):
    await db.create_task(_task("t"))

    assert await db.update_task_routing(
        "t", profile_id="worker", intelligence_class="standard-high", preferred_workspace_id=None
    )
    assert await _source(db, "t") == LEGACY
    assert await db.update_task_routing(
        "t", profile_id="triage", intelligence_class=None, preferred_workspace_id=None
    )
    assert await _source(db, "t") == ROLE
    assert await db.update_task_routing(
        "t", profile_id="worker", intelligence_class=None, preferred_workspace_id=None,
        route_source=ROUTER,
    )
    assert await _source(db, "t") == ROUTER
    assert await db.update_task_routing(
        "t", profile_id=None, intelligence_class=None, preferred_workspace_id=None
    )
    assert await _source(db, "t") == UNROUTED


async def test_update_task_with_a_profile_stamps_it(db):
    await db.create_task(_task("t"))
    await db.update_task("t", profile_id="worker")
    assert await _source(db, "t") == LEGACY
    await db.update_task("t", profile_id="worker", route_source=ROUTER)
    assert await _source(db, "t") == ROUTER
    await db.update_task("t", priority=5)  # no profile write, no source change
    assert await _source(db, "t") == ROUTER


async def test_reroute_and_undo_keep_the_source(db):
    await db.create_task(_task("legacy", "worker"))
    await db.create_task(_task("routed", "worker", route_source=ROUTER))
    record = {"project_id": "p", "reason_code": "provider_unavailable"}

    for task_id in ("legacy", "routed"):
        assert await db.apply_task_reroute(
            task_id, expected_profile_id="worker", to_profile_id="other-worker", record=record
        )
    assert await _source(db, "legacy") == LEGACY
    assert await _source(db, "routed") == ROUTER

    assert await db.undo_task_reroute("routed", record={"actor": "operator"})
    task = await db.get_task("routed")
    assert (task.profile_id, task.route_source) == ("worker", ROUTER)


async def test_reroute_stamps_a_row_written_without_a_source(db):
    # A writer that bypassed the query layer left a profile marked unrouted.
    await db.create_task(_task("raw"))
    async with db._engine.begin() as conn:
        await conn.execute(sa.update(tasks).where(tasks.c.id == "raw").values(profile_id="worker"))
    assert await _source(db, "raw") == UNROUTED

    assert await db.apply_task_reroute(
        "raw", expected_profile_id="worker", to_profile_id="other-worker",
        record={"project_id": "p", "reason_code": "capacity_spill"},
    )
    assert await _source(db, "raw") == LEGACY


async def test_the_database_refuses_an_unknown_source(db):
    await db.create_task(_task("t"))
    with pytest.raises(sa.exc.IntegrityError):
        async with db._engine.begin() as conn:
            await conn.execute(
                sa.update(tasks).where(tasks.c.id == "t").values(route_source="explicit")
            )


# -- router binding -----------------------------------------------------------


async def test_project_inserts_fall_back_to_the_default_router(db, tmp_path):
    await db.create_project(Project(id="unbound", name="U", assignment_playbook_id=None))
    await db.create_project(Project(id="modelled", name="M"))
    await db.register_onboarded_project(
        Project(id="onboarded", name="O", assignment_playbook_id=None),
        Workspace(
            id="w", project_id="onboarded", workspace_path=str(tmp_path / "repo"),
            source_type=RepoSourceType.LINK, kind_id="project-repo",
        ),
    )
    await db.create_project(Project(id="custom", name="C", assignment_playbook_id="my-router"))

    bindings = {
        pid: (await db.get_project(pid)).assignment_playbook_id
        for pid in ("unbound", "modelled", "onboarded", "custom")
    }
    assert bindings == {
        "unbound": DEFAULT_ROUTER_PLAYBOOK_ID,
        "modelled": DEFAULT_ROUTER_PLAYBOOK_ID,
        "onboarded": DEFAULT_ROUTER_PLAYBOOK_ID,
        "custom": "my-router",
    }


def test_routing_config_loads_and_validates(tmp_path):
    assert AppConfig().routing.default_router == "default-assignment-routing"
    path = tmp_path / "config.yaml"
    path.write_text(
        "database:\n  url: postgresql://x/y\n"
        "discord:\n  bot_token: t\n  guild_id: '1'\n"
        "routing:\n  default_router: project-router\n",
        encoding="utf-8",
    )
    assert load_config(str(path)).routing.default_router == "project-router"
    [error] = RoutingConfig(default_router=" ").validate()
    assert (error.section, error.field) == ("routing", "default_router")


# -- the design and art kinds -------------------------------------------------


def test_design_and_art_are_task_kinds():
    assert {TaskType.DESIGN.value, TaskType.ART.value} == {"design", "art"}
    assert {"design", "art"} <= TASK_TYPE_VALUES


def test_cli_choices_are_every_task_kind():
    from src.cli.styles import TASK_TYPES

    assert set(TASK_TYPES) == TASK_TYPE_VALUES


@pytest.mark.parametrize("kind", ["design", "art"])
def test_cli_task_create_forwards_the_kind(kind):
    from click.testing import CliRunner

    from src.cli.app import cli

    captured: dict = {}

    async def execute(name, args=None):
        if name == "create_task":
            captured.update(args or {})
            return {"created": "task-1", "title": "T"}
        return {}

    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.execute = AsyncMock(side_effect=execute)
    base = ["task", "create", "--project", "p", "--title", "T", "--description", "D"]
    with patch("src.cli.tasks._get_client", return_value=client):
        result = CliRunner().invoke(cli, [*base, "--type", kind])
        refused = CliRunner().invoke(cli, [*base, "--type", "sketch"])
    assert result.exit_code == 0, result.output
    assert captured["task_type"] == kind
    assert refused.exit_code == 2 and "sketch" in refused.output


def test_tool_schemas_offer_design_and_art():
    from src.tools.definitions import _ALL_TOOL_DEFINITIONS

    schemas = {tool["name"]: tool["input_schema"] for tool in _ALL_TOOL_DEFINITIONS}
    for name in ("create_task", "edit_task"):
        assert {"design", "art"} <= set(schemas[name]["properties"]["task_type"]["enum"]), name


@pytest.mark.parametrize("kind", ["design", "art"])
def test_graph_validator_accepts_the_kinds(kind):
    graph = parse_graph(
        json.dumps({"version": 1, "nodes": [{"key": "a", "title": "A", "task_type": kind}]})
    )
    assert _check_task_types(graph) == []
    bogus = parse_graph(
        json.dumps({"version": 1, "nodes": [{"key": "a", "title": "A", "task_type": "sketch"}]})
    )
    assert [e.rule for e in _check_task_types(bogus)] == ["bad_task_type"]


@pytest.fixture
async def handler(db, tmp_path):
    orch = MagicMock()
    orch.db = db
    orch._emit_notify = AsyncMock()
    config = MagicMock()
    config.vault_root = str(tmp_path / "vault")
    handler = CommandHandler(orch, config)
    handler._active_project_id = None
    return handler


@pytest.mark.parametrize("kind", ["design", "art"])
async def test_kinds_round_trip_through_create_task_and_the_api_model(handler, db, kind):
    created = await handler._cmd_create_task(
        {"project_id": "p", "title": f"{kind} work", "task_type": kind}
    )
    assert created.get("task_type") == kind, created
    task_id = created["task_id"]
    assert (await db.get_task(task_id)).task_type is TaskType(kind)

    shown = await handler._cmd_get_task({"task_id": task_id})
    detail = GetTaskResponse.model_validate(shown)
    assert detail.task_type == kind
    assert detail.route_source in ROUTE_SOURCES


async def test_a_graph_node_carries_a_kind_and_a_stamped_source(handler, db):
    result = await handler._cmd_create_task_graph({
        "project_id": "p",
        "graph": {
            "version": 1,
            "parent": {"title": "Epic"},
            "nodes": [
                {"key": "d", "title": "D", "acceptance": ["x"], "task_type": "design",
                 "profile": "worker"},
                {"key": "a", "title": "A", "acceptance": ["x"], "task_type": "art"},
            ],
        },
    })
    assert "error" not in result, result
    ids = {node["key"]: node["task_id"] for node in result["nodes"]}
    design, art = await db.get_task(ids["d"]), await db.get_task(ids["a"])
    assert (design.task_type, design.route_source) == (TaskType.DESIGN, LEGACY)
    assert art.task_type is TaskType.ART
