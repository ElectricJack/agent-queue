"""The re-route engine (provider-failover D11-D17).

Two tiers:

* the pure planner (:func:`plan_sweep`) against a hand-built
  :class:`PlanContext` -- every D12 rule, the D14 target rule, the D15 trickle
  and per-task limits, and a randomised property check that no sweep sequence
  ever queues more moved work on a rung than its trickle allows or touches a
  bound;
* the service and the commands on real PostgreSQL through an initialised
  orchestrator: with ``codex`` unavailable a ``preferred``
  ``standard-high-codex`` task moves to ``standard-high-claude``, a pinned one
  holds with ``provider_pinned``, an ``astra-high`` task holds with
  ``no_equivalent_rung``; every move is recorded on the task and reversible;
  routing options exclude the unavailable provider; the project default is
  derived, never persisted.

No LLM, no CLI.
"""

from __future__ import annotations

import random
from types import SimpleNamespace

import pytest

from src.config import ProviderFailoverConfig
from src.intelligence_classes import IntelligenceClass
from src.models import AgentProfile, Task, TaskStatus
from src.providers.availability import AVAILABLE, DEGRADED, DISABLED, EXHAUSTED, UNAUTHENTICATED
from src.providers.intent import CLASS_ONLY, PINNED, PREFERRED
from src.providers.reroute import (
    Candidate,
    PlanContext,
    Rung,
    batch_id_for,
    plan_sweep,
    provider_order,
)
from src.sessions.harness_parser import Harness

# -- the pure planner -------------------------------------------------------------


def _ctx(
    *,
    states=None,
    rungs=None,
    backlog=None,
    stats=None,
    config=None,
    default_providers=None,
    now=10_000.0,
) -> PlanContext:
    rungs = rungs or {
        "std-high-codex": Rung("std-high-codex", "codex", "std-high", "codex", "pool", True, 2),
        "std-high-claude": Rung("std-high-claude", "claude", "std-high", "claude", "pool", True, 2),
        "astra-high-codex": Rung(
            "astra-high-codex", "codex", "astra-high", "codex", "pool", True, 2
        ),
    }
    providers = {pid: rung.provider for pid, rung in rungs.items()}
    providers.setdefault("supervisor", "codex")
    return PlanContext(
        rungs=rungs,
        profile_providers=providers,
        states=states if states is not None else {"codex": UNAUTHENTICATED, "claude": AVAILABLE},
        generations={"codex": 3, "claude": 1},
        backlog=backlog or {},
        stats=stats or {},
        default_providers=default_providers or {},
        session_providers=frozenset({r.provider for r in rungs.values()}),
        config=config or ProviderFailoverConfig(),
        now=now,
    )


def _cand(task_id, profile="std-high-codex", *, intent=PREFERRED, priority=100, **kw):
    return Candidate(
        task_id=task_id,
        project_id=kw.pop("project_id", "p"),
        profile_id=profile,
        priority=priority,
        created_at=kw.pop("created_at", float(len(task_id))),
        intent=intent,
        **kw,
    )


def _by_id(decisions):
    return {d.task_id: d for d in decisions}


def test_preferred_moves_to_the_equivalent_rung_on_an_available_provider():
    [d] = plan_sweep([_cand("t1")], _ctx())
    assert d.action == "move"
    assert (d.to_profile_id, d.to_provider) == ("std-high-claude", "claude")
    assert d.to_class is None  # the class never changes automatically
    assert d.provider_generation == 3


def test_class_only_fails_over_like_preferred():
    [d] = plan_sweep([_cand("t1", intent=CLASS_ONLY)], _ctx())
    assert d.action == "move" and d.to_profile_id == "std-high-claude"


def test_pinned_holds_with_provider_pinned():
    [d] = plan_sweep([_cand("t1", intent=PINNED)], _ctx())
    assert (d.action, d.kind) == ("hold", "provider_pinned")


