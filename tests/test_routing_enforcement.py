"""Only router routes are claimable (mandatory-routing spec §6.1, §9.1, §11; Task 6).

Router readiness per project, ``task.route_needed`` emission for unrouted and
ready-legacy work (never containers), the pool claim frontier, pool demand,
the push path's route filter and the cutover: queued legacy work is re-routed
once the router is ready, in-flight legacy work finishes where it is.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.assignment_routing import EffectiveAssignmentRoute, claimable_routes
from src.models import Task, TaskStatus, TaskType
from src.routing.readiness import (
    ROUTER_GRANT,
    RouterReadiness,
    orchestrator_ready_projects,
    orchestrator_router_ready,
    ready_router_projects,
)
from src.routing.sources import (
    LEGACY,
    OVERRIDE,
    ROLE,
    ROUTER,
    UNROUTED,
    claimable_sources,
    route_is_claimable,
)
from tests import test_routing_router as _router
from tests.test_routing_router import ROUTER_ID, _create, _route

# The router suite's orchestrator and handler fixtures, shared as-is.
orch = _router.orch
handler = _router.handler

APPLY = frozenset({"task_route_plan", ROUTER_GRANT})
OLD_ROUTER = frozenset({"task_route_options", "task_route"})


def _activation(playbook_id, sha, *, scope="system", identifier="", enabled=True):
    return {
        "playbook_id": playbook_id, "scope": scope, "scope_identifier": identifier,
        "enabled": enabled, "active_artifact_sha256": sha,
    }


def _project(project_id, router=ROUTER_ID):
    return SimpleNamespace(id=project_id, assignment_playbook_id=router)


def _ready(orchestrator, *project_ids: str) -> None:
    """Pin *orchestrator*'s ready set, as a refreshed cycle would."""
    orchestrator.router_readiness._ready = frozenset(project_ids)


# -- readiness -----------------------------------------------------------------------


def test_a_router_is_ready_when_its_bound_playbook_grants_task_route_apply():
    grants = {"new": APPLY, "old": OLD_ROUTER, "own": APPLY}.get
    projects = [
        _project("system-new"),
        _project("own-router", router="project-router"),
        _project("other-scope", router="project-router"),
        _project("unbound", router=None),
        _project("disabled", router="disabled-router"),
    ]
    activations = [
        _activation(ROUTER_ID, "new"),
        _activation("project-router", "own", scope="project", identifier="own-router"),
        _activation("disabled-router", "new", enabled=False),
    ]
    assert ready_router_projects(projects, activations, grants) == {"system-new", "own-router"}
    # The superseded shipped router (task_route, no apply) is not ready.
    old = [_activation(ROUTER_ID, "old")]
    assert ready_router_projects([_project("p")], old, grants) == frozenset()


async def test_readiness_refreshes_from_the_live_activations_and_caches_grants():
    loads: list[str] = []
    artifacts = {
        "new": SimpleNamespace(steps={"a": SimpleNamespace(command=ROUTER_GRANT, tool_use=None)}),
        "old": SimpleNamespace(steps={"a": SimpleNamespace(command="task_route", tool_use=None)}),
    }

    def load(sha):
        loads.append(sha)
        if sha not in artifacts:
            raise FileNotFoundError(sha)
        return artifacts[sha]

    rows = [_activation(ROUTER_ID, "old")]

    async def list_projects():
        return [_project("p")]

    async def list_playbook_activations(*, enabled_only):
        assert enabled_only
        return rows

    db = SimpleNamespace(
        list_projects=list_projects, list_playbook_activations=list_playbook_activations,
    )
    readiness = RouterReadiness(db_getter=lambda: db, artifact_loader=load)
    assert await readiness.is_ready("p") is False
    rows[:] = [_activation(ROUTER_ID, "new")]
    assert await readiness.ready_projects() == frozenset()  # cached until a refresh
    assert await readiness.refresh() == {"p"}
    assert await readiness.refresh() == {"p"}
    rows[:] = [_activation(ROUTER_ID, "missing")]
    assert await readiness.refresh() == frozenset()
    assert await readiness.refresh() == frozenset()
    # Grants are cached per immutable artifact; an unreadable one is retried.
    assert loads == ["old", "new", "missing", "missing"]


async def test_a_stub_orchestrator_has_no_ready_project():
    assert await orchestrator_ready_projects(SimpleNamespace()) == frozenset()
    assert await orchestrator_router_ready(object(), "p") is False


# -- the claimable rule ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "profile", "not_ready", "ready"),
    [
        (UNROUTED, None, False, False),
        (LEGACY, "w", True, False),
        (ROUTER, "w", True, True),
        (OVERRIDE, "w", True, True),
        (ROLE, "triage", True, True),
    ],
)
def test_only_router_override_and_role_routes_are_claimable_once_ready(
    source, profile, not_ready, ready
):
    assert route_is_claimable(profile, source, router_ready=False) is not_ready
    assert route_is_claimable(profile, source, router_ready=True) is ready
    assert (source in claimable_sources(True)) is ready


