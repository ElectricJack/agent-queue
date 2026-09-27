"""Capacity spill (provider-failover D24): the pure planner, its view and its config.

Everything here is pure: a hand-built :class:`CapacityView` (or a
``PoolMeasurement`` the adapter folds into one) and a :class:`PlanContext` go
in, one :class:`Decision` per candidate comes out.  No database, no clock, no
LLM.  The sweep that gathers the snapshot and applies the plan is tested in
``tests/test_provider_reroute.py``.
"""

from __future__ import annotations

import random

import pytest
import yaml

from src.config import (
    PROVIDER_FAILOVER_SUBSECTIONS,
    ProviderFailoverConfig,
    ProviderFailoverSpillConfig,
    load_config,
    load_provider_failover_config,
)
from src.models import AgentProfile
from src.orchestrator.pools import PoolMeasurement
from src.providers.availability import AVAILABLE, DEGRADED, UNAUTHENTICATED
from src.providers.intent import CLASS_ONLY, PINNED, PREFERRED
from src.providers.reroute import PlanContext, Rung
from src.providers.spill import (
    SPILL_HOLD_KINDS,
    CapacityView,
    PoolCapacity,
    PoolProjectCapacity,
    SpillCandidate,
    capacity_view_from_measurement,
    plan_capacity_spill,
)
from src.scheduler import PlacementCandidate, PoolKey, PoolProjectSupply, PoolSupply

NOW = 100_000.0

RUNGS = {
    "std-high-opencode": Rung("std-high-opencode", "opencode", "std-high", "opencode", "pool"),
    "std-high-claude": Rung("std-high-claude", "claude", "std-high", "claude", "pool"),
    "std-high-codex": Rung("std-high-codex", "codex", "std-high", "codex", "pool"),
    "deep-high-claude": Rung("deep-high-claude", "claude", "deep-high", "claude", "pool"),
    "std-high-claude-task": Rung(
        "std-high-claude-task", "claude", "std-high", "claude", "task"
    ),
}


def _ctx(*, states=None, stats=None, config=None, disabled=(), now=NOW) -> PlanContext:
    rungs = {
        pid: Rung(r.profile_id, r.harness, r.class_id, r.provider, r.lifecycle,
                  pid not in disabled, r.capacity)
        for pid, r in RUNGS.items()
    }
    return PlanContext(
        rungs=rungs,
        profile_providers={pid: r.provider for pid, r in rungs.items()},
        states=states or {},
        stats=stats or {},
        session_providers=frozenset(r.provider for r in rungs.values()),
        config=config or ProviderFailoverConfig(),
        now=now,
    )


def _proj(*, idle=0, busy=0, starting=0, ready=0) -> PoolProjectCapacity:
    return PoolProjectCapacity(idle=idle, busy=busy, starting=starting, ready=ready)


def _pool(pid, *, max_active=None, enabled=True, quarantined=(), **projects) -> PoolCapacity:
    rung = RUNGS[pid]
    projs = dict(projects)
    return PoolCapacity(
        profile_id=pid,
        class_id=rung.class_id,
        provider=rung.provider,
        enabled=enabled,
        lifecycle="pool",
        max_active=max_active,
        live=sum(p.idle + p.busy + p.starting for p in projs.values()),
        idle=sum(p.idle for p in projs.values()),
        starting=sum(p.starting for p in projs.values()),
        ready=sum(p.ready for p in projs.values()),
        projects=projs,
        quarantined=frozenset(quarantined),
    )


def _view(*pools, room=None, headroom=None) -> CapacityView:
    return CapacityView(
        pools={pool.profile_id: pool for pool in pools},
        project_room=room if room is not None else {"p": 10, "q": 10},
        global_headroom=headroom,
    )


def _saturated_opencode(ready=1, **kw) -> PoolCapacity:
    """The 2026-09-26 shape: one seat, busy, work queued behind it."""
    return _pool("std-high-opencode", max_active=1, p=_proj(busy=1, ready=ready), **kw)