def test_single_provider_class_holds_by_construction():
    [d] = plan_sweep([_cand("t1", "astra-high-codex")], _ctx())
    assert (d.action, d.kind) == ("hold", "no_equivalent_rung")


def test_class_policy_hold():
    cfg = ProviderFailoverConfig(classes={"std-high": "hold"})
    [d] = plan_sweep([_cand("t1")], _ctx(config=cfg))
    assert (d.action, d.kind) == ("hold", "class_policy_hold")


def test_default_policy_hold():
    cfg = ProviderFailoverConfig(default_policy="hold")
    [d] = plan_sweep([_cand("t1")], _ctx(config=cfg))
    assert d.kind == "class_policy_hold"


def test_a_degraded_provider_is_never_a_target_unless_allowed():
    ctx = _ctx(states={"codex": EXHAUSTED, "claude": DEGRADED})
    [d] = plan_sweep([_cand("t1")], ctx)
    assert (d.action, d.kind) == ("hold", "no_available_target")
    cfg = ProviderFailoverConfig()
    cfg.reroute.allow_degraded_target = True
    [d] = plan_sweep([_cand("t1")], _ctx(states={"codex": EXHAUSTED, "claude": DEGRADED},
                                         config=cfg))
    assert d.action == "move"


def test_a_disabled_target_pool_is_not_a_target():
    rungs = {
        "std-high-codex": Rung("std-high-codex", "codex", "std-high", "codex", "pool", True, 2),
        "std-high-claude": Rung(
            "std-high-claude", "claude", "std-high", "claude", "pool", False, 2
        ),
    }
    [d] = plan_sweep([_cand("t1")], _ctx(rungs=rungs))
    assert (d.action, d.kind) == ("hold", "no_equivalent_rung")


def test_every_provider_down_holds_everything_and_moves_nothing():
    ctx = _ctx(states={"codex": UNAUTHENTICATED, "claude": DISABLED})
    decisions = plan_sweep([_cand("t1"), _cand("t2", intent=CLASS_ONLY)], ctx)
    assert {d.kind for d in decisions} == {"all_providers_unavailable"}
    assert not [d for d in decisions if d.action == "move"]


def test_a_task_on_a_launchable_provider_is_skipped():
    [d] = plan_sweep([_cand("t1", "std-high-claude")], _ctx())
    assert d.action == "skip"


def test_role_profile_holds_no_equivalent_rung():
    [d] = plan_sweep([_cand("t1", "supervisor", intelligence_class="std-high")], _ctx())
    assert (d.action, d.kind) == ("hold", "no_equivalent_rung")


def test_trickle_keeps_one_pool_width_moved_and_tops_up_in_priority_order():
    rungs = {
        "std-high-codex": Rung("std-high-codex", "codex", "std-high", "codex", "pool", True, 3),
        "std-high-claude": Rung(
            "std-high-claude", "claude", "std-high", "claude", "pool", True, 1
        ),
    }
    cands = [
        _cand("low", priority=200, created_at=1.0),
        _cand("urgent", priority=5, created_at=3.0),
        _cand("mid", priority=100, created_at=2.0),
    ]
    decisions = _by_id(plan_sweep(cands, _ctx(rungs=rungs)))
    assert decisions["urgent"].action == "move"
    assert (decisions["mid"].kind, decisions["mid"].ahead) == ("awaiting_failover_capacity", 0)
    assert (decisions["low"].kind, decisions["low"].ahead) == ("awaiting_failover_capacity", 1)
    # Once the moved task is claimed, the next sweep tops up with the next one.
    decisions = _by_id(plan_sweep(cands[:1] + cands[2:], _ctx(rungs=rungs, backlog={})))
    assert decisions["mid"].action == "move"
    # ... but not while it still sits in the target's queue.
    decisions = _by_id(
        plan_sweep(cands[:1] + cands[2:], _ctx(rungs=rungs, backlog={"std-high-claude": 1}))
    )
    assert decisions["mid"].kind == "awaiting_failover_capacity"


