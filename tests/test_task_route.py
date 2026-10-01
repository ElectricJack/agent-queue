"""``aq task route`` re-runs the router; ``task_route_override`` is the audited
emergency override; ``aq task explain`` says why the router has not routed a task.

Spec: ``projects/agent-queue/specs/2026-09-28-mandatory-task-routing.md`` §7 and
§10 (Task 7).  The router suite's fixtures supply a real handler over pool
profiles on two harnesses, so a re-run can be routed again by the real
``task_route_plan`` / ``task_route_apply``.
"""

from __future__ import annotations

import json
import re
import time

import pytest

from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
from src.commands.routing_commands import INVALID_OVERRIDE, NOT_PERMITTED, NOT_ROUTABLE
from src.models import AgentProfile, SessionRecord, TaskStatus, TaskType
from src.profiles.capabilities import CapabilityPolicy
from src.routing.filing import ROUTING_CHOICE_FORBIDDEN
from src.routing.sources import OVERRIDE, ROLE, ROUTER, UNROUTED
from tests import test_routing_router as _router
from tests.test_routing_router import ROUTER_ID, _as_playbook, _create, _route

# The router suite's orchestrator and handler fixtures, shared as-is.
orch = _router.orch
handler = _router.handler

REASON = "codex keeps failing this build; pin it while we look"
COMMANDS = ("task_route", "task_route_override", "explain_task")


def _ready(orchestrator, *project_ids: str) -> None:
    """Pin *orchestrator*'s ready set, as a refreshed cycle would."""
    orchestrator.router_readiness._ready = frozenset(project_ids)


def _session(session_id: str, *, elevated: bool = False) -> ExecutionPrincipal:
    return ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        policy=CapabilityPolicy.from_namespaces(aq_commands=list(COMMANDS)),
        session_id=session_id,
        project_id="p",
        elevated=elevated,
    )


PLAYBOOK = ExecutionPrincipal(
    kind=PrincipalKind.PLAYBOOK,
    policy=CapabilityPolicy.from_namespaces(aq_commands=list(COMMANDS)),
    profile_id="pipeline",
)


async def _as(handler, principal, command: str, args: dict) -> dict:
    with principal_context(principal):
        return await handler.execute(command, dict(args))


async def _supervisor(db, session_id: str = "super") -> ExecutionPrincipal:
    """A live named supervisor session, and the elevated principal its token carries."""
    await db.create_profile(AgentProfile(id="supervisor", name="Supervisor", harness="claude"))
    await db.create_session(SessionRecord(
        id=session_id, project_id="p", profile_id="supervisor", harness="claude",
        provider="fake", name="p-supervisor", lifecycle="named", work_dir="/tmp",
        epoch="e", instance_token="token", started_at=time.time(), state="running",
    ))
    return _session(session_id, elevated=True)


def _events(orch, event_type: str) -> list[dict]:
    return [
        call.args[1] for call in orch.bus.emit.await_args_list if call.args[0] == event_type
    ]


async def _comments(db, task_id: str) -> list[dict]:
    return (await db.list_task_comments(task_id))["comments"]


# -- aq task route: a router re-run ---------------------------------------------------