def _roomy_claude(**kw) -> PoolCapacity:
    return _pool("std-high-claude", max_active=4, p=_proj(busy=1), **kw)


def _cand(task_id, profile="std-high-opencode", *, age=600.0, intent=PREFERRED, **kw):
    return SpillCandidate(
        task_id=task_id,
        project_id=kw.pop("project_id", "p"),
        profile_id=profile,
        priority=kw.pop("priority", 100),
        created_at=kw.pop("created_at", 1.0),
        intelligence_class=kw.pop("intelligence_class", None),
        intent=intent,
        updated_at=NOW - age,
        **kw,
    )


def _plan(candidates, view, *, ctx=None, preferred=None):
    return plan_capacity_spill(
        candidates, ctx or _ctx(), view, preferred_providers=preferred or {}, now=NOW
    )


def _by_id(decisions):
    return {d.task_id: d for d in decisions}


# -- move and wait ----------------------------------------------------------------


def test_a_full_single_seat_rung_spills_to_a_same_class_rung_with_headroom():
    [d] = _plan([_cand("t1")], _view(_saturated_opencode(), _roomy_claude()))
    assert d.action == "move"
    assert (d.from_profile_id, d.from_provider) == ("std-high-opencode", "opencode")
    assert (d.to_profile_id, d.to_provider) == ("std-high-claude", "claude")
    assert d.to_class is None and d.provider_state == AVAILABLE
    # The detail is what the task comment quotes (D24, S7).
    assert "std-high-opencode had no free capacity for 10 min" in d.detail
    assert "1/1 live, 0 idle" in d.detail


def test_class_only_spills_like_preferred():
    [d] = _plan([_cand("t1", intent=CLASS_ONLY)], _view(_saturated_opencode(), _roomy_claude()))
    assert d.action == "move"


def test_below_the_threshold_it_holds_spill_waiting():
    [d] = _plan([_cand("t1", age=100.0)], _view(_saturated_opencode(), _roomy_claude()))
    assert (d.action, d.kind) == ("hold", "spill_waiting")
    assert "eligible in 200 s" in d.detail


def test_the_threshold_is_configurable_and_inclusive():
    cfg = ProviderFailoverConfig()
    cfg.spill.after_seconds = 60
    ctx = _ctx(config=cfg)
    [d] = _plan([_cand("t1", age=60.0)], _view(_saturated_opencode(), _roomy_claude()), ctx=ctx)
    assert d.action == "move"


# -- skip: the source can serve it ------------------------------------------------


def test_a_source_that_can_still_start_a_worker_is_skipped():
    source = _pool("std-high-opencode", max_active=2, p=_proj(busy=1, ready=1))
    [d] = _plan([_cand("t1")], _view(source, _roomy_claude()))
    assert d.action == "skip" and d.kind is None
    assert "can start another worker" in d.detail


def test_an_unbounded_source_under_global_headroom_is_skipped():
    source = _pool("std-high-opencode", p=_proj(busy=3, ready=1))
    [d] = _plan([_cand("t1")], _view(source, _roomy_claude(), headroom=2))
    assert d.action == "skip"


def test_a_source_with_an_idle_worker_in_the_project_is_skipped():
    source = _pool("std-high-opencode", max_active=1, p=_proj(idle=1, ready=1))
    [d] = _plan([_cand("t1")], _view(source, _roomy_claude()))
    assert d.action == "skip"
    assert "1 idle" in d.detail


def test_an_idle_worker_in_another_project_does_not_serve_this_one():
    source = _pool("std-high-opencode", max_active=1, p=_proj(ready=1), q=_proj(idle=1))
    [d] = _plan([_cand("t1")], _view(source, _roomy_claude()))
    assert d.action == "move"