def test_target_backlog_factor_scales_the_trickle():
    cfg = ProviderFailoverConfig()
    cfg.reroute.target_backlog_factor = 2.0
    decisions = plan_sweep([_cand(f"t{i}") for i in range(6)], _ctx(config=cfg))
    assert sum(d.action == "move" for d in decisions) == 4  # ceil(2.0 * 2)


def test_max_per_sweep():
    cfg = ProviderFailoverConfig()
    cfg.reroute.max_per_sweep = 1
    cfg.reroute.target_backlog_factor = 10.0
    decisions = plan_sweep([_cand(f"t{i}") for i in range(3)], _ctx(config=cfg))
    assert [d.action for d in decisions].count("move") == 1


def test_task_cooldown_and_max_auto_per_task():
    stats = {
        "cool": {"auto_count": 1, "last_auto_at": 10_000.0 - 60, "left_providers": set()},
        "tired": {"auto_count": 2, "last_auto_at": 1.0, "left_providers": set()},
        "fine": {"auto_count": 1, "last_auto_at": 1.0, "left_providers": set()},
    }
    decisions = _by_id(
        plan_sweep([_cand("cool"), _cand("tired"), _cand("fine")], _ctx(stats=stats))
    )
    assert decisions["cool"].kind == "reroute_limit_reached"
    assert "cooldown" in decisions["cool"].detail
    assert decisions["tired"].kind == "reroute_limit_reached"
    assert decisions["fine"].action == "move"


def test_a_provider_the_task_already_left_is_skipped():
    rungs = {
        "std-high-codex": Rung("std-high-codex", "codex", "std-high", "codex", "pool", True, 2),
        "std-high-claude": Rung(
            "std-high-claude", "claude", "std-high", "claude", "pool", True, 2
        ),
    }
    stats = {"t1": {"auto_count": 1, "last_auto_at": 1.0, "left_providers": {"claude"}}}
    [d] = plan_sweep([_cand("t1")], _ctx(rungs=rungs, stats=stats))
    assert (d.action, d.kind) == ("hold", "no_equivalent_rung")


def test_max_priority_value_moves_only_urgent_work():
    cfg = ProviderFailoverConfig()
    cfg.reroute.max_priority_value = 50
    decisions = _by_id(plan_sweep([_cand("urgent", priority=10), _cand("slow")], _ctx(config=cfg)))
    assert decisions["urgent"].action == "move"
    assert decisions["slow"].kind == "priority_policy_hold"


def test_provider_paused_tasks_resume_and_legacy_pauses_need_include_paused():
    paused = _cand("pp", status="PAUSED", provider_pause={"provider": "codex"})
    legacy = _cand("lp", status="PAUSED", legacy_pause=True)
    pinned_pause = _cand("pinp", status="PAUSED", intent=PINNED, provider_pause={"p": 1})
    decisions = _by_id(plan_sweep([paused, legacy, pinned_pause], _ctx()))
    assert decisions["pp"].resume and decisions["pp"].action == "move"
    assert decisions["lp"].action == "skip" and not decisions["lp"].resume
    # A pinned provider-paused task still returns to READY, then holds.
    assert decisions["pinp"].resume and decisions["pinp"].kind == "provider_pinned"
    decisions = _by_id(plan_sweep([legacy], _ctx(), include_paused=True))
    assert decisions["lp"].resume and decisions["lp"].action == "move"


def test_force_moves_a_pin_and_to_profile_cross_class_needs_force():
    ctx = _ctx()
    [d] = plan_sweep([_cand("t1", intent=PINNED)], ctx, force=True, explicit=True)
    assert d.action == "move"
    [d] = plan_sweep(
        [_cand("t1", "astra-high-codex")], ctx, to_profile="std-high-claude", explicit=True
    )
    assert (d.action, d.kind) == ("hold", "no_equivalent_rung")
    [d] = plan_sweep(
        [_cand("t1", "astra-high-codex")],
        ctx,
        to_profile="std-high-claude",
        explicit=True,
        force=True,
    )
    assert d.action == "move" and d.to_class == "std-high"