async def test_route_clears_the_route_and_the_router_routes_the_task_again(handler, orch):
    """Acceptance 1: cleared on an unclaimed task, re-routed on the next cascade."""
    db = orch.db
    await _create(db, "t", task_type=TaskType.RESEARCH,
                  route={"constraints": {"exclude_providers": ["claude"]}})
    assert (await _route(handler, "t"))["outcome"] == "routed"
    routed = await db.get_task("t")
    assert (routed.route_source, routed.profile_id) == (ROUTER, "standard-high-codex")
    _ready(orch, "p")
    orch._route_needed_emitted = {"t": time.time()}  # throttled: asked moments ago

    result = await handler.execute("task_route", {
        "task_id": "t", "intelligence_class": "deep-high", "task_type": "bugfix",
        "reason": "the fix is harder than the filer thought",
    })

    assert result["success"] is True, result
    assert result["route_source"] == UNROUTED
    assert result["cleared"] == {
        "route_source": ROUTER, "profile_id": "standard-high-codex",
        "intelligence_class": "standard-high", "provider_intent": "class_only",
    }
    task = await db.get_task("t")
    assert (task.profile_id, task.intelligence_class, task.route_source) == (None, None, UNROUTED)
    assert (task.class_hint, task.task_type) == ("deep-high", TaskType.BUGFIX)
    assert task.provider_intent == "class_only"
    # The route record goes; the task's own constraints stay for the re-plan.
    assert task.route == {"constraints": {"exclude_providers": ["claude"]}}
    assert "t" not in orch._route_needed_emitted
    [comment] = await _comments(db, "t")
    assert "the fix is harder than the filer thought" in comment["body"]

    # The next cascade asks the router at once, despite the recent emission.
    orch.bus.emit.reset_mock()
    await orch._emit_route_needed_events()
    [needed] = _events(orch, "task.route_needed")
    assert (needed["task_id"], needed["router"], needed["class_hint"]) == (
        "t", ROUTER_ID, "deep-high",
    )
    # ... and the router routes it with the new hints: deep-high, never on the
    # Claude cell the policy reserves for design, and not on excluded Claude.
    assert (await _route(handler, "t"))["outcome"] == "routed"
    again = await db.get_task("t")
    assert (again.route_source, again.intelligence_class, again.profile_id) == (
        ROUTER, "deep-high", "deep-high-codex",
    )


async def test_route_keeps_hints_it_is_not_given_and_clears_an_empty_one(handler, orch):
    db = orch.db
    await _create(db, "t", task_type=TaskType.RESEARCH, class_hint="standard-high")
    assert (await handler.execute("task_route", {"task_id": "t"}))["success"]
    task = await db.get_task("t")
    assert (task.class_hint, task.task_type) == ("standard-high", TaskType.RESEARCH)
    assert await _comments(db, "t") == []  # no reason, no override: nothing to say
    assert (await handler.execute(
        "task_route", {"task_id": "t", "intelligence_class": "", "task_type": ""}
    ))["success"]
    task = await db.get_task("t")
    assert (task.class_hint, task.task_type) == (None, None)


async def test_route_validates_its_hints(handler, orch):
    await _create(orch.db, "t")
    bad_class = await handler.execute(
        "task_route", {"task_id": "t", "intelligence_class": "warp-speed"}
    )
    assert bad_class["success"] is False and "warp-speed" in bad_class["error"]
    bad_kind = await handler.execute("task_route", {"task_id": "t", "task_type": "vibes"})
    assert bad_kind["success"] is False and "Invalid task_type" in bad_kind["error"]
    assert await handler.execute("task_route", {"task_id": "nope"}) == {
        "success": False, "error": "task 'nope' not found",
    }


async def test_route_refuses_a_route_and_points_at_the_override(handler, orch):
    await _create(orch.db, "t")
    for argument, value in (("profile_id", "standard-high-codex"), ("pin", True),
                            ("provider_intent", "pinned"), ("provider", "codex")):
        refused = await handler.execute("task_route", {"task_id": "t", argument: value})
        assert refused["code"] == ROUTING_CHOICE_FORBIDDEN, refused
        assert "aq task route-override" in refused["error"]
    assert (await orch.db.get_task("t")).route_source == UNROUTED