def test_only_the_first_unserved_candidates_are_planned():
    # One seat, taken by a worker still booting: it will serve one task, so
    # two of the three waiting tasks are unserved.
    source = _pool("std-high-opencode", max_active=1, p=_proj(starting=1, ready=3))
    decisions = _plan(
        [_cand("t1", created_at=1.0), _cand("t2", created_at=2.0), _cand("t3", created_at=3.0)],
        _view(source, _roomy_claude()),
    )
    by_id = _by_id(decisions)
    assert [by_id[t].action for t in ("t1", "t2", "t3")] == ["move", "move", "skip"]


def test_a_source_on_an_unavailable_provider_is_left_to_failover():
    ctx = _ctx(states={"opencode": UNAUTHENTICATED})
    [d] = _plan([_cand("t1")], _view(_saturated_opencode(), _roomy_claude()), ctx=ctx)
    assert d.action == "skip"
    assert d.provider_state == UNAUTHENTICATED
    assert "failover" in d.detail


def test_a_project_the_measurement_did_not_cover_is_skipped():
    [d] = _plan([_cand("t1", project_id="paused")], _view(_saturated_opencode(), _roomy_claude()))
    assert d.action == "skip"
    assert "not an active project" in d.detail


def test_a_task_lifecycle_profile_is_not_a_spill_source():
    [d] = _plan([_cand("t1", "std-high-claude-task")], _view(_roomy_claude()))
    assert d.action == "skip"


def test_a_source_blocked_by_quarantine_or_project_room_spills():
    quarantined = _saturated_opencode(quarantined={"p"})
    unbounded = _pool("std-high-opencode", p=_proj(busy=1, ready=1), quarantined={"p"})
    [d] = _plan([_cand("t1")], _view(unbounded, _roomy_claude()))
    assert d.action == "move" and "quarantined" in d.detail
    [d] = _plan([_cand("t1")], _view(quarantined, _roomy_claude()))
    assert d.action == "move"
    # No project room: the source cannot start here, and neither can the
    # target -- only an idle worker's surplus in the project can take it.
    no_room = _pool("std-high-opencode", p=_proj(busy=1, ready=1))
    idle_claude = _pool("std-high-claude", max_active=4, p=_proj(idle=1))
    [d] = _plan([_cand("t1")], _view(no_room, idle_claude, room={"p": 0}))
    assert d.action == "move" and d.to_profile_id == "std-high-claude"
    [d] = _plan([_cand("t1")], _view(no_room, _roomy_claude(), room={"p": 0}))
    assert (d.action, d.kind) == ("hold", "spill_no_target")


# -- hold ------------------------------------------------------------------------


def test_pinned_holds():
    [d] = _plan([_cand("t1", intent=PINNED)], _view(_saturated_opencode(), _roomy_claude()))
    assert (d.action, d.kind) == ("hold", "spill_pinned")


def test_class_policy_hold_holds():
    cfg = ProviderFailoverConfig(classes={"std-high": "hold"})
    [d] = _plan([_cand("t1")], _view(_saturated_opencode(), _roomy_claude()),
                ctx=_ctx(config=cfg))
    assert (d.action, d.kind) == ("hold", "class_policy_hold")


def test_the_auto_move_limit_holds():
    stats = {"t1": {"auto_count": 2, "last_auto_at": NOW - 10_000, "left_providers": set()}}
    [d] = _plan([_cand("t1")], _view(_saturated_opencode(), _roomy_claude()),
                ctx=_ctx(stats=stats))
    assert (d.action, d.kind) == ("hold", "reroute_limit_reached")


def test_the_cooldown_holds():
    stats = {"t1": {"auto_count": 1, "last_auto_at": NOW - 60, "left_providers": set()}}
    [d] = _plan([_cand("t1")], _view(_saturated_opencode(), _roomy_claude()),
                ctx=_ctx(stats=stats))
    assert (d.action, d.kind) == ("hold", "reroute_limit_reached")
    assert "cooldown" in d.detail