def test_an_explicit_move_off_a_healthy_provider_needs_to_profile():
    ctx = _ctx(states={"codex": AVAILABLE, "claude": AVAILABLE})
    [d] = plan_sweep([_cand("t1")], ctx, explicit=True)
    assert d.action == "skip"
    [d] = plan_sweep([_cand("t1")], ctx, explicit=True, to_profile="std-high-claude", force=True)
    assert d.action == "move"


def test_provider_order_config_and_project_default():
    ctx = _ctx(default_providers={"p": "claude"})
    assert provider_order(ctx, "p")[0] == "claude"
    cfg = ProviderFailoverConfig(order=["codex", "claude"])
    assert provider_order(_ctx(config=cfg), "p")[:2] == ["codex", "claude"]


def test_batch_id_is_one_outage():
    assert batch_id_for("codex", 7) == "prb-codex-7"


def test_property_no_sweep_sequence_exceeds_the_trickle_or_touches_a_bound():
    """After any sequence of sweeps and claims, moved-and-queued work on every
    rung stays within its trickle, and the planner never changes a rung's
    bounds (D14: failover raises no ceiling; claims are what run the work, and
    they are bounded by ``max_active`` unchanged)."""
    rng = random.Random(20260920)
    for _trial in range(60):
        cap = rng.randint(1, 3)
        rungs = {
            "a-codex": Rung("a-codex", "codex", "a", "codex", "pool", True, rng.randint(1, 3)),
            "a-claude": Rung("a-claude", "claude", "a", "claude", "pool", True, cap),
        }
        before = dict(rungs)
        cfg = ProviderFailoverConfig()
        cfg.reroute.max_per_sweep = rng.randint(1, 5)
        queue = [_cand(f"t{i}", "a-codex", priority=rng.randint(1, 200)) for i in range(12)]
        backlog = 0
        for _sweep in range(8):
            decisions = plan_sweep(
                queue, _ctx(rungs=rungs, backlog={"a-claude": backlog}, config=cfg)
            )
            moved = [d for d in decisions if d.action == "move"]
            assert len(moved) <= cfg.reroute.max_per_sweep
            backlog += len(moved)
            assert backlog <= max(1, cap)
            moved_ids = {d.task_id for d in moved}
            queue = [c for c in queue if c.task_id not in moved_ids]
            # Some of the moved work is claimed (never more than max_active).
            backlog -= rng.randint(0, min(backlog, cap))
        assert rungs == before


# -- the service on PostgreSQL ------------------------------------------------------

CLASSES = {
    "standard-high": IntelligenceClass(
        "standard-high",
        "Standard high",
        "",
        {"anthropic": {"model": "claude-opus-5"}, "openai": {"model": "gpt-6"}},
    ),
    "astra-high": IntelligenceClass(
        "astra-high", "Astra", "", {"openai": {"model": "gpt-6-astra"}}
    ),
}


@pytest.fixture
async def orch(tmp_path):
    from tests.session_dispatch_helpers import create_session_project, make_session_orch

    orch = await make_session_orch(tmp_path)
    orch.session_spec_builder._intelligence_classes = dict(CLASSES)
    orch.harness_registry.upsert(
        Harness(id="codex", name="codex", command="codex", prompt_mode="arg",
                process_names=("codex",))
    )

    async def probe(provider, timeout):
        return "cannot_tell"

    orch.provider_availability._probe_impl = probe
    # The shipped rungs are seeded by ``initialize``; make the three this
    # suite reasons about pools of two.
    for pid, harness, cls in (
        ("standard-high-claude", "claude", "standard-high"),
        ("standard-high-codex", "codex", "standard-high"),
        ("astra-high-codex", "codex", "astra-high"),
    ):
        if await orch.db.get_profile(pid) is None:
            await orch.db.create_profile(
                AgentProfile(id=pid, name=pid, harness=harness, default_class=cls)
            )
        await orch.db.update_profile(pid, lifecycle="pool", max_active=2)
    await create_session_project(orch)
    yield orch
    await orch.wait_for_running_tasks(timeout=5)
    await orch.provider_availability.close()
    await orch.db.close()


