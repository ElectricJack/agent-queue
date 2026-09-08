"""size_pools — desired-state sizing over the global (profile-only) pool key.

Pure function, no I/O.  Everything here is fleet-wide: a profile is one
pool no matter how many projects run it, and the only ceiling above
``max_active`` is the box-wide ``global_cap``.  Which project a start lands
in is ``place_pool_actions``' problem — see ``tests/test_pool_placement.py``.
"""

from __future__ import annotations

from src.scheduler import PoolKey, PoolProjectSupply, PoolSupply, size_pools

K = PoolKey("worker")
K2 = PoolKey("reviewer")


def run(**over):
    kw = dict(
        supply={},
        demand={},
        bounds={},
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


def test_global_cap_counts_running_sessions():
    sup = {K: PoolSupply(running_busy=2), K2: PoolSupply()}
    actions, _ = run(
        supply=sup,
        demand={K: 3, K2: 3},
        bounds={K: (0, 5), K2: (0, 5)},
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


def test_starting_sessions_count_against_the_global_cap():
    supply = {K: PoolSupply(starting=1), K2: PoolSupply(starting=1)}
    actions, _ = run(
        supply=supply,
        demand={K: 3, K2: 3},
        bounds={K: (0, 4), K2: (0, 4)},
        global_cap=3,
        max_starts_per_tick=10,
    )
    assert sum(action.count for action in actions) == 1


def test_global_cap_is_fair_shared_round_robin_across_profiles():
    """A saturated fleet spreads its remaining headroom instead of draining it
    into whichever profile sorts first."""
    actions, _ = run(
        supply={K: PoolSupply(), K2: PoolSupply()},
        demand={K: 5, K2: 5},
        bounds={K: (0, 5), K2: (0, 5)},
        global_cap=2,
        max_starts_per_tick=10,
    )
    starts = {action.key: action.count for action in actions}
    assert starts == {K: 1, K2: 1}


def test_surplus_timer_survives_partial_drain_then_resets_on_demand():
    supply = {K: PoolSupply(running_idle=4, idle_session_ids=list("abcd"))}
    actions, since = run(
        supply=supply,
        demand={K: 0},
        bounds={K: (0, 4)},
        surplus_since={K: 1.0},
        now=200.0,
        max_drains_per_tick=1,
    )
    assert actions[0].count == 1 and since == {K: 1.0}
    _, since = run(
        supply={K: PoolSupply(running_idle=3, idle_session_ids=list("bcd"))},
        demand={K: 3},
        bounds={K: (0, 4)},
        surplus_since=since,
    )
    assert since == {}


def test_scale_down_never_selects_busy_or_starting_session_ids():
    actions, _ = run(
        supply={
            K: PoolSupply(
                running_idle=3, running_busy=1, starting=1, idle_session_ids=["old", "mid", "new"]
            )
        },
        demand={K: 0},
        bounds={K: (0, 8)},
        surplus_since={K: 0.0},
        now=1000.0,
        max_drains_per_tick=5,
    )
    assert actions[0].session_ids == ("old", "mid", "new")


# ---------------------------------------------------------------------------
# Global keying — the behaviour this rewrite exists for.
# ---------------------------------------------------------------------------


def aggregated(*locals_):
    """A ``PoolSupply`` folded from per-project breakdowns, as ``_measure_pools`` builds it."""
    sup = PoolSupply()
    for project_id, local in locals_:
        sup.running_idle += local.running_idle
        sup.running_busy += local.running_busy
        sup.starting += local.starting
        sup.draining += local.draining
        sup.idle_session_ids.extend(local.idle_session_ids)
        sup.by_project[project_id] = local
    return sup


def test_demand_and_supply_aggregate_across_projects_into_one_pool():
    """Three projects with one ready task each is a fleet of three, not three
    fleets of three: the pre-change key minted one pool per project and gave
    every one of them the profile's full bounds."""
    supply = {
        K: aggregated(
            ("a", PoolProjectSupply(running_busy=1)),
            ("b", PoolProjectSupply()),
            ("c", PoolProjectSupply()),
        )
    }
    actions, _ = run(
        supply=supply,
        demand={K: 3},  # one ready task in each of the three projects
        bounds={K: (0, 3)},
        max_starts_per_tick=10,
    )
    # want = busy(1) + ready(3) = 4, clamped to max_active 3, minus the one
    # session already live.
    assert [(a.key, a.kind, a.count) for a in actions] == [(K, "start", 2)]


def test_max_active_is_a_fleet_ceiling_not_a_per_project_one():
    supply = {
        K: aggregated(
            ("a", PoolProjectSupply(running_idle=1)),
            ("b", PoolProjectSupply(running_idle=1)),
        )
    }
    actions, _ = run(supply=supply, demand={K: 9}, bounds={K: (0, 2)}, max_starts_per_tick=10)
    assert actions == []  # already at max_active fleet-wide


def test_min_active_is_a_fleet_floor_funded_once():
    """``min_active: 2`` parks two workers somewhere, not two per project."""
    supply = {K: aggregated(("a", PoolProjectSupply(running_idle=1)), ("b", PoolProjectSupply()))}
    actions, _ = run(supply=supply, demand={K: 0}, bounds={K: (2, 5)}, max_starts_per_tick=10)
    assert [(a.kind, a.count) for a in actions] == [("start", 1)]


def test_effective_floor_raised_by_per_project_reservations():
    """``_measure_pools`` folds ``Σ min_per_project`` into the floor it hands the
    sizer, so a reservation the global ``min_active`` cannot fund raises the
    fleet rather than being silently ignored."""
    actions, _ = run(
        supply={K: PoolSupply()},
        demand={K: 0},
        bounds={K: (3, 8)},  # min_active 1, three eligible projects reserving 1 each
        max_starts_per_tick=10,
    )
    assert [(a.kind, a.count) for a in actions] == [("start", 3)]


def test_effective_floor_is_still_clamped_by_max_active():
    """A floor that exceeds ``max_active`` is a contradictory configuration; the
    ceiling wins (doctor names it, sizing does not invent a new failure mode)."""
    actions, _ = run(
        supply={K: PoolSupply()},
        demand={K: 0},
        bounds={K: (5, 2)},
        max_starts_per_tick=10,
    )
    assert [(a.kind, a.count) for a in actions] == [("start", 2)]


def test_global_max_active_binds_below_the_sum_of_pool_ceilings():
    """``swarm.global_max_active`` is the box-wide bound that did not exist while
    ``global_cap=None`` was hardcoded at the call site."""
    actions, _ = run(
        supply={K: PoolSupply(running_idle=2), K2: PoolSupply(running_idle=2)},
        demand={K: 10, K2: 10},
        bounds={K: (0, 8), K2: (0, 8)},
        global_cap=4,
        max_starts_per_tick=10,
    )
    assert actions == []


def test_drain_session_ids_are_the_fleet_wide_idle_set():
    """The sizer names candidates fleet-wide; placement re-selects which of them
    actually stop, per project."""
    supply = {
        K: aggregated(
            ("a", PoolProjectSupply(running_idle=1, idle_session_ids=["a1"])),
            ("b", PoolProjectSupply(running_idle=1, idle_session_ids=["b1"])),
        )
    }
    actions, _ = run(
        supply=supply,
        demand={K: 0},
        bounds={K: (0, 4)},
        surplus_since={K: 0.0},
        now=500.0,
    )
    assert [(a.kind, a.count, a.session_ids) for a in actions] == [("drain", 2, ("a1", "b1"))]