def test_no_same_class_rung_holds_spill_no_target():
    source = _pool("deep-high-claude", max_active=1, p=_proj(busy=1, ready=1))
    [d] = _plan([_cand("t1", "deep-high-claude")], _view(source, _roomy_claude()))
    assert (d.action, d.kind) == ("hold", "spill_no_target")


# -- targets ---------------------------------------------------------------------


def test_a_degraded_target_is_never_chosen():
    codex = _pool("std-high-codex", max_active=2)
    view = _view(_saturated_opencode(), _roomy_claude(), codex)
    [d] = _plan([_cand("t1")], view, ctx=_ctx(states={"claude": DEGRADED}))
    assert d.to_profile_id == "std-high-codex"
    # allow_degraded_target is failover's knob; spill never takes a degraded target.
    cfg = ProviderFailoverConfig()
    cfg.reroute.allow_degraded_target = True
    [d] = _plan([_cand("t1")], view,
                ctx=_ctx(states={"claude": DEGRADED, "codex": DEGRADED}, config=cfg))
    assert (d.action, d.kind) == ("hold", "spill_no_target")


def test_a_disabled_target_is_never_chosen():
    codex = _pool("std-high-codex", max_active=2)
    view = _view(_saturated_opencode(), _roomy_claude(enabled=False), codex)
    [d] = _plan([_cand("t1")], view, ctx=_ctx(disabled={"std-high-claude"}))
    assert d.to_profile_id == "std-high-codex"


def test_a_target_quarantined_in_the_project_is_never_chosen():
    codex = _pool("std-high-codex", max_active=2)
    view = _view(_saturated_opencode(), _roomy_claude(quarantined={"p"}), codex)
    [d] = _plan([_cand("t1")], view)
    assert d.to_profile_id == "std-high-codex"


def test_a_target_whose_own_unserved_demand_takes_its_headroom_is_not_a_target():
    # One free seat on claude, but claude has its own task waiting in q.
    claude = _pool("std-high-claude", max_active=2, p=_proj(busy=1), q=_proj(ready=1))
    [d] = _plan([_cand("t1")], _view(_saturated_opencode(), claude))
    assert (d.action, d.kind) == ("hold", "spill_no_target")
    codex = _pool("std-high-codex", max_active=2)
    [d] = _plan([_cand("t1")], _view(_saturated_opencode(), claude, codex))
    assert d.to_profile_id == "std-high-codex"


def test_targets_follow_the_configured_provider_order():
    cfg = ProviderFailoverConfig(order=["codex", "claude"])
    codex = _pool("std-high-codex", max_active=2)
    [d] = _plan([_cand("t1")], _view(_saturated_opencode(), _roomy_claude(), codex),
                ctx=_ctx(config=cfg))
    assert d.to_profile_id == "std-high-codex"


def test_the_per_target_headroom_caps_the_moves():
    claude = _pool("std-high-claude", max_active=2, p=_proj(busy=1))
    source = _saturated_opencode(ready=3)
    decisions = _plan([_cand(f"t{i}", created_at=float(i)) for i in range(3)],
                      _view(source, claude))
    assert [d.action for d in decisions] == ["move", "hold", "hold"]
    assert {d.kind for d in decisions[1:]} == {"spill_no_target"}


def test_the_global_headroom_caps_the_moves():
    claude = _pool("std-high-claude", p=_proj(busy=1))
    source = _pool("std-high-opencode", max_active=1, p=_proj(busy=1, ready=3))
    decisions = _plan([_cand(f"t{i}", created_at=float(i)) for i in range(3)],
                      _view(source, claude, headroom=2))
    assert [d.action for d in decisions] == ["move", "move", "hold"]


