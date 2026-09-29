"""Projects bound to the router and ``aq doctor --check routing.bypassed`` (Task 8).

Spec: ``projects/agent-queue/specs/2026-09-28-mandatory-task-routing.md`` §8
(the router binding, ``aq project set <p> router``) and §10 (the doctor check
and its project-only fix).
"""

from __future__ import annotations

import time

import pytest
from sqlalchemy import select, text

from src.database.tables import tasks as tasks_table
from src.doctor.models import DoctorContext, Severity
from src.doctor.routing_checks import (
    CHECK_ID,
    STALE_UNROUTED_SECONDS,
    _check_bypassed,
    routing_checks,
)
from src.doctor.runner import apply_fix
from src.models import Project, TaskStatus
from src.playbooks.artifact_ref import ArtifactRef
from src.routing.readiness import (
    BINDING_MISSING,
    BINDING_NOT_READY,
    BINDING_NOT_ROUTER,
    BINDING_READY,
    BINDING_UNBOUND,
    ROUTER_GRANT,
    binding_state,
)
from src.routing.sources import LEGACY, OVERRIDE, ROUTER, UNROUTED
from tests import test_routing_router as _router
from tests.test_routing_router import ROUTER_ID, _create

# The router suite's orchestrator and handler fixtures, shared as-is.
orch = _router.orch
handler = _router.handler

APPLY = frozenset({"task_route_plan", ROUTER_GRANT})
NOT_A_ROUTER = frozenset({"task_create"})


async def _activate(
    orchestrator, playbook_id: str, digit: str, grants, *, scope: str = "system",
    identifier: str = "", enabled: bool = True,
) -> str:
    """Activate an artifact of *playbook_id* whose grants are *grants*."""
    sha = "sha256:" + digit * 64
    ref = ArtifactRef(
        playbook_id=playbook_id, artifact_sha256=sha, schema_generation=2,
        contract_fingerprint="sha256:" + "b" * 64, source_digest="sha256:" + "c" * 64,
        compiler_build="test",
    )
    await orchestrator.db.upsert_playbook_artifact(
        ref, scope=scope, scope_identifier=identifier, path=f"/artifacts/{digit}.json",
        size_bytes=1,
    )
    await orchestrator.db.set_playbook_activation(
        playbook_id=playbook_id, scope=scope, scope_identifier=identifier,
        artifact_sha256=sha, enabled=enabled, activated_by="test",
        health="ready" if enabled else "disabled", reasons="[]",
    )
    # The grant cache doctor and the binding check read (no artifact file).
    orchestrator.router_readiness._grants[sha] = frozenset(grants)
    return sha


def _ctx(handler) -> DoctorContext:
    return DoctorContext(config=handler.config, db=handler.orchestrator.db, handler=handler)


async def _age(db, task_id: str, seconds: float) -> None:
    async with db._engine.begin() as conn:
        await conn.execute(
            text("UPDATE tasks SET updated_at = :at WHERE id = :id"),
            {"at": time.time() - seconds, "id": task_id},
        )


async def _task_rows(db) -> list[tuple]:
    async with db._engine.connect() as conn:
        return [tuple(row) for row in (await conn.execute(
            select(tasks_table).order_by(tasks_table.c.id)
        )).fetchall()]


# -- binding_state (pure) ------------------------------------------------------------


def _row(playbook_id, sha, *, scope="system", identifier="", enabled=True):
    return {
        "activation_id": f"act-{playbook_id}-{scope}", "playbook_id": playbook_id,
        "scope": scope, "scope_identifier": identifier, "enabled": enabled,
        "active_artifact_sha256": sha, "health": "ready" if enabled else "disabled",
    }


@pytest.mark.parametrize(
    "bound, activations, state",
    [
        (ROUTER_ID, [_row(ROUTER_ID, "new")], BINDING_READY),
        ("own", [_row("own", "new", scope="project", identifier="p")], BINDING_READY),
        (None, [_row(ROUTER_ID, "new")], BINDING_UNBOUND),
        ("  ", [_row(ROUTER_ID, "new")], BINDING_UNBOUND),
        ("gone", [_row(ROUTER_ID, "new")], BINDING_MISSING),
        ("own", [_row("own", "new", scope="project", identifier="other")], BINDING_MISSING),
        ("pipeline", [_row("pipeline", "plain")], BINDING_NOT_ROUTER),
        (ROUTER_ID, [_row(ROUTER_ID, "new", enabled=False)], BINDING_NOT_READY),
        (ROUTER_ID, [_row(ROUTER_ID, None)], BINDING_NOT_READY),
    ],
)
def test_binding_state(bound, activations, state):
    grants = {"new": APPLY, "plain": NOT_A_ROUTER}.get
    assert binding_state("p", bound, activations, lambda sha: grants(sha, frozenset()))[0] == state