async def test_the_claim_frontier_and_pool_demand_follow_readiness(orch):
    db = orch.db
    for task_id, profile, source in (
        ("unrouted", None, UNROUTED),
        ("legacy", "standard-high-codex", LEGACY),
        ("routed", "standard-high-codex", ROUTER),
        ("override", "standard-high-codex", OVERRIDE),
    ):
        await db.create_task(Task(
            id=task_id, project_id="p", title=task_id, description="d",
            status=TaskStatus.READY, profile_id=profile, route_source=source,
            intelligence_class="standard-high" if profile else None,
            priority={"legacy": 1, "routed": 2, "override": 3}.get(task_id, 0),
        ))

    async def frontier(router_ready):
        taken = []
        async with db._engine.begin() as conn:
            for task_id in ("unrouted", "legacy", "routed", "override"):
                if await db.select_ready_for_profile(
                    conn, project_id="p", profile_id="standard-high-codex", agent_id="a",
                    task_id=task_id, router_ready=router_ready,
                ):
                    taken.append(task_id)
        return taken

    assert await frontier(False) == ["legacy", "routed", "override"]
    assert await frontier(True) == ["routed", "override"]
    assert await db.count_ready_by_profile("p", router_ready=False) == {"standard-high-codex": 3}
    assert await db.count_ready_by_profile("p", router_ready=True) == {"standard-high-codex": 2}
    # Without a readiness answer the structural count still sees every row.
    assert await db.count_ready_by_profile("p") == {None: 1, "standard-high-codex": 3}


def test_the_push_path_keeps_only_claimable_routes_for_queued_work():
    def task(task_id, status, profile, source, project="p"):
        return Task(id=task_id, project_id=project, title=task_id, description="d",
                    status=status, profile_id=profile, route_source=source)

    tasks = [
        task("unrouted", TaskStatus.READY, None, UNROUTED),
        task("legacy", TaskStatus.READY, "w", LEGACY),
        task("legacy-elsewhere", TaskStatus.READY, "w", LEGACY, project="q"),
        task("routed", TaskStatus.READY, "w", ROUTER),
        task("in-flight-legacy", TaskStatus.IN_PROGRESS, "w", LEGACY),
    ]
    routes = {
        t.id: EffectiveAssignmentRoute(t.id, "standard-high", None, "explicit") for t in tasks
    }
    assert set(claimable_routes(routes, tasks, frozenset())) == {
        "legacy", "legacy-elsewhere", "routed", "in-flight-legacy",
    }
    assert set(claimable_routes(routes, tasks, frozenset({"p"}))) == {
        "legacy-elsewhere", "routed", "in-flight-legacy",
    }


# -- emission -------------------------------------------------------------------------


async def _emitted(orchestrator) -> set[str]:
    orchestrator._route_needed_emitted = {}
    orchestrator.bus.emit.reset_mock()
    await orchestrator._emit_route_needed_events()
    return {
        call.args[1]["task_id"] for call in orchestrator.bus.emit.await_args_list
        if call.args[0] == "task.route_needed"
    }


async def test_emission_reads_unrouted_and_ready_legacy_never_containers(orch):
    db = orch.db
    await _create(db, "unrouted", task_type=TaskType.RESEARCH)
    await _create(db, "legacy", profile_id="standard-high-codex",
                  intelligence_class="standard-high")
    await _create(db, "routed", profile_id="standard-high-codex", route_source=ROUTER,
                  intelligence_class="standard-high")
    await _create(db, "parent", status=TaskStatus.DEFINED)
    await _create(db, "child", parent_task_id="parent", status=TaskStatus.DEFINED)
    await _create(db, "flagged")
    async with db.immediate() as conn:
        await db.mark_container("flagged", conn=conn)

    assert await _emitted(orch) == {"unrouted", "child"}
    _ready(orch, "p")
    assert await _emitted(orch) == {"unrouted", "child", "legacy"}


async def test_cutover_reroutes_queued_legacy_and_leaves_in_flight_legacy(handler, orch):
    """Spec §11: queued legacy work is re-routed; in-flight work finishes where it is
    and is re-routed only if it comes back to the queue."""
    db = orch.db
    await _create(db, "queued", task_type=TaskType.RESEARCH, profile_id="deep-high-claude",
                  intelligence_class="deep-high")
    await _create(db, "running", task_type=TaskType.RESEARCH, profile_id="deep-high-claude",
                  intelligence_class="deep-high", status=TaskStatus.IN_PROGRESS)
    _ready(orch, "p")
    assert await _emitted(orch) == {"queued"}

    assert (await _route(handler, "queued"))["outcome"] == "routed"
    queued = await db.get_task("queued")
    assert queued.route_source == ROUTER
    assert queued.route["legacy"]["profile_id"] == "deep-high-claude"
    running = await db.get_task("running")
    assert (running.profile_id, running.route_source) == ("deep-high-claude", LEGACY)

    await db.update_task("running", status=TaskStatus.READY)  # stopped back to the queue
    assert await _emitted(orch) == {"running"}
    assert (await _route(handler, "running"))["outcome"] == "routed"
    assert (await db.get_task("running")).route_source == ROUTER
    assert await _emitted(orch) == set()


async def test_a_router_that_is_not_ready_leaves_legacy_work_running(orch):
    db = orch.db
    await _create(db, "legacy", profile_id="standard-high-codex",
                  intelligence_class="standard-high")
    assert await _emitted(orch) == set()
    async with db._engine.begin() as conn:
        assert await db.select_ready_for_profile(
            conn, project_id="p", profile_id="standard-high-codex", agent_id="a",
            router_ready=await orchestrator_router_ready(orch, "p"),
        ) == "legacy"