@pytest.mark.parametrize("state", ["in_progress", "assigned", "completed", "role"])
async def test_route_refuses_a_task_no_route_may_change(handler, orch, state):
    kw = {
        "in_progress": {"status": TaskStatus.IN_PROGRESS},
        "assigned": {"status": TaskStatus.ASSIGNED},
        "completed": {"status": TaskStatus.COMPLETED},
        "role": {"profile_id": "reviewer", "route_source": ROLE,
                 "intelligence_class": "deep-high"},
    }[state]
    await _create(orch.db, "t", **kw)
    before = await orch.db.get_task("t")
    refused = await handler.execute("task_route", {"task_id": "t"})
    assert refused["success"] is False and refused["code"] == NOT_ROUTABLE, refused
    after = await orch.db.get_task("t")
    assert (after.route_source, after.profile_id) == (before.route_source, before.profile_id)


async def test_route_refuses_a_playbook_a_service_and_a_worker_that_did_not_file_it(
    handler, orch
):
    await _create(orch.db, "t", created_by_kind="session", created_by_id="filer")
    with _as_playbook():
        refused = await handler.execute("task_route", {"task_id": "t"})
    assert refused["code"] == NOT_PERMITTED and "playbook" in refused["error"]
    for principal in (ExecutionPrincipal.service("cascade"), PLAYBOOK, _session("someone")):
        refused = await _as(handler, principal, "task_route", {"task_id": "t"})
        assert refused["success"] is False and refused["code"] == NOT_PERMITTED, refused


async def test_route_admits_a_worker_for_its_own_filing_and_the_supervisor(handler, orch):
    db = orch.db
    await _create(db, "mine", created_by_kind="session", created_by_id="filer")
    routed = await _as(handler, _session("filer"), "task_route", {
        "task_id": "mine", "task_type": "docs", "reason": "it is a docs change",
    })
    assert routed["success"] is True, routed
    [comment] = await _comments(db, "mine")
    assert (comment["author_kind"], comment["author_id"]) == ("agent", "filer")

    await _create(db, "theirs")
    supervisor = await _supervisor(db)
    assert (await _as(handler, supervisor, "task_route", {"task_id": "theirs"}))["success"]
    stale = _session("not-a-live-supervisor", elevated=True)
    refused = await _as(handler, stale, "task_route", {"task_id": "theirs"})
    assert refused["code"] == NOT_PERMITTED
    assert "live named supervisor" in refused["error"]


# -- task_route_override: the audited emergency override ---------------------------


async def test_override_pins_the_task_and_audits_it(handler, orch):
    """Pinned to one candidate, gate resolved, evented and commented; route clears it."""
    db = orch.db
    await _create(db, "t", task_type=TaskType.RESEARCH, class_hint="standard-high",
                  route={"constraints": {"exclude_providers": ["codex"]}})
    gate_id, _ = await db.create_gate("p", "routing", "route me", waiter_task_ids=["t"])

    result = await handler.execute("task_route_override", {
        "task_id": "t", "profile_id": "standard-high-codex", "reason": REASON,
    })

    assert result["success"] is True, result
    assert result["by"] == "human:local-operator"
    assert result["resolved_gate_ids"] == [gate_id]
    task = await db.get_task("t")
    assert (task.route_source, task.profile_id, task.intelligence_class) == (
        OVERRIDE, "standard-high-codex", "standard-high",
    )
    assert task.provider_intent == "pinned"
    assert [c["profile_id"] for c in task.route["candidates"]] == ["standard-high-codex"]
    assert task.route["override"]["by"] == "human:local-operator"
    assert task.route["override"]["reason"] == REASON
    assert task.route["constraints"] == {"exclude_providers": ["codex"]}
    assert (await db.get_gate(gate_id))["status"] == "resolved"
    [event] = _events(orch, "task.route_overridden")
    assert (event["task_id"], event["profile_id"], event["by"], event["reason"]) == (
        "t", "standard-high-codex", "human:local-operator", REASON,
    )
    [comment] = await _comments(db, "t")
    assert REASON in comment["body"] and comment["author_kind"] == "user"

    # A later ``aq task route`` clears the override and says so on the task.
    cleared = await handler.execute("task_route", {"task_id": "t"})
    assert cleared["cleared"]["route_source"] == OVERRIDE
    task = await db.get_task("t")
    assert (task.route_source, task.profile_id) == (UNROUTED, None)
    assert "override" in (await _comments(db, "t"))[-1]["body"]


