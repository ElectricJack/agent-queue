"""Only router routes are claimable (mandatory-routing spec §6.1, §9.1, §11; Task 6).

Router readiness per project, ``task.route_needed`` emission for unrouted and
ready-legacy work (never containers), the pool claim frontier, pool demand,
the push path's route filter and the cutover: queued legacy work is re-routed
once the router is ready, in-flight legacy work finishes where it is.
"""

from __future__ import annotations

import time
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


@pytest.mark.parametrize("disable", ["pool", "zero", "provider", "quarantine"])
async def test_unavailable_router_route_is_automatically_returned_and_rerouted(
    handler, orch, disable,
):
    from src.providers.availability import ProviderAvailability

    orch.set_command_handler(handler)
    _ready(orch, "p")
    _router._pool_source(orch)
    constraints = {"exclude_providers": ["uninstalled-provider"]}
    await _create(orch.db, "t", task_type=TaskType.RESEARCH, class_hint="standard-high",
                  prefer_target="codex", prefer_mode="soft", route={"constraints": constraints})
    assert (await _route(handler, "t"))["profile_id"] == "standard-high-codex"
    before = await orch.db.get_task("t")
    history = [{"attempt_id": "prior", "intelligence_class": "standard-high",
                "reason": "verification_failed"}]
    await orch.db.set_task_meta("t", "routing_failed_attempts", history)
    if disable == "pool":
        result = await handler.execute("pool_set_enabled", {
            "profile_id": "standard-high-codex", "enabled": False,
        })
        assert result["success"], result
    elif disable == "zero":
        await orch.db.update_profile("standard-high-codex", max_active=0)
    elif disable == "provider":
        orch.provider_availability._rows["codex"] = ProviderAvailability(
            provider="codex", override_state="disabled", updated_at=time.time(),
        )
    else:
        orch._quarantine_pool("p", "standard-high-codex", "private captured output")
    # The periodic sweep must repair existing routes, including after a restart
    # with no in-memory configuration event and no spare alternative capacity.
    for i in range(3):
        await _create(orch.db, f"full-{i}", profile_id="standard-high-claude",
                      intelligence_class="standard-high", route_source=ROUTER)
    assert await _emitted(orch) == {"t"}
    reset = await orch.db.get_task("t")
    assert (reset.route_source, reset.profile_id, reset.intelligence_class) == (UNROUTED, None, None)
    assert (reset.class_hint, reset.prefer_target, reset.prefer_mode) == (
        before.class_hint, before.prefer_target, before.prefer_mode,
    )
    assert reset.route == {"constraints": constraints}
    assert await orch.db.get_task_meta("t", "routing_failed_attempts") == history
    assert (await _route(handler, "t"))["profile_id"] == "standard-high-claude"
    assert await _emitted(orch) == set()
    comments = (await orch.db.list_task_comments("t"))["comments"]
    assert len(comments) == 1 and "unavailable" in comments[0]["body"]


async def test_automatic_rerouting_preserves_strict_target_until_reenabled(handler, orch):
    orch.set_command_handler(handler)
    _ready(orch, "p")
    await _create(orch.db, "t", task_type=TaskType.RESEARCH,
                  prefer_target="codex", prefer_mode="strict")
    assert (await _route(handler, "t"))["profile_id"] == "standard-high-codex"
    await orch.db.update_profile("standard-high-codex", enabled=False)
    assert await _emitted(orch) == {"t"}
    assert (await _router._plan(handler, "t"))["outcome"] in {"held", "no_candidates"}
    task = await orch.db.get_task("t")
    assert (task.profile_id, task.prefer_target, task.prefer_mode) == (None, "codex", "strict")
    await orch.db.update_profile("standard-high-codex", enabled=True)
    assert (await _route(handler, "t"))["profile_id"] == "standard-high-codex"


async def test_automatic_rerouting_leaves_active_and_authoritative_routes_alone(handler, orch):
    orch.set_command_handler(handler)
    _ready(orch, "p")
    for task_id, source, status in (
        ("running", ROUTER, TaskStatus.IN_PROGRESS),
        ("claimed", ROUTER, TaskStatus.READY),
        ("override", OVERRIDE, TaskStatus.READY),
        ("role", ROLE, TaskStatus.READY),
        ("healthy", ROUTER, TaskStatus.READY),
    ):
        await _create(orch.db, task_id, task_type=TaskType.RESEARCH,
                      profile_id="standard-high-claude" if task_id == "healthy"
                      else "standard-high-codex", route_source=source,
                      intelligence_class="standard-high", status=status)
    await _router._session(orch.db, "live", task_id="claimed")
    await orch.db.update_profile("standard-high-codex", enabled=False)
    assert await _emitted(orch) == set()
    for task_id in ("running", "claimed", "override", "role"):
        assert (await orch.db.get_task(task_id)).profile_id == "standard-high-codex"


@pytest.mark.parametrize("change", ["reenable", "claim", "route"])
async def test_route_refresh_rechecks_target_and_claim_after_waiting_for_apply_lock(
    handler, orch, monkeypatch, change,
):
    import asyncio

    orch.set_command_handler(handler)
    _ready(orch, "p")
    await _create(orch.db, "t", task_type=TaskType.RESEARCH,
                  profile_id="standard-high-codex", route_source=ROUTER,
                  intelligence_class="standard-high")
    await orch.db.update_profile("standard-high-codex", enabled=False)
    observed = asyncio.Event()
    original = handler._routing_static_facts

    async def facts(*args, **kwargs):
        result = await original(*args, **kwargs)
        observed.set()
        return result

    monkeypatch.setattr(handler, "_routing_static_facts", facts)
    async with orch.db.routing_apply_lock():
        sweep = asyncio.create_task(_emitted(orch))
        await asyncio.wait_for(observed.wait(), timeout=10)
        if change == "reenable":
            await orch.db.update_profile("standard-high-codex", enabled=True)
        elif change == "claim":
            await _router._session(orch.db, "live", task_id="t")
        else:
            await orch.db.update_task("t", profile_id="standard-high-claude", route_source=ROUTER)
    assert await sweep == set()
    assert (await orch.db.get_task("t")).profile_id == (
        "standard-high-claude" if change == "route" else "standard-high-codex"
    )