def test_the_project_room_caps_the_moves():
    claude = _pool("std-high-claude", p=_proj(busy=1))
    decisions = _plan([_cand(f"t{i}", created_at=float(i)) for i in range(2)],
                      _view(_saturated_opencode(ready=2), claude, room={"p": 1}))
    assert [d.action for d in decisions] == ["move", "hold"]


def test_the_per_sweep_cap_holds_the_rest_with_their_target():
    source = _saturated_opencode(ready=7)
    claude = _pool("std-high-claude", p=_proj(busy=1))
    decisions = _plan([_cand(f"t{i}", created_at=float(i)) for i in range(7)],
                      _view(source, claude))
    assert [d.action for d in decisions].count("move") == 5  # spill.max_per_sweep default
    held = [d for d in decisions if d.action == "hold"]
    assert [d.kind for d in held] == ["spill_sweep_limit"] * 2
    assert [d.ahead for d in held] == [0, 1]
    assert {d.to_profile_id for d in held} == {"std-high-claude"}


def test_a_disabled_source_pool_moves_its_whole_queue_up_to_target_headroom():
    # Disabled: the idle worker drains, so all three tasks are planned (not
    # just the two the idle-worker arithmetic would call unserved).
    source = _pool("std-high-opencode", enabled=False, max_active=0, p=_proj(idle=1, ready=3))
    claude = _pool("std-high-claude", max_active=3, p=_proj(busy=1))
    decisions = _plan([_cand(f"t{i}", created_at=float(i)) for i in range(3)],
                      _view(source, claude), ctx=_ctx(disabled={"std-high-opencode"}))
    assert [d.action for d in decisions] == ["move", "move", "hold"]
    assert "disabled" in decisions[0].detail
    assert decisions[2].kind == "spill_no_target"


def test_zero_global_headroom_still_moves_onto_an_idle_surplus_in_the_project():
    source = _pool("std-high-opencode", p=_proj(busy=1, ready=2))
    claude = _pool("std-high-claude", p=_proj(idle=1), q=_proj(idle=3))
    decisions = _plan([_cand("t1", created_at=1.0), _cand("t2", created_at=2.0)],
                      _view(source, claude, headroom=0))
    assert [d.action for d in decisions] == ["move", "hold"]
    assert decisions[0].to_profile_id == "std-high-claude"
    assert decisions[1].kind == "spill_no_target"  # q's idle workers cannot claim p's work


def test_an_idle_surplus_is_net_of_the_targets_own_ready_work():
    source = _pool("std-high-opencode", p=_proj(busy=1, ready=1))
    claude = _pool("std-high-claude", p=_proj(idle=1, ready=1))
    [d] = _plan([_cand("t1")], _view(source, claude, headroom=0))
    assert (d.action, d.kind) == ("hold", "spill_no_target")


# -- the project's preferred provider (S6) ------------------------------------------


def test_a_preferred_provider_restricts_targets():
    codex = _pool("std-high-codex", max_active=2)
    view = _view(_saturated_opencode(), _roomy_claude(), codex)
    [d] = _plan([_cand("t1")], view, preferred={"p": "codex"})
    assert d.to_profile_id == "std-high-codex"
    full_codex = _pool("std-high-codex", max_active=1, p=_proj(busy=1))
    [d] = _plan([_cand("t1")], _view(_saturated_opencode(), _roomy_claude(), full_codex),
                preferred={"p": "codex"})
    assert (d.action, d.kind) == ("hold", "spill_no_target")
    assert "codex" in d.detail
    # Another project's preference does not apply here.
    [d] = _plan([_cand("t1")], view, preferred={"q": "codex"})
    assert d.to_profile_id == "std-high-claude"


def test_a_task_on_the_preferred_provider_never_spills_off_it():
    source = _pool("std-high-claude", max_active=1, p=_proj(busy=1, ready=1))
    codex = _pool("std-high-codex", max_active=2)
    [d] = _plan([_cand("t1", "std-high-claude")], _view(source, codex),
                preferred={"p": "claude"})
    assert (d.action, d.kind) == ("hold", "spill_preferred_provider")
    # Without the preference it spills.
    [d] = _plan([_cand("t1", "std-high-claude")], _view(source, codex))
    assert d.to_profile_id == "std-high-codex"


