"""size_pools — desired-state sizing over the global (profile-only) pool key.

Pure function, no I/O.  Everything here is fleet-wide: a profile is one
pool no matter how many projects run it, and the only ceiling above
``max_active`` is the box-wide ``global_cap``.  Which project a start lands
in is ``place_pool_actions``' problem — see ``tests/test_pool_placement.py``.
"""

from __future__ import annotations

import pytest
from hypothesis import given, strategies as st

from src.scheduler import PoolKey, PoolProjectSupply, PoolSupply, size_pools

K = PoolKey("worker")
K2 = PoolKey("reviewer")


@st.composite
def pool_histories(draw):
    count = draw(st.integers(1, 4))
    ceilings = draw(st.lists(st.integers(0, 6), min_size=count, max_size=count))
    floors = [draw(st.integers(0, ceiling)) for ceiling in ceilings]
    ticks = draw(st.lists(
        st.tuples(
            st.lists(st.integers(0, 10), min_size=count, max_size=count),
            st.integers(0, 6),  # completions per pool before sizing
            st.integers(0, 6),  # claims per pool after sizing
        ), min_size=1, max_size=15,
    ))
    return ceilings, floors, ticks


@given(history=pool_histories(), global_cap=st.integers(0, 15), budget=st.integers(1, 5))
def test_property_pool_histories_preserve_fleet_and_profile_caps(history, global_cap, budget):
    """Apply starts, claims, completions and drains across successive measurements."""
    ceilings, floors, ticks = history
    keys = [PoolKey(f"pool-{i}") for i in range(len(ceilings))]
    bounds = {key: (lo, hi) for key, lo, hi in zip(keys, floors, ceilings)}
    supply = {key: PoolSupply() for key in keys}
    surplus = {}
    next_session = 0
    for tick, (ready, completions, claims) in enumerate(ticks):
        booting = {key: sup.starting for key, sup in supply.items()}
        for key in keys:
            sup = supply[key]
            completed = min(completions, sup.running_busy)
            sup.running_busy -= completed
            for _ in range(completed):
                sup.idle_session_ids.append(f"session-{next_session}")
                next_session += 1
            sup.running_idle += completed
        actions, surplus = size_pools(
            supply=supply, demand=dict(zip(keys, ready)), bounds=bounds,
            global_cap=global_cap, surplus_since=surplus, now=float(tick * 10),
            scale_down_grace=10, max_starts_per_tick=budget, max_drains_per_tick=budget,
        )
        assert sum(a.count for a in actions if a.kind == "start") <= budget
        assert sum(a.count for a in actions if a.kind == "drain") <= budget
        for action in actions:
            sup = supply[action.key]
            if action.kind == "start":
                # A launch remains in flight until the following measurement.
                sup.starting += action.count
            else:
                assert action.count == len(action.session_ids)
                assert list(action.session_ids) == sup.idle_session_ids[:action.count]
                assert tick * 10 - surplus[action.key] >= 10
                del sup.idle_session_ids[:action.count]
                sup.running_idle -= action.count
        for key, ceiling in zip(keys, ceilings):
            sup = supply[key]
            assert 0 <= sup.running_idle + sup.running_busy + sup.starting <= ceiling
        assert sum(s.running_idle + s.running_busy + s.starting for s in supply.values()) <= global_cap
        for key, queued in zip(keys, ready):
            sup = supply[key]
            for _ in range(booting[key]):
                sup.idle_session_ids.append(f"session-{next_session}")
                next_session += 1
            sup.running_idle += booting[key]
            sup.starting -= booting[key]
            claimed = min(claims, queued, sup.running_idle)
            del sup.idle_session_ids[:claimed]
            sup.running_idle -= claimed
            sup.running_busy += claimed


@given(ready=st.integers(1, 10), cap=st.integers(1, 6), budget=st.integers(1, 4))
def test_property_ready_demand_gets_capacity_without_raising_a_ceiling(ready, cap, budget):
    supply = PoolSupply()
    for tick in range(ready):
        # Finish the previous tick's claims, freeing these same workers.
        supply.running_idle += supply.running_busy
        supply.running_busy = 0
        supply.idle_session_ids = [f"idle-{i}" for i in range(supply.running_idle)]
        actions, _ = run(
            supply={K: supply}, demand={K: ready}, bounds={K: (0, cap)},
            global_cap=cap, max_starts_per_tick=budget, now=float(tick),
        )
        supply.running_idle += sum(a.count for a in actions if a.kind == "start")
        claimed = min(ready, supply.running_idle)
        assert claimed > 0
        supply.running_idle -= claimed
        supply.running_busy += claimed
        ready -= claimed
        assert supply.running_idle + supply.running_busy <= cap
        if ready == 0:
            break
    assert ready == 0


@pytest.mark.asyncio
async def test_unrouted_work_is_demand_for_no_pool(tmp_path):
    """Mandatory routing §9.1: an unrouted task is demand for no pool, whatever
    the project default or preference; a legacy route is demand only while the
    project's router is not ready."""
    from copy import deepcopy
    from src.models import AgentProfile, Task, TaskStatus
    from tests.session_dispatch_helpers import create_session_project, make_session_orch

    orch = await make_session_orch(tmp_path)
    try:
        await create_session_project(orch)
        for profile in await orch.db.list_profiles():
            if profile.default_class == "standard-medium":
                await orch.db.update_profile(profile.id, enabled=False)
        for provider in ("claude", "codex"):
            await orch.db.create_profile(AgentProfile(
                id=f"preferred-test-{provider}", name=provider, harness=provider,
                lifecycle="pool", default_class="standard-medium", max_active=2,
            ))
        # The fixture class is Claude-only unless this test adds the Codex slice.
        cls = deepcopy(orch.session_spec_builder._intelligence_classes["standard-medium"])
        cls.mapping["codex"] = {
            "model": "gpt-6",
        }
        orch.session_spec_builder._intelligence_classes["standard-medium"] = cls
        await orch.db.update_project("p-1", preferred_provider="codex")
        await orch.db.create_task(Task(
            id="unrouted", project_id="p-1", title="Unrouted", description="",
            status=TaskStatus.READY,
        ))
        await orch.db.create_task(Task(
            id="legacy", project_id="p-1", title="Legacy", description="",
            status=TaskStatus.READY, profile_id="preferred-test-codex", route_source="legacy",
            intelligence_class="standard-medium",
        ))
        measurement = await orch._measure_pools()
        assert measurement.demand[PoolKey("preferred-test-codex")] == 1
        assert measurement.demand[PoolKey("preferred-test-claude")] == 0
        orch.router_readiness._ready = frozenset({"p-1"})
        measurement = await orch._measure_pools()
        assert measurement.demand[PoolKey("preferred-test-codex")] == 0
        assert measurement.demand[PoolKey("preferred-test-claude")] == 0
    finally:
        await orch.provider_availability.close()
        await orch.db.close()


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