async def test_override_by_the_supervisor(handler, orch):
    await _create(orch.db, "t", class_hint="standard-high")
    supervisor = await _supervisor(orch.db)
    result = await _as(handler, supervisor, "task_route_override", {
        "task_id": "t", "profile_id": "standard-high-claude", "reason": REASON,
    })
    assert result["success"] is True, result
    assert result["by"] == "supervisor session:super"
    [comment] = await _comments(orch.db, "t")
    assert (comment["author_kind"], comment["author_id"]) == ("supervisor", "super")


async def test_override_refuses_workers_playbooks_and_tokens(handler, orch):
    """Acceptance 2: only the local operator and a live supervisor session."""
    await _create(orch.db, "t", class_hint="standard-high",
                  created_by_kind="session", created_by_id="filer")
    args = {"task_id": "t", "profile_id": "standard-high-claude", "reason": REASON}
    with _as_playbook():
        refused = await handler.execute("task_route_override", args)
    assert refused["code"] == NOT_PERMITTED
    for principal in (
        _session("filer"),                               # a worker, even the filer
        _session("api-token", elevated=True),            # an elevated token, no supervisor
        PLAYBOOK,
        ExecutionPrincipal.service("cascade"),
    ):
        refused = await _as(handler, principal, "task_route_override", args)
        assert refused["success"] is False and refused["code"] == NOT_PERMITTED, refused
    task = await orch.db.get_task("t")
    assert (task.route_source, task.profile_id) == (UNROUTED, None)
    assert _events(orch, "task.route_overridden") == []


@pytest.mark.parametrize("reason", [None, "", "too short", "x" * 401])
async def test_override_requires_a_reason(handler, orch, reason):
    await _create(orch.db, "t", class_hint="standard-high")
    args = {"task_id": "t", "profile_id": "standard-high-claude"}
    if reason is not None:
        args["reason"] = reason
    refused = await handler.execute("task_route_override", args)
    assert refused["success"] is False and refused["code"] == INVALID_OVERRIDE, refused
    assert "10 to 400 characters" in refused["error"]
    assert (await orch.db.get_task("t")).route_source == UNROUTED


async def test_override_refuses_what_cannot_run_the_task(handler, orch):
    db = orch.db
    await db.create_profile(AgentProfile(id="supervisor", name="S", harness="claude"))
    await db.create_profile(AgentProfile(
        id="resident", name="R", harness="claude", default_class="standard-high",
        lifecycle="named",
    ))
    await _create(db, "t", class_hint="standard-high")

    async def refusal(profile_id, **extra):
        result = await handler.execute("task_route_override", {
            "task_id": "t", "profile_id": profile_id, "reason": REASON, **extra,
        })
        assert result["success"] is False, result
        return result

    assert "supervisor" in (await refusal("supervisor"))["error"]
    assert "role profile" in (await refusal("reviewer"))["error"]
    assert "not a worker candidate" in (await refusal("resident"))["error"]
    assert "fixed class 'standard-high'" in (
        await refusal("standard-high-claude", intelligence_class="deep-high")
    )["error"]
    assert "not found" in (await refusal("nope"))["error"]
    assert (await db.get_task("t")).route_source == UNROUTED

    await _create(db, "running", status=TaskStatus.IN_PROGRESS, class_hint="standard-high")
    claimed = await handler.execute("task_route_override", {
        "task_id": "running", "profile_id": "standard-high-claude", "reason": REASON,
    })
    assert claimed["code"] == NOT_ROUTABLE


async def test_gate_resolve_refuses_routing(handler, orch):
    await _create(orch.db, "t")
    gate_id, _ = await orch.db.create_gate("p", "routing", "Route", waiter_task_ids=["t"])
    refused = await handler.execute("gate_resolve", {"gate_id": gate_id, "resolved_by": "human"})
    assert refused["success"] is False
    assert "task_route_apply" in refused["error"]
    assert "aq task route-override" in refused["error"]