# -- ordering and shape -------------------------------------------------------------


def test_ordering_is_deterministic_by_priority_created_at_and_task_id():
    claude = _pool("std-high-claude", max_active=3, p=_proj(busy=1))
    cands = [
        _cand("b", priority=50, created_at=5.0),
        _cand("a", priority=50, created_at=5.0),
        _cand("z", priority=10, created_at=9.0),
        _cand("m", priority=100, created_at=1.0),
    ]
    view = _view(_saturated_opencode(ready=4), claude)
    first = _plan(cands, view)
    assert [d.task_id for d in first] == ["z", "a", "b", "m"]
    assert [d.action for d in first] == ["move", "move", "hold", "hold"]
    for seed in range(5):
        shuffled = cands[:]
        random.Random(seed).shuffle(shuffled)
        assert [d.to_dict() for d in _plan(shuffled, view)] == [d.to_dict() for d in first]


def test_every_hold_kind_is_a_declared_one():
    stats = {"t3": {"auto_count": 9, "last_auto_at": None, "left_providers": set()}}
    cands = [
        _cand("t1", intent=PINNED, created_at=1.0),
        _cand("t2", age=10.0, created_at=2.0),
        _cand("t3", created_at=3.0),
        _cand("t4", created_at=4.0),
    ]
    decisions = _plan(cands, _view(_saturated_opencode(ready=4)), ctx=_ctx(stats=stats))
    kinds = {d.kind for d in decisions if d.action == "hold"}
    assert kinds == {"spill_pinned", "spill_waiting", "reroute_limit_reached", "spill_no_target"}
    assert kinds <= set(SPILL_HOLD_KINDS)


def test_the_planner_writes_nothing_into_its_inputs():
    view = _view(_saturated_opencode(ready=2), _roomy_claude())
    before = repr(view)
    _plan([_cand("t1", created_at=1.0), _cand("t2", created_at=2.0)], view)
    assert repr(view) == before


# -- the view adapter -----------------------------------------------------------------


def _profile(pid, harness, default_class, **kw) -> AgentProfile:
    return AgentProfile(
        id=pid, name=pid, harness=harness, default_class=default_class, lifecycle="pool", **kw
    )


