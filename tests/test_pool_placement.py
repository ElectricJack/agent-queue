"""place_pool_actions — which project each sized start or drain lands in.

Pure function, no I/O, no clock: ``size_pools`` says how many workers a
profile should have fleet-wide, and this says where they go.  A worker is
bound to one project for its whole life (workspace and token scope are both
minted at launch), so this is the only moment the choice is made — which is
exactly why it is worth testing on its own, without a database.
"""

from __future__ import annotations

from src.scheduler import (
    PlacementCandidate,
    PoolAction,
    PoolKey,
    place_pool_actions,
)

K = PoolKey("worker")


def cand(project_id: str, **over) -> PlacementCandidate:
    """An unremarkable candidate: eligible, no reservation, room to grow."""
    kw = dict(
        project_id=project_id,
        ready=0,
        live=0,
        project_live_total=0,
        project_cap=None,
        workspace_capacity=4,
        quarantined=False,
        warm_floor=0,
        idle_session_ids=(),
        starting=0,
    )
    kw.update(over)
    return PlacementCandidate(**kw)


def place(count=1, kind="start", candidates=(), session_ids=()):
    action = PoolAction(key=K, kind=kind, count=count, session_ids=tuple(session_ids))
    return place_pool_actions(actions=[action], candidates={K: list(candidates)})


def placed(starts):
    return [(s.project_id, s.count, s.reason) for s in starts]


# ---------------------------------------------------------------------------
# Start ordering
# ---------------------------------------------------------------------------


def test_warm_floor_outranks_a_larger_deficit_elsewhere():
    """A stated per-project reservation is funded before raw demand: ``b`` wants
    five workers, but ``a`` was promised one."""
    starts, _, _ = place(
        candidates=[cand("a", warm_floor=1), cand("b", ready=5)],
    )
    assert placed(starts) == [("a", 1, "warm_floor")]


def test_largest_warm_floor_shortfall_first():
    starts, _, _ = place(
        candidates=[cand("a", warm_floor=1), cand("b", warm_floor=3)],
    )
    assert placed(starts) == [("b", 1, "warm_floor")]


def test_deficit_outranks_spread():
    starts, _, _ = place(candidates=[cand("a", live=2, ready=4), cand("b")])
    assert placed(starts) == [("a", 1, "deficit")]


def test_spread_prefers_the_project_with_fewest_live_sessions():
    """With nothing queued anywhere, a start goes where it concentrates least."""
    starts, _, _ = place(candidates=[cand("a", live=3), cand("b", live=1)])
    assert placed(starts) == [("b", 1, "spread")]


def test_budget_spreads_rather_than_piling_onto_the_first_project():
    """Re-sorting after every placement is the point: two starts into two equally
    hungry projects is one each, not two into whichever sorted first."""
    starts, _, _ = place(count=2, candidates=[cand("a", ready=4), cand("b", ready=4)])
    assert placed(starts) == [("a", 1, "deficit"), ("b", 1, "deficit")]


def test_a_deep_deficit_can_still_take_the_whole_budget():
    """Spreading is a tie-break, not a quota: a nine-task backlog outranks a
    one-task one twice over, even after the first start is counted against it."""
    starts, _, _ = place(count=2, candidates=[cand("a", ready=9), cand("b", ready=1)])
    assert placed(starts) == [("a", 2, "deficit")]


def test_ties_break_on_project_id_and_are_deterministic():
    for _ in range(5):
        starts, _, _ = place(candidates=[cand("zulu"), cand("alpha"), cand("mike")])
        assert placed(starts) == [("alpha", 1, "spread")]


def test_repeat_placements_in_one_project_collapse_into_one_start():
    starts, _, _ = place(count=2, candidates=[cand("solo", ready=9)])
    assert placed(starts) == [("solo", 2, "deficit")]


# ---------------------------------------------------------------------------
# Start eligibility, one predicate at a time
# ---------------------------------------------------------------------------


