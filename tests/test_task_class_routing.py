"""An explicit intelligence class picks its own lane at creation time.

``create_task --intelligence-class X`` without a profile used to store X on
whatever profile creation chose implicitly (project default, supervisor
fallback, inherited caller profile), so a READY task could be claimed by a
worker of another tier or provider before anyone ran ``task route``.  The
profile is now resolved from the class before the row is written.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.commands.handler import CommandHandler
from src.commands.task_commands import _check_capability_escalation
from src.config import AppConfig, DatabaseConfig
from src.database import Database
from src.models import AgentProfile, Project, TaskStatus
from src.orchestrator import Orchestrator
from src.vault import ensure_default_intelligence_classes
from tests.db_fixtures import lease_dsn

WORKER_CAPS = {"harness_tools": ["Bash", "Edit"], "aq_commands": [], "plugin_tools": []}


def _worker(profile_id, harness, default_class, *, lifecycle="pool", **extra):
    return AgentProfile(
        id=profile_id, name=profile_id, harness=harness, lifecycle=lifecycle,
        default_class=default_class, needs_workspace=False, **{**WORKER_CAPS, **extra},
    )


@pytest.fixture
async def setup(tmp_path):
    db = Database(lease_dsn("class_route.db"))
    await db.initialize()
    await db.create_project(Project(id="p", name="Project"))
    await db.create_profile(AgentProfile(
        id="supervisor", name="Supervisor", harness="claude", lifecycle="named",
        needs_workspace=False, harness_tools=[], aq_commands=[], plugin_tools=[],
    ))
    for profile in (
        _worker("standard-medium-claude", "claude", "standard-medium"),
        _worker("standard-high-claude", "claude", "standard-high"),
        _worker("standard-high-codex", "codex", "standard-high"),
        _worker("deep-high-codex", "codex", "deep-high"),
        # A task-lifecycle profile of the same class loses to the pool.
        _worker("zz-standard-high-task", "claude", "standard-high", lifecycle="task"),
        # Disabled and special-purpose profiles are never a class route.
        _worker("fast-low-claude", "claude", "fast-low", enabled=False),
        _worker("reviewer", "codex", "standard-low", lifecycle="task"),
    ):
        await db.create_profile(profile)
    await db.update_project("p", default_profile_id="standard-medium-claude")
    data_dir = str(tmp_path / "data")
    ensure_default_intelligence_classes(data_dir)
    config = AppConfig(data_dir=data_dir, database=DatabaseConfig(url=lease_dsn("class_route.db")))
    orch = Orchestrator(config)
    orch.db = db
    orch.git = MagicMock()
    orch._emit_notify = AsyncMock()
    handler = CommandHandler(orch, config)
    yield handler, db
    handler._caller_profile_id = None
    await db.close()


async def _create_as(handler, caller, **args):
    handler._caller_profile_id = caller
    try:
        return await handler._cmd_create_task({"project_id": "p", "title": "Work", **args})
    finally:
        handler._caller_profile_id = None


async def test_supervisor_class_selects_matching_pool_profile_before_insert(setup):
    handler, db = setup
    result = await _create_as(handler, "supervisor", intelligence_class="standard-high")
    assert result.get("success") is True, result
    assert result["profile_id"] == "standard-high-claude"
    assert result["profile_source"] == "class_match"
    task = await db.get_task(result["created"])
    # Written once with an agreeing route: READY, unblocked, no route needed.
    assert (task.profile_id, task.intelligence_class) == ("standard-high-claude", "standard-high")
    assert task.status == TaskStatus.READY and not task.is_blocked
    assert await db.get_gates_for_task(task.id) == []


async def test_class_match_prefers_the_implicit_routes_provider(setup):
    handler, db = setup
    await db.update_project("p", default_profile_id="deep-high-codex")
    result = await _create_as(handler, "supervisor", intelligence_class="standard-high")
    assert result["profile_id"] == "standard-high-codex"
    assert result["profile_source"] == "class_match"


async def test_class_the_default_already_runs_keeps_the_default(setup):
    handler, _db = setup
    result = await _create_as(handler, "supervisor", intelligence_class="standard-medium")
    assert result["profile_id"] == "standard-medium-claude"
    assert result["profile_source"] == "project_default"


async def test_supervisor_may_name_a_worker_profile_it_does_not_contain(setup):
    handler, db = setup
    supervisor = await db.get_profile("supervisor")
    worker = await db.get_profile("standard-high-codex")
    # The subset check would reject this pair; the supervisor routes work
    # rather than delegating its own capabilities, so it is not applied.
    assert _check_capability_escalation(supervisor, worker)
    result = await _create_as(
        handler, "supervisor", profile_id="standard-high-codex",
        intelligence_class="standard-high",
    )
    assert result.get("success") is True, result
    assert result["profile_source"] == "explicit"
    assert (await db.get_task(result["created"])).profile_id == "standard-high-codex"


async def test_worker_caller_escalation_is_still_enforced(setup):
    handler, db = setup
    await db.create_profile(_worker(
        "narrow-worker", "claude", "standard-medium", lifecycle="task", harness_tools=[],
    ))
    explicit = await _create_as(handler, "narrow-worker", profile_id="standard-high-claude")
    assert "Capability escalation rejected" in explicit["error"]
    # A class match is a delegation too, bounded the same way.
    by_class = await _create_as(handler, "narrow-worker", intelligence_class="standard-high")
    assert "Capability escalation rejected" in by_class["error"]
    assert "standard-high-claude" in by_class["error"]
    assert await db.list_tasks(project_id="p") == []


async def test_worker_caller_class_match_within_its_bounds(setup):
    handler, _db = setup
    inherited = await _create_as(handler, "standard-medium-claude")
    assert inherited["profile_id"] == "standard-medium-claude"
    assert inherited["profile_source"] == "inherited"
    matched = await _create_as(
        handler, "standard-medium-claude", intelligence_class="standard-high"
    )
    assert matched["profile_id"] == "standard-high-claude"
    assert matched["profile_source"] == "class_match"


async def test_class_no_enabled_worker_runs_fails_listing_available_classes(setup):
    handler, db = setup
    # fast-low exists in the vault; its only profile is disabled.
    result = await _create_as(handler, "supervisor", intelligence_class="fast-low")
    assert result["success"] is False
    assert "no enabled worker profile runs intelligence class 'fast-low'" in result["error"]
    assert "deep-high, standard-high, standard-medium" in result["error"]
    assert await db.list_tasks(project_id="p") == []


async def test_unknown_class_is_rejected_before_selection(setup):
    handler, db = setup
    result = await _create_as(handler, "supervisor", intelligence_class="no-such-class")
    assert "not found in vault" in result["error"]
    assert await db.list_tasks(project_id="p") == []


async def test_operator_create_pins_the_class_lane_over_an_implicit_default(setup):
    handler, db = setup
    matched = await _create_as(handler, None, intelligence_class="standard-high")
    assert matched["profile_source"] == "class_match"
    assert (await db.get_task(matched["created"])).profile_id == "standard-high-claude"
    # A class the default runs keeps the historical implicit (NULL) route.
    kept = await _create_as(handler, None, intelligence_class="standard-medium")
    assert kept["profile_source"] == "project_default"
    assert (await db.get_task(kept["created"])).profile_id is None


async def test_graph_nodes_resolve_their_class_lane(setup):
    handler, db = setup
    report = await handler._cmd_create_task_graph({
        "project_id": "p",
        "graph": {"nodes": [
            {"key": "high", "title": "High", "intelligence_class": "standard-high"},
            {"key": "same", "title": "Same", "intelligence_class": "standard-medium"},
            {"key": "pinned", "title": "Pinned", "profile": "standard-high-codex",
             "intelligence_class": "standard-high"},
        ]},
    })
    assert "error" not in report, report
    assert report["class_matched_profiles"] == {"high": "standard-high-claude"}
    ids = {node["key"]: node["task_id"] for node in report["nodes"]}
    assert (await db.get_task(ids["high"])).profile_id == "standard-high-claude"
    assert (await db.get_task(ids["same"])).profile_id is None
    assert (await db.get_task(ids["pinned"])).profile_id == "standard-high-codex"


async def test_graph_node_class_without_a_worker_fails_the_whole_graph(setup):
    handler, db = setup
    report = await handler._cmd_create_task_graph({
        "project_id": "p",
        "graph": {"nodes": [
            {"key": "ok", "title": "Ok", "intelligence_class": "standard-high"},
            {"key": "bad", "title": "Bad", "intelligence_class": "fast-low"},
        ]},
    })
    assert "nothing was created" in report["error"]
    assert [(e["rule"], e["node"]) for e in report["errors"]] == [
        ("invalid_intelligence_class", "bad")
    ]
    assert await db.list_tasks(project_id="p") == []
