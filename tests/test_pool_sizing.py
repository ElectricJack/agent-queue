"""size_pools — spec §11.1 desired-state sizing.  Pure function, no I/O."""

from __future__ import annotations

from src.scheduler import PoolKey, PoolSupply, size_pools

K = PoolKey("proj", "worker")
K2 = PoolKey("proj", "reviewer")
KB = PoolKey("other", "worker")


def run(**over):
    kw = dict(
        supply={},
        demand={},
        bounds={},
        project_caps={},
        global_cap=None,
        surplus_since={},
        now=1000.0,
        scale_down_grace=120,
        max_starts_per_tick=2,
        max_drains_per_tick=5,
    )
    kw.update(over)
    return size_pools(**kw)


def test_scale_up_to_want_bounded_by_max_and_tick():
    actions, _ = run(supply={K: PoolSupply()}, demand={K: 5}, bounds={K: (0, 3)})
    assert [(a.kind, a.count) for a in actions] == [("start", 2)]  # min(3, 5) capped at 2/tick


def test_min_active_keeps_idle_workers():
    actions, _ = run(supply={K: PoolSupply()}, demand={K: 0}, bounds={K: (1, 3)})
    assert [(a.kind, a.count) for a in actions] == [("start", 1)]


def test_never_below_busy_plus_starting():
    actions, _ = run(
        supply={K: PoolSupply(running_busy=2, starting=1)}, demand={K: 0}, bounds={K: (0, 1)}
    )
    assert actions == []


def test_scale_down_waits_for_grace_then_drains_idle_oldest_first():
    sup = {K: PoolSupply(running_idle=3, idle_session_ids=["a", "b", "c"])}
    actions, since = run(supply=sup, demand={K: 0}, bounds={K: (1, 5)})
    assert actions == [] and since == {K: 1000.0}
    actions, since = run(
        supply=sup, demand={K: 0}, bounds={K: (1, 5)}, surplus_since=since, now=1000.0 + 121
    )
    assert [(a.kind, a.count, a.session_ids) for a in actions] == [("drain", 2, ("a", "b"))]


def test_surplus_clears_when_demand_returns():
    actions, since = run(
        supply={K: PoolSupply(running_idle=2, idle_session_ids=["a", "b"])},
        demand={K: 2},
        bounds={K: (0, 5)},
        surplus_since={K: 1.0},
    )
    assert actions == [] and since == {}


def test_draining_sessions_do_not_count_as_surplus_again():
    sup = {K: PoolSupply(running_idle=1, draining=1, idle_session_ids=["a"])}
    actions, _ = run(
        supply=sup, demand={K: 0}, bounds={K: (1, 5)}, surplus_since={K: 0.0}, now=500.0
    )
    assert actions == []  # current excludes draining: idle(1) == desired(1) -> no surplus


def test_draining_sessions_excluded_from_current_so_idle_alone_drains():
    sup = {K: PoolSupply(running_idle=2, draining=1, idle_session_ids=["a", "b"])}
    actions, _ = run(
        supply=sup, demand={K: 0}, bounds={K: (0, 5)}, surplus_since={K: 0.0}, now=500.0
    )
    assert [(a.kind, a.count, a.session_ids) for a in actions] == [("drain", 2, ("a", "b"))]


def test_project_cap_is_fair_shared_across_pools():
    sup = {K: PoolSupply(), K2: PoolSupply()}
    actions, _ = run(
        supply=sup,
        demand={K: 4, K2: 4},
        bounds={K: (0, 4), K2: (0, 4)},
        project_caps={"proj": 2},
        max_starts_per_tick=10,
    )
    starts = {a.key: a.count for a in actions if a.kind == "start"}
    assert starts == {K: 1, K2: 1}


def test_global_cap_counts_running_sessions():
    sup = {K: PoolSupply(running_busy=2), KB: PoolSupply()}
    actions, _ = run(
        supply=sup,
        demand={K: 3, KB: 3},
        bounds={K: (0, 5), KB: (0, 5)},
        global_cap=3,
        max_starts_per_tick=10,
    )
    starts = {a.key: a.count for a in actions if a.kind == "start"}
    assert sum(starts.values()) == 1


def test_drains_bounded_per_tick():
    sup = {K: PoolSupply(running_idle=8, idle_session_ids=list("abcdefgh"))}
    actions, _ = run(
        supply=sup,
        demand={K: 0},
        bounds={K: (0, 8)},
        surplus_since={K: 0.0},
        now=500.0,
        max_drains_per_tick=3,
    )
    assert [(a.kind, a.count) for a in actions] == [("drain", 3)]