def test_a_not_ready_router_names_the_activation_to_fix():
    rows = [_row(ROUTER_ID, "new", enabled=False)]
    _state, detail = binding_state("p", ROUTER_ID, rows, lambda sha: APPLY)
    assert f"act-{ROUTER_ID}-system" in detail and "disabled" in detail


# -- a new project is bound and has no default profile ------------------------------


async def test_a_created_project_is_bound_to_the_default_router(handler, orch):
    result = await handler.execute("create_project", {"name": "Fresh"})
    assert result["assignment_playbook_id"] == handler.config.routing.default_router
    assert "default_profile_id" not in result
    project = await orch.db.get_project("fresh")
    assert project.assignment_playbook_id == handler.config.routing.default_router
    shown = await handler.execute("get_project", {"project_id": "fresh"})
    assert shown["assignment_playbook_id"] == handler.config.routing.default_router
    assert "default_profile_id" not in shown


async def test_create_project_follows_the_configured_default_router(handler, orch):
    handler.config.routing.default_router = "house-router"
    result = await handler.execute("create_project", {"name": "Housed"})
    assert result["assignment_playbook_id"] == "house-router"
    assert (await orch.db.get_project("housed")).assignment_playbook_id == "house-router"


# -- aq project set <p> router --------------------------------------------------------


async def test_the_local_operator_binds_a_ready_router(handler, orch):
    await _activate(orch, ROUTER_ID, "1", APPLY)
    await _activate(orch, "own-router", "2", APPLY, scope="project", identifier="p")
    result = await handler.execute(
        "edit_project", {"project_id": "p", "assignment_playbook_id": "own-router"},
    )
    assert result["updated"] == "p" and result["fields"] == ["assignment_playbook_id"]
    assert (await orch.db.get_project("p")).assignment_playbook_id == "own-router"
    # The frontier's cached ready set was refreshed with the new binding.
    assert "p" in await orch.router_readiness.ready_projects()


@pytest.mark.parametrize(
    "router, code",
    [
        ("gone", "router_missing"),
        ("pipeline", "router_not_router"),
        ("parked", "router_not_ready"),
        ("", "router_required"),
        (None, "router_required"),
    ],
)
async def test_binding_refuses_what_cannot_route(handler, orch, router, code):
    await _activate(orch, ROUTER_ID, "1", APPLY)
    await _activate(orch, "pipeline", "3", NOT_A_ROUTER)
    await _activate(orch, "parked", "4", APPLY, enabled=False)
    result = await handler.execute(
        "edit_project", {"project_id": "p", "assignment_playbook_id": router},
    )
    assert result["success"] is False and result["error_code"] == code, result
    assert (await orch.db.get_project("p")).assignment_playbook_id == ROUTER_ID


@pytest.mark.parametrize("kind", ["session", "service"])
async def test_only_the_local_operator_binds_a_router(handler, orch, kind):
    from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
    from src.profiles.capabilities import CapabilityPolicy

    await _activate(orch, ROUTER_ID, "1", APPLY)
    await _activate(orch, "own-router", "2", APPLY, scope="project", identifier="p")
    principal = (
        ExecutionPrincipal.service("ratchet") if kind == "service" else ExecutionPrincipal(
            kind=PrincipalKind.SESSION, session_id="supervisor-session", project_id="p",
            elevated=True,
            policy=CapabilityPolicy.from_namespaces(aq_commands=["edit_project"]),
        )
    )
    with principal_context(principal):
        result = await handler._cmd_edit_project(
            {"project_id": "p", "assignment_playbook_id": "own-router"}
        )
    assert result["error_code"] == "local_operator_only", result
    assert (await orch.db.get_project("p")).assignment_playbook_id == ROUTER_ID


# -- aq doctor --check routing.bypassed ----------------------------------------------


def test_the_check_is_registered_with_a_fix():
    from src.doctor import default_registry

    (check,) = routing_checks()
    assert check.id == CHECK_ID and check.fix is not None
    assert CHECK_ID in {c.id for c in default_registry().checks()}


async def test_a_clean_install_is_ok(handler, orch):
    await _activate(orch, ROUTER_ID, "1", APPLY)
    await _create(orch.db, "routed", profile_id="standard-high-claude", route_source=ROUTER,
                  intelligence_class="standard-high")
    await _create(orch.db, "fresh")  # unrouted, but only just filed
    result = await _check_bypassed(_ctx(handler))
    assert result.severity == Severity.OK, result.detail
    assert result.fixable is False
    assert result.data["ready_projects"] == ["p"]