async def _task(orch, task_id, profile, *, intent=PREFERRED, cls="standard-high", priority=100,
                status=TaskStatus.READY):
    await orch.db.create_task(
        Task(
            id=task_id,
            project_id="p-1",
            title=task_id,
            description="d",
            status=status,
            priority=priority,
            profile_id=profile,
            intelligence_class=cls,
            provider_intent=intent,
        )
    )


async def _codex_down(orch):
    await orch.provider_availability.set_state(
        "codex", "disabled", by="human:test", reason="out of usage", until=None
    )
    assert orch.provider_availability.suppresses("codex")


def _handler(orch):
    from src.commands.handler import CommandHandler

    return CommandHandler(orch, orch.config)


async def test_acceptance_codex_unavailable(orch):
    """The epic's criterion: preferred moves, pinned holds with a reason, astra
    holds, and every move is recorded and reversible."""
    await _task(orch, "pref", "standard-high-codex", intent=PREFERRED, priority=10)
    await _task(orch, "pin", "standard-high-codex", intent=PINNED)
    await _task(orch, "astra", "astra-high-codex", cls="astra-high")
    await _codex_down(orch)
    handler = _handler(orch)

    plan = await handler.execute("provider_reroute", {"dry_run": True})
    assert plan["outcome"] == "rerouted" and plan["applied"] is False
    assert (await orch.db.get_task("pref")).profile_id == "standard-high-codex"

    result = await handler.execute("provider_reroute", {})
    assert result["outcome"] == "rerouted", result
    assert [d["task_id"] for d in result["moved"]] == ["pref"]
    held = {d["task_id"]: d["kind"] for d in result["held"]}
    assert held == {"pin": "provider_pinned", "astra": "no_equivalent_rung"}
    generation = orch.provider_availability.row("codex").generation
    assert result["batch_ids"] == [f"prb-codex-{generation}"]

    moved = await orch.db.get_task("pref")
    assert moved.profile_id == "standard-high-claude"
    assert moved.rerouted_from == "standard-high-codex"
    assert moved.provider_intent == PREFERRED  # intent never changes on a re-route
    assert moved.intelligence_class == "standard-high"
    [row] = await orch.db.list_task_reroutes(task_id="pref")
    assert row["reason_code"] == "provider_unavailable"
    assert (row["from_provider"], row["to_provider"]) == ("codex", "claude")
    assert row["provider_state"] == DISABLED
    comments = (await orch.db.list_task_comments("pref"))["comments"]
    assert any("Re-routed from `standard-high-codex`" in c["body"] for c in comments)

    # ``aq provider status`` counts what the outage moved (D20).
    status = await handler.execute("provider_status", {"provider": "codex"})
    [codex] = status["providers"]
    assert codex["rerouted"] == 1 and codex["batch_id"] == result["batch_ids"][0]

    # One supervisor notice for the project, once per batch.
    notices = await orch.db.list_messages(to_kind="session", to_id="supervisor-p-1")
    assert len(notices) == 1 and "pref" in notices[0].body and "pin" in notices[0].body
    again = await handler.execute("provider_reroute", {})
    assert again["outcome"] == "held" and again["notices"] == []
    assert len(await orch.db.list_messages(to_kind="session", to_id="supervisor-p-1")) == 1

    # The pinned task explains itself (D18).
    hold = await orch.provider_availability.hold_for(await orch.db.get_task("pin"))
    assert hold["kind"] == "provider_pinned" and hold["provider"] == "codex"
    hold = await orch.provider_availability.hold_for(await orch.db.get_task("astra"))
    assert hold["kind"] == "no_equivalent_rung"

    # Undo is refused while codex is still down, unless forced.
    refused = await handler.execute("provider_reroute_undo", {"task_id": ["pref"]})
    assert refused["success"] is False and "still" in refused["error"]
    await orch.provider_availability.set_state("codex", "auto", by="human:test")
    undone = await handler.execute("provider_reroute_undo", {"batch_id": result["batch_ids"][0]})
    assert undone["outcome"] == "undone", undone
    back = await orch.db.get_task("pref")
    assert back.profile_id == "standard-high-codex" and back.rerouted_from is None
    rows = await orch.db.list_task_reroutes(task_id="pref")
    assert [r["reason_code"] for r in rows] == ["operator_undo", "provider_unavailable"]
    assert rows[1]["undone_at"] is not None