def test_quarantined_project_is_skipped_before_the_budget_is_spent():
    """Quarantine used to be checked *after* the sizer picked a key, so a broken
    project silently burned its share of the tick's start budget.  Under a global
    key one broken project could burn the whole fleet's."""
    starts, _, starved = place(candidates=[cand("broken", quarantined=True), cand("ok")])
    assert placed(starts) == [("ok", 1, "spread")]
    assert starved == []


def test_project_without_workspace_capacity_is_skipped():
    starts, _, _ = place(candidates=[cand("full", workspace_capacity=0), cand("ok")])
    assert placed(starts) == [("ok", 1, "spread")]


def test_project_at_its_concurrency_cap_is_skipped():
    starts, _, _ = place(
        candidates=[cand("capped", project_live_total=2, project_cap=2), cand("ok")],
    )
    assert placed(starts) == [("ok", 1, "spread")]


def test_project_cap_counts_pool_sessions_of_every_profile():
    """``project_live_total`` is the project's whole pool footprint, not this
    pool's — two workers of another profile fill a cap of two."""
    starts, _, starved = place(
        candidates=[cand("a", live=0, project_live_total=2, project_cap=2)],
    )
    assert starts == []
    assert starved[0].reasons == {"a": "at project cap (2)"}


def test_a_second_start_respects_the_capacity_the_first_consumed():
    """Within one tick a placed start has taken a workspace and a cap slot; the
    next decision has to see that."""
    starts, _, starved = place(
        count=2,
        candidates=[cand("a", ready=9, workspace_capacity=1, project_cap=4)],
    )
    assert placed(starts) == [("a", 1, "deficit")]
    assert starved[0].wanted == 1


def test_idle_worker_with_nothing_queued_skips_the_project():
    """That idle worker will claim the next ready task itself; launching beside it
    just manufactures a drain candidate two minutes later."""
    starts, _, _ = place(
        candidates=[cand("warm", live=1, idle_session_ids=("s1",)), cand("cold")],
    )
    assert placed(starts) == [("cold", 1, "spread")]


def test_idle_worker_does_not_skip_a_project_with_unserved_demand():
    starts, _, _ = place(
        candidates=[cand("busy", live=1, ready=4, idle_session_ids=("s1",)), cand("cold", live=2)],
    )
    assert placed(starts) == [("busy", 1, "deficit")]


def test_a_launch_already_in_flight_counts_as_serving_the_queue():
    """``ready - (idle + starting)``: one ready task with one worker already
    booting is not an unserved deficit."""
    starts, _, _ = place(
        candidates=[
            cand("booting", live=2, ready=1, starting=1, idle_session_ids=("s1",)),
            cand("cold", live=3),
        ],
    )
    assert placed(starts) == [("cold", 1, "spread")]


def test_the_idle_skip_never_blocks_a_warm_floor():
    """A reservation of two is not satisfied by one idle worker."""
    starts, _, _ = place(
        candidates=[cand("a", live=1, warm_floor=2, idle_session_ids=("s1",))],
    )
    assert placed(starts) == [("a", 1, "warm_floor")]


# ---------------------------------------------------------------------------
# Starvation
# ---------------------------------------------------------------------------


def test_starvation_names_the_blocking_reason_per_project():
    starts, _, starved = place(
        count=2,
        candidates=[
            cand("q", quarantined=True),
            cand("nows", workspace_capacity=0),
            cand("capped", project_live_total=1, project_cap=1),
        ],
    )
    assert starts == []
    assert len(starved) == 1
    assert starved[0].key == K
    assert starved[0].wanted == 2
    assert starved[0].reasons == {
        "q": "quarantined",
        "nows": "no workspace capacity",
        "capped": "at project cap (1)",
    }


def test_starvation_when_the_profile_has_no_candidate_project_at_all():
    starts, drains, starved = place(count=1, candidates=[])
    assert (starts, drains) == ([], [])
    assert starved[0].wanted == 1 and starved[0].reasons