def test_capacity_view_from_measurement_folds_supply_candidates_and_bounds():
    opencode, claude = PoolKey("std-high-opencode"), PoolKey("std-high-claude")
    m = PoolMeasurement()
    m.profiles = {
        opencode: _profile("std-high-opencode", "opencode", "std-high", max_active=1),
        claude: _profile("std-high-claude", "claude", "std-high", max_active=4, enabled=False),
    }
    m.supply = {
        opencode: PoolSupply(
            running_busy=1, draining=1,
            by_project={"p": PoolProjectSupply(running_busy=1, draining=1)},
        ),
        claude: PoolSupply(
            running_idle=2, starting=1, idle_session_ids=["s1", "s2"],
            by_project={
                "p": PoolProjectSupply(running_idle=1, idle_session_ids=["s1"]),
                "q": PoolProjectSupply(running_idle=1, starting=1, idle_session_ids=["s2"]),
            },
        ),
    }
    m.demand = {opencode: 3, claude: 0}
    m.bounds = {opencode: (0, 1), claude: (0, 0)}  # disabled -> (0, 0)
    m.candidates = {
        opencode: [
            PlacementCandidate("p", ready=3, live=1, project_live_total=2, project_cap=5,
                               workspace_capacity=4, quarantined=False, warm_floor=0),
            PlacementCandidate("q", ready=0, live=0, project_live_total=2, project_cap=None,
                               workspace_capacity=1, quarantined=True, warm_floor=0),
        ],
        claude: [
            PlacementCandidate("p", ready=0, live=1, project_live_total=2, project_cap=5,
                               workspace_capacity=4, quarantined=False, warm_floor=0,
                               idle_session_ids=("s1",)),
            PlacementCandidate("q", ready=0, live=2, project_live_total=2, project_cap=None,
                               workspace_capacity=1, quarantined=False, warm_floor=0,
                               idle_session_ids=("s2",), starting=1),
        ],
    }
    view = capacity_view_from_measurement(
        m, 10, profile_providers={"std-high-claude": "anthropic-ish"}
    )
    oc = view.pools["std-high-opencode"]
    assert (oc.class_id, oc.provider, oc.enabled, oc.lifecycle) == (
        "std-high", "opencode", True, "pool"
    )
    assert (oc.max_active, oc.live, oc.idle, oc.starting, oc.ready) == (1, 1, 0, 0, 3)
    assert oc.projects["p"] == PoolProjectCapacity(idle=0, busy=1, starting=0, ready=3)
    assert oc.quarantined == frozenset({"q"})
    cl = view.pools["std-high-claude"]
    assert cl.provider == "anthropic-ish" and cl.enabled is False and cl.max_active == 0
    assert (cl.live, cl.idle, cl.starting) == (3, 2, 1)
    assert cl.projects["q"] == PoolProjectCapacity(idle=1, busy=0, starting=1, ready=0)
    # Room: min(workspaces, project cap - live pool sessions); no cap means workspaces.
    assert view.project_room == {"p": 3, "q": 1}
    # Global headroom is the sizer's: cap minus idle + busy + starting everywhere.
    assert view.global_headroom == 10 - 4
    assert capacity_view_from_measurement(m, None).global_headroom is None
    assert capacity_view_from_measurement(m, 2).global_headroom == 0


def test_an_empty_measurement_is_an_empty_view():
    view = capacity_view_from_measurement(PoolMeasurement(), 8)
    assert view.pools == {} and view.project_room == {} and view.global_headroom == 8


# -- config (D22 / D24 S2) ------------------------------------------------------------------


def test_spill_defaults():
    spill = ProviderFailoverConfig().spill
    assert isinstance(spill, ProviderFailoverSpillConfig)
    assert (spill.enabled, spill.after_seconds, spill.max_per_sweep) == (True, 300, 5)
    assert PROVIDER_FAILOVER_SUBSECTIONS["spill"] is ProviderFailoverSpillConfig


def test_spill_loads_and_absent_keys_keep_defaults():
    cfg = load_provider_failover_config({"spill": {"after_seconds": 120}})
    assert (cfg.spill.enabled, cfg.spill.after_seconds, cfg.spill.max_per_sweep) == (
        True, 120, 5
    )
    assert load_provider_failover_config({}).spill == ProviderFailoverSpillConfig()


@pytest.mark.parametrize(
    ("spill", "field"),
    [({"after_seconds": -1}, "spill.after_seconds"), ({"max_per_sweep": 0}, "spill.max_per_sweep")],
)
def test_spill_validation(spill, field):
    errors = load_provider_failover_config({"spill": spill}).validate()
    assert [e.field for e in errors] == [field]


def test_spill_zero_threshold_is_valid():
    assert load_provider_failover_config({"spill": {"after_seconds": 0}}).validate() == []


def test_spill_round_trips_through_load_config(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump({
        "database": {"url": "postgresql+asyncpg://test:test@localhost/test"},
        "discord": {"bot_token": "t", "guild_id": "1"},
        "provider_failover": {
            "spill": {"enabled": False, "after_seconds": 900, "max_per_sweep": 2},
        },
    }))
    spill = load_config(str(path)).provider_failover.spill
    assert (spill.enabled, spill.after_seconds, spill.max_per_sweep) == (False, 900, 2)
