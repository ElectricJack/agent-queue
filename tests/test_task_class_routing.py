"""Creation stores hints, never a resolved route (mandatory-routing spec §1, §5.3).

``create_task --intelligence-class X`` used to resolve a profile at creation:
the project's ``preferred_provider`` plus the class (``class_match``), the
supervisor's project default, a profiled caller's own profile (inheritance),
or the project default, each then matched onto a class lane.  Every one of
those paths is gone.  A task filed without a profile by the supervisor, a
playbook or the operator is stored ``unrouted`` with the class as its
``class_hint`` and no profile or class, for the project's router to route.
Role tasks (``triage``, ``spec-ingest``, …) filed by a service or playbook keep
their stage profile with ``route_source='role'`` and the role's own class.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.commands.handler import CommandHandler
from src.commands.principal import (
    DENY_ALL,
    TRUSTED_LOCAL,
    ExecutionPrincipal,
    PrincipalKind,
    principal_context,
)
from src.commands.task_commands import _check_capability_escalation
from src.config import AppConfig, DatabaseConfig
from src.database import Database
from src.models import AgentProfile, Project, Task
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
        _worker("fast-high-claude", "claude", "fast-high"),
        _worker("standard-high-claude", "claude", "standard-high"),
        _worker("standard-high-codex", "codex", "standard-high"),
        _worker("deep-high-codex", "codex", "deep-high"),
        # A task-lifecycle profile of the same class loses to the pool.
        _worker("zz-standard-high-task", "claude", "standard-high", lifecycle="task"),
        # Disabled and special-purpose profiles are never a class route.
        _worker("fast-low-claude", "claude", "fast-low", enabled=False),
        _worker("reviewer", "codex", "fast-low", lifecycle="task"),
        # Role profiles run their own class (spec §4, D3).
        _worker("triage", "claude", "fast-high", lifecycle="task"),
        _worker("spec-ingest", "claude", "deep-high", lifecycle="task"),
    ):
        await db.create_profile(profile)
    await db.update_project("p", default_profile_id="fast-high-claude")
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


def _assert_unrouted(task, class_hint):
    assert task.profile_id is None
    assert task.intelligence_class is None
    assert task.class_hint == class_hint
    assert task.route_source == "unrouted"
    assert task.provider_intent == "class_only"


@pytest.mark.parametrize("caller", [None, "supervisor", "fast-high-claude"])
@pytest.mark.parametrize("preferred_provider", [None, "codex"])
async def test_a_filing_without_a_profile_is_stored_unrouted_with_its_hint(
    setup, caller, preferred_provider,
):
    """No path picks a route: not the class, the project default, the
    supervisor fallback, the caller's own profile or the preferred provider."""
    handler, db = setup
    await db.update_project("p", preferred_provider=preferred_provider)
    result = await _create_as(handler, caller, intelligence_class="standard-high")
    assert result.get("success") is True, result
    assert "profile_id" not in result and "profile_source" not in result
    assert result["route_source"] == "unrouted"
    assert result["class_hint"] == "standard-high"
    task = await db.get_task(result["created"])
    _assert_unrouted(task, "standard-high")


@pytest.mark.parametrize(
    "principal",
    [TRUSTED_LOCAL, ExecutionPrincipal.service("playbook-dispatch")],
    ids=["operator", "playbook"],
)
async def test_operator_and_playbook_filings_are_routed_by_the_router(setup, principal):
    handler, db = setup
    with principal_context(principal):
        result = await _create_as(handler, None, intelligence_class="deep-high")
    assert result.get("success") is True, result
    task = await db.get_task(result["created"])
    _assert_unrouted(task, "deep-high")
    # The router's planner reads the hint (spec §6.4 step 1).
    facts = await handler._routing_task_facts(task, await db.get_project("p"))
    assert facts.class_hint == "deep-high"


async def test_a_filing_with_no_class_has_no_hint(setup):
    handler, db = setup
    result = await _create_as(handler, "supervisor")
    assert result.get("success") is True, result
    _assert_unrouted(await db.get_task(result["created"]), None)


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
    task = await db.get_task(result["created"])
    # An explicit profile still works until the filing surfaces refuse it
    # (spec Task 5), stamped ``legacy``.
    assert task.profile_id == "standard-high-codex"
    assert task.route_source == "legacy"
    assert (task.intelligence_class, task.class_hint) == ("standard-high", "standard-high")


async def test_worker_caller_escalation_is_still_enforced(setup):
    handler, db = setup
    await db.create_profile(_worker(
        "narrow-worker", "claude", "fast-high", lifecycle="task", harness_tools=[],
    ))
    explicit = await _create_as(handler, "narrow-worker", profile_id="standard-high-claude")
    assert "Capability escalation rejected" in explicit["error"]
    assert await db.list_tasks(project_id="p") == []
    # A class is a hint, not a delegation: nothing is matched, so nothing
    # escalates, and the filing is stored unrouted.
    by_class = await _create_as(handler, "narrow-worker", intelligence_class="standard-high")
    assert by_class.get("success") is True, by_class
    _assert_unrouted(await db.get_task(by_class["created"]), "standard-high")


async def test_a_profiled_caller_no_longer_inherits_its_profile(setup):
    handler, db = setup
    result = await _create_as(handler, "fast-high-claude")
    assert result.get("success") is True, result
    _assert_unrouted(await db.get_task(result["created"]), None)