async def test_undo_refuses_a_running_task(orch):
    await _task(orch, "t", "standard-high-codex")
    await _codex_down(orch)
    handler = _handler(orch)
    await handler.execute("provider_reroute", {})
    await orch.db.update_task("t", status=TaskStatus.IN_PROGRESS)
    result = await handler.execute("provider_reroute_undo", {"task_id": ["t"], "force": True})
    assert result["success"] is False and "running or claimed" in result["error"]


async def test_force_moves_a_pin_and_records_the_actor(orch):
    await _task(orch, "pin", "standard-high-codex", intent=PINNED)
    await _codex_down(orch)
    result = await _handler(orch).execute(
        "provider_reroute", {"task_id": ["pin"], "force": True}
    )
    assert [d["task_id"] for d in result["moved"]] == ["pin"], result
    [row] = await orch.db.list_task_reroutes(task_id="pin")
    assert row["reason_code"] == "operator_forced"
    assert row["actor"].startswith("human:")
    assert row["batch_id"].startswith("prf-")
    assert (await orch.db.get_task("pin")).provider_intent == PINNED


async def test_mode_observe_plans_but_never_moves(orch):
    await _task(orch, "t", "standard-high-codex")
    await _codex_down(orch)
    orch.config.provider_failover.mode = "observe"
    try:
        result = await _handler(orch).execute("provider_reroute", {})
    finally:
        orch.config.provider_failover.mode = "enforce"
    assert result["outcome"] == "disabled" and result["applied"] is False
    assert (await orch.db.get_task("t")).profile_id == "standard-high-codex"


async def test_nothing_unavailable_is_idle(orch):
    await _task(orch, "t", "standard-high-codex")
    result = await _handler(orch).execute("provider_reroute", {})
    assert result["outcome"] == "idle"


async def test_worker_token_is_out_of_scope(orch):
    handler = _handler(orch)
    handler._current_scope = {"kind": "session", "session_id": "s", "elevated": False}
    try:
        result = await handler._cmd_provider_reroute({})
    finally:
        handler._current_scope = None
    assert result["success"] is False and result["error"].startswith("out of scope")


async def test_provider_paused_task_resumes_then_moves(orch):
    await _task(orch, "pp", "standard-high-codex", status=TaskStatus.READY)
    await orch.db.transition_task(
        "pp", TaskStatus.PAUSED, context="test", resume_after=10**10
    )
    await orch.db.set_task_meta("pp", "provider_pause", {"provider": "codex", "state": "x"})
    await _task(orch, "legacy", "standard-high-codex", status=TaskStatus.READY)
    await orch.db.transition_task(
        "legacy", TaskStatus.PAUSED, context="test", resume_after=10**10
    )
    await _codex_down(orch)
    result = await _handler(orch).execute("provider_reroute", {})
    assert result["resumed"] == ["pp"], result
    task = await orch.db.get_task("pp")
    assert task.status == TaskStatus.READY and task.profile_id == "standard-high-claude"
    assert await orch.db.get_task_meta("pp", "provider_pause") is None
    assert (await orch.db.get_task("legacy")).status == TaskStatus.PAUSED
    result = await _handler(orch).execute(
        "provider_reroute", {"include_paused": True, "dry_run": True}
    )
    assert "legacy" in [d["task_id"] for d in result["moved"]]