# -- aq task explain: why the router has not routed a task -------------------------------


async def _explain(handler, task_id: str) -> dict:
    return await handler.execute("explain_task", {"task_id": task_id})


def _route_reason(result: dict) -> dict:
    codes = {
        "awaiting_route", "route_failed", "route_held", "route_no_candidates",
        "router_not_ready", "router_unbound",
    }
    [reason] = [r for r in result["reasons"] if r["code"] in codes]
    return reason


async def test_explain_names_an_unbound_and_a_not_ready_router(handler, orch):
    """The same binding verdict as ``aq doctor --check routing.bypassed``."""
    await _create(orch.db, "t")
    # Bound to the default router, which has no activation here: not ready.
    reason = _route_reason(await _explain(handler, "t"))
    assert (reason["code"], reason["ref"]) == ("router_not_ready", ROUTER_ID)
    assert "has no system activation" in reason["detail"]

    # Bound to a playbook with no activation at all: nothing routes it.
    await orch.db.update_project("p", assignment_playbook_id="gone-router")
    reason = _route_reason(await _explain(handler, "t"))
    assert (reason["code"], reason["ref"]) == ("router_unbound", "p")
    assert "gone-router" in reason["detail"]


async def test_explain_names_awaiting_route_with_the_last_emission(handler, orch):
    await _create(orch.db, "t")
    _ready(orch, "p")
    orch._route_needed_emitted = {}
    reason = _route_reason(await _explain(handler, "t"))
    assert reason["code"] == "awaiting_route" and "once the task" in reason["detail"]
    emitted_at = time.time() - 30
    orch._route_needed_emitted = {"t": emitted_at}
    reason = _route_reason(await _explain(handler, "t"))
    # Explain reads its own clock, so a slow runner can tick past the 30s mark.
    elapsed = int(time.time() - emitted_at)
    assert reason["code"] == "awaiting_route"
    ago = re.search(r"last emitted (\d+)s ago", reason["detail"])
    assert ago is not None and 30 <= int(ago.group(1)) <= elapsed, reason["detail"]


async def _seed_run(db, run_id: str, task_id: str, *, lifecycle: str, started_at: float,
                    bindings: dict | None = None, error: str | None = None) -> None:
    from sqlalchemy import insert, select

    from src.database.tables import playbook_artifacts, playbook_v2_runs

    digest = "sha256:" + "d" * 64
    snapshot = {"event": {"task_id": task_id, "_event_type": "task.route_needed"},
                "bindings": bindings or {}, "lifecycle": lifecycle}
    async with db.immediate() as conn:
        if (await conn.execute(select(playbook_artifacts.c.artifact_sha256).where(
            playbook_artifacts.c.artifact_sha256 == digest
        ))).first() is None:
            await conn.execute(insert(playbook_artifacts).values(
                artifact_sha256=digest, playbook_id=ROUTER_ID, scope="system",
                scope_identifier="", schema_generation=2, version=1,
                source_digest="sha256:" + "e" * 64, contract_fingerprint="sha256:" + "f" * 64,
                profile_fingerprint="", compiler_build="test", path="artifacts/d.json",
                size_bytes=1, validation="{}", compiled_at=None, created_at=started_at,
            ))
        await conn.execute(insert(playbook_v2_runs).values(
            run_id=run_id, playbook_id=ROUTER_ID, artifact_sha256=digest,
            rule_id="route-task", lifecycle=lifecycle, event_type="task.route_needed",
            snapshot=json.dumps(snapshot), error=error, started_at=started_at,
            updated_at=started_at,
            completed_at=None if lifecycle == "running" else started_at + 1,
        ))