async def test_each_seeded_bypass_kind_is_reported(handler, orch):
    await _activate(orch, ROUTER_ID, "1", APPLY)
    await _activate(orch, "pipeline", "3", NOT_A_ROUTER)
    for project_id, router in (
        ("unbound", ""), ("missing", "gone-router"), ("not-router", "pipeline"),
    ):
        await orch.db.create_project(Project(id=project_id, name=project_id))
        await orch.db.update_project(project_id, assignment_playbook_id=router)
    # A route the router did not write, queued in a ready project.
    await _create(orch.db, "bypass", profile_id="standard-high-codex", route_source=LEGACY)
    # Unrouted for longer than an emission window and a run.
    await _create(orch.db, "stale")
    await _age(orch.db, "stale", STALE_UNROUTED_SECONDS + 60)
    # An emergency override, still open.
    await _create(orch.db, "override", profile_id="deep-high-claude", route_source=OVERRIDE,
                  status=TaskStatus.IN_PROGRESS)
    # In-flight legacy work finishes where it is.
    await _create(orch.db, "in-flight", profile_id="standard-high-claude",
                  route_source=LEGACY, status=TaskStatus.IN_PROGRESS)
    # A hand pin replaced at the cutover, kept in route.legacy.
    await _create(orch.db, "was-pinned", profile_id="standard-high-claude",
                  route_source=ROUTER, route={"legacy": {
                      "profile_id": "standard-high-codex", "intelligence_class": "standard-high",
                      "provider_intent": "pinned",
                  }})

    result = await _check_bypassed(_ctx(handler))

    assert result.severity == Severity.ERROR
    assert result.fixable is True
    data = result.data
    assert [b["task_id"] for b in data["bypass"]] == ["bypass"]
    assert {r["project_id"]: r["state"] for r in data["unbound_projects"]} == {
        "unbound": BINDING_UNBOUND, "missing": BINDING_MISSING, "not-router": BINDING_NOT_ROUTER,
    }
    assert data["not_ready_projects"] == []
    assert [(s["task_id"], s["reason"]) for s in data["stale_unrouted"]] == [
        ("stale", "awaiting_route")
    ]
    assert [o["task_id"] for o in data["open_overrides"]] == ["override"]
    assert data["in_flight_legacy"] == 1
    assert data["pins_lost"] == 1


async def test_an_unready_router_is_an_error_naming_its_activation(handler, orch):
    await _activate(orch, ROUTER_ID, "1", APPLY, enabled=False)
    await _create(orch.db, "waiting")
    await _age(orch.db, "waiting", STALE_UNROUTED_SECONDS + 60)
    # Legacy work in a project that is not ready is not a bypass: it still runs.
    await _create(orch.db, "legacy", profile_id="standard-high-codex", route_source=LEGACY)

    result = await _check_bypassed(_ctx(handler))

    assert result.severity == Severity.ERROR
    assert result.fixable is False  # re-binding the default router changes nothing
    (not_ready,) = result.data["not_ready_projects"]
    assert not_ready["project_id"] == "p" and not_ready["state"] == BINDING_NOT_READY
    assert "disabled" in not_ready["detail"]
    assert result.data["bypass"] == []
    assert [s["reason"] for s in result.data["stale_unrouted"]] == ["router_not_ready"]


async def test_the_fix_binds_projects_and_touches_no_task(handler, orch):
    await _activate(orch, ROUTER_ID, "1", APPLY)
    await _activate(orch, "pipeline", "3", NOT_A_ROUTER)
    for project_id, router in (("unbound", ""), ("not-router", "pipeline")):
        await orch.db.create_project(Project(id=project_id, name=project_id))
        await orch.db.update_project(project_id, assignment_playbook_id=router)
    await orch.db.create_project(Project(id="kept", name="kept"))
    await _create(orch.db, "bypass", profile_id="standard-high-codex", route_source=LEGACY)
    await _create(orch.db, "queued")
    before = await _task_rows(orch.db)

    (check,) = routing_checks()
    result = await apply_fix(check, _ctx(handler))

    assert result.fix_applied is True
    bindings = {p.id: p.assignment_playbook_id for p in await orch.db.list_projects()}
    assert bindings == {
        "p": ROUTER_ID, "unbound": ROUTER_ID, "not-router": ROUTER_ID, "kept": ROUTER_ID,
    }
    assert result.data["unbound_projects"] == []
    # Doctor fixes never edit tasks: the bypass is still reported, untouched.
    assert await _task_rows(orch.db) == before
    assert [b["task_id"] for b in result.data["bypass"]] == ["bypass"]
    assert (await orch.db.get_task("queued")).route_source == UNROUTED