def test_partial_placement_reports_only_the_unplaced_remainder():
    starts, _, starved = place(
        count=3,
        candidates=[cand("a", ready=9, project_cap=1), cand("b", quarantined=True)],
    )
    assert placed(starts) == [("a", 1, "deficit")]
    assert starved[0].wanted == 2


def test_no_starvation_when_every_start_is_placed():
    starts, _, starved = place(count=2, candidates=[cand("a", ready=9)])
    assert placed(starts) == [("a", 2, "deficit")]
    assert starved == []


# ---------------------------------------------------------------------------
# Drains
# ---------------------------------------------------------------------------


def test_drain_never_crosses_a_warm_floor():
    """The quiet project's one warm worker is the oldest and idlest thing in the
    fleet, and is exactly what ``min_per_project`` bought."""
    _, drains, _ = place(
        kind="drain",
        candidates=[
            cand("quiet", live=1, warm_floor=1, idle_session_ids=("old",)),
            cand("busy", live=2, idle_session_ids=("newer",)),
        ],
    )
    assert [(d.project_id, d.session_ids) for d in drains] == [("busy", ("newer",))]


def test_drain_takes_the_largest_idle_surplus_relative_to_local_demand():
    """Two idle workers with two ready tasks behind them is no surplus at all;
    one idle worker with nothing queued is."""
    _, drains, _ = place(
        kind="drain",
        candidates=[
            cand("queued", live=2, ready=2, idle_session_ids=("q1", "q2")),
            cand("empty", live=1, idle_session_ids=("e1",)),
        ],
    )
    assert [(d.project_id, d.session_ids) for d in drains] == [("empty", ("e1",))]


def test_drain_takes_the_oldest_idle_session_within_a_project():
    _, drains, _ = place(
        kind="drain",
        candidates=[cand("a", live=3, idle_session_ids=("old", "mid", "new"))],
    )
    assert [(d.project_id, d.session_ids) for d in drains] == [("a", ("old",))]


def test_multiple_drains_rebalance_between_projects():
    """Surplus is recomputed after every pick, so the deepest idle pile is
    shaved down to the next one rather than emptied outright."""
    _, drains, _ = place(
        count=3,
        kind="drain",
        candidates=[
            cand("a", live=3, idle_session_ids=("a1", "a2", "a3")),
            cand("b", live=2, idle_session_ids=("b1", "b2")),
        ],
    )
    assert [(d.project_id, d.session_ids) for d in drains] == [
        ("a", ("a1", "a2")),
        ("b", ("b1",)),
    ]


def test_drain_stops_when_nothing_is_left_to_take():
    _, drains, _ = place(
        count=4,
        kind="drain",
        candidates=[cand("a", live=1, idle_session_ids=("a1",))],
    )
    assert [(d.project_id, d.session_ids) for d in drains] == [("a", ("a1",))]


def test_drain_ignores_projects_with_no_idle_session():
    _, drains, _ = place(
        kind="drain",
        candidates=[cand("busy", live=2, ready=5), cand("idle", live=1, idle_session_ids=("i1",))],
    )
    assert [(d.project_id, d.session_ids) for d in drains] == [("idle", ("i1",))]


def test_drain_ties_break_on_project_id():
    for _ in range(5):
        _, drains, _ = place(
            kind="drain",
            candidates=[
                cand("zulu", live=1, idle_session_ids=("z1",)),
                cand("alpha", live=1, idle_session_ids=("a1",)),
            ],
        )
        assert [d.project_id for d in drains] == ["alpha"]


def test_starts_and_drains_for_several_pools_stay_separated():
    other = PoolKey("reviewer")
    starts, drains, _ = place_pool_actions(
        actions=[
            PoolAction(key=K, kind="start", count=1),
            PoolAction(key=other, kind="drain", count=1),
        ],
        candidates={
            K: [cand("a", ready=3)],
            other: [cand("a", live=1, idle_session_ids=("r1",))],
        },
    )
    assert [(s.key, s.project_id) for s in starts] == [(K, "a")]
    assert [(d.key, d.session_ids) for d in drains] == [(other, ("r1",))]