@pytest.mark.parametrize(
    ("lifecycle", "bindings", "error", "code", "ref", "detail"),
    [
        ("failed", {}, "the plan step raised", "route_failed", "run-new",
         "the plan step raised"),
        ("failed", {"plan_a": {"task_id": "t", "reason": "workspace_requirement",
                               "detail": "feature: no worker candidate at class deep-high"}},
         None, "route_no_candidates", "workspace_requirement", "workspace_requirement"),
        ("completed", {"plan_a": {"task_id": "t", "providers": ["claude", "codex"],
                                  "candidates": [{"profile_id": "standard-high-codex"}]}},
         None, "route_held", "claude, codex", "claude, codex"),
        ("completed", {"plan_a": {"task_id": "t", "questions": ["narrow?"]},
                       "plan_b": {"task_id": "t", "providers": ["codex"]}},
         None, "route_held", "codex", "codex"),
        ("running", {}, None, "awaiting_route", "run-new", "is running"),
    ],
)
async def test_explain_reads_the_routers_latest_run_for_the_task(
    handler, orch, lifecycle, bindings, error, code, ref, detail
):
    """Acceptance 3: route_failed, route_no_candidates and route_held from the run."""
    db = orch.db
    await _create(db, "t")
    await _create(db, "other")
    _ready(orch, "p")
    now = time.time()
    await _seed_run(db, "run-old", "t", lifecycle="completed", started_at=now - 600,
                    bindings={"plan_a": {"task_id": "t", "profile_id": "x"}})
    await _seed_run(db, "run-new", "t", lifecycle=lifecycle, started_at=now - 60,
                    bindings=bindings, error=error)
    await _seed_run(db, "run-other", "other", lifecycle="failed", started_at=now - 5)
    await _seed_run(db, "run-stale", "t", lifecycle="failed", started_at=now - 10 * 3600)

    reason = _route_reason(await _explain(handler, "t"))
    assert (reason["code"], reason["ref"]) == (code, ref), reason
    assert detail in reason["detail"]
    if code != "awaiting_route":
        assert "run-new" in reason["detail"]


async def test_explain_prints_the_route_of_a_routed_and_an_overridden_task(handler, orch):
    db = orch.db
    await _create(db, "routed", task_type=TaskType.RESEARCH)
    await _route(handler, "routed")
    route = (await _explain(handler, "routed"))["assignment_route"]
    assert (route["source"], route["profile_id"], route["intelligence_class"]) == (
        ROUTER, "standard-high-codex", "standard-high",
    )
    assert route["rule"] and route["reason"] and route["playbook_run_id"] == "run-1"
    assert route["playbook_id"] == ROUTER_ID and route["override"] is None

    await _create(db, "pinned", class_hint="standard-high")
    await handler.execute("task_route_override", {
        "task_id": "pinned", "profile_id": "standard-high-claude", "reason": REASON,
    })
    result = await _explain(handler, "pinned")
    route = result["assignment_route"]
    assert (route["source"], route["provider_intent"]) == (OVERRIDE, "pinned")
    assert route["override"]["reason"] == REASON
    assert not {"awaiting_route", "router_not_ready"} & set(result["reason_codes"])


async def test_explain_gives_no_route_reason_for_a_container_or_claimable_legacy(
    handler, orch
):
    db = orch.db
    await _create(db, "parent", status=TaskStatus.DEFINED)
    await _create(db, "child", parent_task_id="parent", status=TaskStatus.DEFINED)
    await _create(db, "legacy", profile_id="standard-high-codex",
                  intelligence_class="standard-high")
    _ready(orch)  # not ready: legacy work stays claimable
    assert "router_not_ready" not in (await _explain(handler, "parent"))["reason_codes"]
    legacy = await _explain(handler, "legacy")
    assert "route_waiting_for_compatible_agent" in legacy["reason_codes"]
    _ready(orch, "p")  # ready: a legacy route is no route (§6.1)
    assert _route_reason(await _explain(handler, "legacy"))["code"] == "awaiting_route"