async def test_route_options_exclude_the_unavailable_provider(orch):
    await _task(orch, "t", None, intent=CLASS_ONLY)
    await _codex_down(orch)
    result = await _handler(orch).execute("task_route_options", {"task_id": "t"})
    assert result["success"], result
    assert result["outcome"] == "explicit"
    assert result["explicit_profile_id"] == "standard-high-claude"
    assert all(o["provider_key"] != "codex" for o in result["options"])
    assert {o["profile_id"] for o in result["unavailable_options"]} >= {"standard-high-codex"}
    await _task(orch, "a", None, intent=CLASS_ONLY, cls="astra-high")
    result = await _handler(orch).execute("task_route_options", {"task_id": "a"})
    assert result["outcome"] == "held"


async def test_project_default_is_derived_never_persisted(orch):
    await orch.db.update_project("p-1", default_profile_id="standard-high-codex")
    project = await orch.db.get_project("p-1")
    assert await orch._effective_default_profile_id(project) == "standard-high-codex"
    await _codex_down(orch)
    assert await orch._effective_default_profile_id(project) == "standard-high-claude"
    assert (await orch.db.get_project("p-1")).default_profile_id == "standard-high-codex"
    await orch.provider_availability.set_state("codex", "auto", by="human:test")
    assert await orch._effective_default_profile_id(project) == "standard-high-codex"


async def test_recovery_leaves_moved_tasks_and_releases_holds(orch):
    await _task(orch, "pref", "standard-high-codex")
    await _task(orch, "pin", "standard-high-codex", intent=PINNED)
    await _codex_down(orch)
    await _handler(orch).execute("provider_reroute", {})
    await orch.provider_availability.set_state("codex", "auto", by="human:test")
    assert (await orch.db.get_task("pref")).profile_id == "standard-high-claude"
    assert await orch.provider_availability.hold_for(await orch.db.get_task("pin")) is None
    result = await _handler(orch).execute("provider_reroute", {})
    assert result["outcome"] == "idle"


async def test_claim_sql_references_no_provider_table():
    """D14: availability is enforced on the claiming session, never per candidate."""
    import inspect

    from src.database.queries import claim_queries

    source = inspect.getsource(claim_queries)
    for name in ("provider_availability", "task_reroutes"):
        assert name not in source


def test_decision_dicts_carry_the_api_model_fields():
    from src.api.models.provider import RerouteDecision

    [d] = plan_sweep([_cand("t1")], _ctx())
    RerouteDecision(**d.to_dict())
    assert SimpleNamespace  # keep the import used for readability of fixtures


# -- the direct path fails fast (D13a) ---------------------------------------------


async def test_llm_calls_fail_fast_while_the_llm_key_is_unavailable():
    from src.config import LLMConfig
    from src.llm.client import LLMClient
    from src.llm.fake import FakeProvider
    from src.llm.providers.errors import ProviderUnavailableError

    provider = FakeProvider()
    provider.add_text("hello")
    client = LLMClient.with_provider(provider, config=LLMConfig())
    client.availability_gate = lambda: "provider llm is unauthenticated: 401"
    with pytest.raises(ProviderUnavailableError):
        await client.complete("hi")
    assert provider.calls == []
    client.availability_gate = lambda: None
    assert (await client.complete("hi")).text == "hello"


async def test_llm_block_reason_follows_the_llm_key_in_enforce_mode():
    from src.config import AppConfig
    from src.providers.availability_service import ProviderAvailabilityService

    config = AppConfig()

    class Db:
        async def save_provider_availability(self, row, transition=None):
            return None

        async def log_event(self, *args, **kwargs):
            return None

        async def latest_provider_usage(self, provider=None):
            return []

    service = ProviderAvailabilityService(db=Db(), config_getter=lambda: config)
    assert service.llm_block_reason() is None
    await service.set_state("llm", "disabled", by="human:test", reason="rotating key")
    assert "llm is disabled" in service.llm_block_reason()
    config.provider_failover.mode = "observe"
    assert service.llm_block_reason() is None
