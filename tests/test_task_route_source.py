"""Route source, class hint, route record, router binding and the design/art kinds.

Revision 1 of the mandatory-task-routing spec (2026-09-28 §11, Task 1): every
profile write records who made it, every new project is bound to a router
(§8), and ``design`` / ``art`` are task kinds everywhere a kind is accepted.
Revision 2 (Task 6) ends the transitional stamping: a profile write that
declares no source is refused, and ``ck_tasks_route_source_profile`` holds
``(profile_id IS NULL) = (route_source = 'unrouted')`` at the database.
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
    UndeclaredRouteSource,
    declared_route_source,
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
        ("worker", ROUTER, ROUTER),
        ("worker", "override", "override"),
        ("worker", LEGACY, LEGACY),
        ("triage", ROLE, ROLE),
    ],
)
def test_declared_route_source(profile_id, declared, stored):
    assert declared_route_source(profile_id, declared) == stored


@pytest.mark.parametrize(
    ("profile_id", "declared"),
    [("worker", None), ("worker", UNROUTED), ("triage", None), ("final-reviewer", UNROUTED)],
)
def test_a_profile_without_a_declared_source_is_refused(profile_id, declared):
    with pytest.raises(UndeclaredRouteSource, match=profile_id):
        declared_route_source(profile_id, declared)


# -- the query layer stores the declared source, and refuses none --------------


async def test_create_task_stores_the_declared_source(db):
    created = {
        "none": _task("none", intelligence_class="standard-high"),
        "legacy": _task("legacy-task", "worker", route_source=LEGACY),
        "role": _task("triage-task", "triage", route_source=ROLE),
        "router": _task("router-task", "worker", route_source=ROUTER),
    }
    for task in created.values():
        await db.create_task(task)

    expected = {"none": UNROUTED, "legacy": LEGACY, "role": ROLE, "router": ROUTER}
    for key, task in created.items():
        assert task.route_source == expected[key], key  # the caller's model follows
        assert await _source(db, task.id) == expected[key], key
        assert (await db.get_task(task.id)).route_source == expected[key], key


async def test_create_task_refuses_a_profile_with_no_source(db):
    with pytest.raises(UndeclaredRouteSource):
        await db.create_task(_task("sourceless", "worker"))
    assert await db.get_task("sourceless") is None


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


async def test_update_task_routing_writes_the_declared_source(db):
    await db.create_task(_task("t"))

    with pytest.raises(UndeclaredRouteSource):
        await db.update_task_routing(
            "t", profile_id="worker", intelligence_class="standard-high",
            preferred_workspace_id=None,
        )
    assert await _source(db, "t") == UNROUTED
    assert await db.update_task_routing(
        "t", profile_id="triage", intelligence_class=None, preferred_workspace_id=None,
        route_source=ROLE,
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


async def test_update_task_with_a_profile_needs_a_source(db):
    await db.create_task(_task("t"))
    with pytest.raises(UndeclaredRouteSource):
        await db.update_task("t", profile_id="worker")
    await db.update_task("t", profile_id="worker", route_source=ROUTER)
    assert await _source(db, "t") == ROUTER
    await db.update_task("t", priority=5)  # no profile write, no source change
    assert await _source(db, "t") == ROUTER
    await db.update_task("t", profile_id=None)
    assert await _source(db, "t") == UNROUTED


async def test_reroute_and_undo_keep_the_source(db):
    await db.create_task(_task("legacy", "worker", route_source=LEGACY))
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


@pytest.mark.parametrize(
    "values",
    [
        {"profile_id": "worker"},  # a profile left unrouted
        {"route_source": ROUTER},  # a source with no profile
    ],
)
async def test_the_database_refuses_a_route_without_both_halves(db, values):
    """``ck_tasks_route_source_profile`` catches a raw write the query layer never sees."""
    await db.create_task(_task("raw"))
    with pytest.raises(sa.exc.IntegrityError, match="ck_tasks_route_source_profile"):
        async with db._engine.begin() as conn:
            await conn.execute(sa.update(tasks).where(tasks.c.id == "raw").values(**values))
    assert await _source(db, "raw") == UNROUTED


def test_the_profile_rule_is_declared_on_the_table():
    [check] = [
        c for c in tasks.constraints
        if isinstance(c, sa.CheckConstraint) and c.name == "ck_tasks_route_source_profile"
    ]
    assert str(check.sqltext) == "(profile_id IS NULL) = (route_source = 'unrouted')"


async def test_delete_profile_sends_its_tasks_back_to_the_router(db):
    """Spec §9.2: the lost route moves to ``route.legacy``; the check holds."""
    constraints = {"constraints": {"exclude_providers": ["codex"]}}
    await db.create_task(_task(
        "queued", "worker", route_source=ROUTER, intelligence_class="standard-high",
        provider_intent="pinned", route=constraints,
    ))
    await db.create_task(_task("kept", "other-worker", route_source=LEGACY))
    await db.create_task(_task("unrouted"))

    await db.delete_profile("worker")

    queued = await db.get_task("queued")
    assert (queued.profile_id, queued.route_source, queued.provider_intent) == (
        None, UNROUTED, "class_only",
    )
    assert queued.route == {
        **constraints,
        "legacy": {
            "profile_id": "worker",
            "intelligence_class": "standard-high",
            "provider_intent": "pinned",
        },
    }
    kept = await db.get_task("kept")
    assert (kept.profile_id, kept.route_source, kept.route) == ("other-worker", LEGACY, None)
    assert (await db.get_task("unrouted")).route is None


async def test_reset_task_route_keeps_constraints_and_legacy(db):
    route = {
        "candidates": [{"profile_id": "worker"}],
        "constraints": {"exclude_providers": ["claude"]},
        "legacy": {"profile_id": "old"},
    }
    await db.create_task(_task("t", "worker", route_source=ROUTER, route=route))
    await db.create_task(_task("bare", "worker", route_source=ROUTER, route={"rule": "x"}))

    assert await db.reset_task_route("t")
    assert await db.reset_task_route("bare")

    task = await db.get_task("t")
    assert (task.profile_id, task.route_source) == (None, UNROUTED)
    assert task.route == {
        "constraints": {"exclude_providers": ["claude"]},
        "legacy": {"profile_id": "old"},
    }
    assert (await db.get_task("bare")).route is None


async def test_reset_task_route_can_require_a_source_and_a_queued_status(db):
    await db.create_task(_task("legacy", "worker", route_source=LEGACY))
    await db.create_task(_task("routed", "worker", route_source=ROUTER))
    assert not await db.reset_task_route("legacy", route_source=ROUTER)
    assert await db.reset_task_route("routed", route_source=ROUTER, queued_only=True)
    assert await _source(db, "legacy") == LEGACY


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


def test_cli_choices_cover_user_creatable_task_kinds():
    from src.cli.styles import TASK_TYPES

    # Delivery intents are authored by promote controls, never ordinary task create.
    assert set(TASK_TYPES) == TASK_TYPE_VALUES - {
        TaskType.PROMOTION.value, TaskType.BACKMERGE.value,
    }


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


async def test_a_graph_node_carries_a_kind_and_is_filed_unrouted(handler, db):
    graph = {
        "version": 1,
        "parent": {"title": "Epic"},
        "nodes": [
            {"key": "d", "title": "D", "acceptance": ["x"], "task_type": "design",
             "intelligence_class": "deep-high"},
            {"key": "a", "title": "A", "acceptance": ["x"], "task_type": "art"},
        ],
    }
    result = await handler._cmd_create_task_graph({"project_id": "p", "graph": graph})
    assert "error" not in result, result
    ids = {node["key"]: node["task_id"] for node in result["nodes"]}
    design, art = await db.get_task(ids["d"]), await db.get_task(ids["a"])
    assert (design.task_type, design.route_source) == (TaskType.DESIGN, UNROUTED)
    assert (design.profile_id, design.intelligence_class, design.class_hint) == (
        None, None, "deep-high",
    )
    assert (art.task_type, art.route_source) == (TaskType.ART, UNROUTED)


async def test_a_graph_node_naming_a_profile_is_refused(handler, db):
    """A node carries hints, never a route (mandatory-routing spec §5.1)."""
    result = await handler._cmd_create_task_graph({
        "project_id": "p",
        "graph": {
            "version": 1,
            "nodes": [{"key": "d", "title": "D", "acceptance": ["x"], "task_type": "design",
                       "profile": "worker"}],
        },
    })
    assert result["code"] == "routing.choice_forbidden"
    assert result["refused"] == ["d.profile"]
    assert await db.list_tasks(project_id="p") == []