async def test_a_class_no_enabled_worker_runs_is_still_a_hint(setup):
    """Whether any worker runs the class is the router's question
    (``no_candidates``), not creation's."""
    handler, db = setup
    result = await _create_as(handler, "supervisor", intelligence_class="fast-low")
    assert result.get("success") is True, result
    _assert_unrouted(await db.get_task(result["created"]), "fast-low")


async def test_unknown_class_is_rejected_before_selection(setup):
    handler, db = setup
    result = await _create_as(handler, "supervisor", intelligence_class="no-such-class")
    assert "not found in vault" in result["error"]
    assert await db.list_tasks(project_id="p") == []


async def test_graph_class_nodes_are_hints_and_named_profiles_keep_their_class(setup):
    handler, db = setup
    await db.update_project("p", preferred_provider="codex")
    report = await handler._cmd_create_task_graph({
        "project_id": "p",
        "graph": {"nodes": [
            {"key": "high", "title": "High", "intelligence_class": "standard-high"},
            # No enabled worker runs fast-low: still only a hint.
            {"key": "low", "title": "Low", "intelligence_class": "fast-low"},
            {"key": "bare", "title": "Bare"},
            {"key": "pinned", "title": "Pinned", "profile": "standard-high-claude",
             "intelligence_class": "standard-high"},
        ]},
    })
    assert "error" not in report, report
    assert "class_matched_profiles" not in report
    ids = {node["key"]: node["task_id"] for node in report["nodes"]}
    _assert_unrouted(await db.get_task(ids["high"]), "standard-high")
    _assert_unrouted(await db.get_task(ids["low"]), "fast-low")
    _assert_unrouted(await db.get_task(ids["bare"]), None)
    pinned = await db.get_task(ids["pinned"])
    assert (pinned.profile_id, pinned.intelligence_class) == ("standard-high-claude", "standard-high")
    assert pinned.route_source == "legacy"


async def test_graph_node_with_an_unknown_class_fails_the_whole_graph(setup):
    handler, db = setup
    report = await handler._cmd_create_task_graph({
        "project_id": "p",
        "graph": {"nodes": [
            {"key": "ok", "title": "Ok", "intelligence_class": "standard-high"},
            {"key": "bad", "title": "Bad", "intelligence_class": "no-such-class"},
        ]},
    })
    assert "nothing was created" in report["error"]
    assert [(e["rule"], e["node"]) for e in report["errors"]] == [
        ("invalid_intelligence_class", "bad")
    ]
    assert await db.list_tasks(project_id="p") == []


# -- role tasks (spec §4, D3) ---------------------------------------------


@pytest.mark.parametrize(
    "principal",
    [
        None,
        ExecutionPrincipal.service("playbook-dispatch"),
        # A playbook principal carries its run's profile (the control plane
        # here, so the delegation subset check does not apply).
        ExecutionPrincipal(kind=PrincipalKind.PLAYBOOK, policy=DENY_ALL, profile_id="supervisor"),
    ],
    ids=["internal", "service", "playbook"],
)
async def test_spec_ingest_is_a_role_task_with_the_role_class(setup, principal):
    handler, db = setup

    async def ensure():
        return await handler._cmd_ensure_task({
            "project_id": "p", "dedup_key": "spec-ingest:specs/x.md",
            "title": "Ingest spec specs/x.md", "profile_id": "spec-ingest",
            # The default pipeline passes a class; the role's own class wins.
            "intelligence_class": "standard-high",
        })

    if principal is None:
        result = await ensure()
    else:
        with principal_context(principal):
            result = await ensure()
    assert result.get("success") is True, result
    task = await db.get_task(result["task_id"])
    assert task.profile_id == "spec-ingest"
    assert task.route_source == "role"
    assert task.intelligence_class == "deep-high"
    assert task.class_hint == "standard-high"


async def test_triage_is_a_role_task_with_the_role_class(setup):
    handler, db = setup
    # The canonical triage task is only born when routing work is waiting.
    await db.create_task(Task(id="unrouted", project_id="p", title="Unrouted", description=""))
    await db.create_gate("p", "routing", "Route task", waiter_task_ids=["unrouted"])
    result = await handler._cmd_ensure_task({
        "project_id": "p", "dedup_key": "triage-open", "profile_id": "triage",
        "title": "Triage unrouted tasks", "description": "Route pending tasks.",
        "intelligence_class": "standard-high",
    })
    assert result.get("success") is True, result
    task = await db.get_task(result["task_id"])
    assert task.profile_id == "triage"
    assert task.route_source == "role"
    assert task.intelligence_class == "fast-high"
    assert task.class_hint == "standard-high"


async def test_an_operator_naming_a_role_profile_keeps_its_class(setup):
    """Only a service or playbook creates role tasks with the role class; an
    operator's explicit profile is the legacy path until Task 5 refuses it."""
    handler, db = setup
    with principal_context(TRUSTED_LOCAL):
        result = await _create_as(
            handler, None, profile_id="spec-ingest", intelligence_class="standard-high",
        )
    assert result.get("success") is True, result
    assert result["profile_source"] == "explicit"
    task = await db.get_task(result["created"])
    assert task.intelligence_class == "standard-high"
