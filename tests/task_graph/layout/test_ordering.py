"""The tidy seed ordering (reorganisation design §3.2)."""

import random

from src.task_graph.layout.constants import FINISHED_STATUSES, RUNNING_STATUSES
from src.task_graph.layout.model import SnapTask
from src.task_graph.layout.ordering import NO_PHASE, activity_class, tidy_seed_key


def task(tid, *, status="READY", created=0.0, container=False, phase_order=None):
    return SnapTask(
        id=tid,
        parent_id=None,
        is_container=container,
        status=status,
        created_at=created,
        phase_order=phase_order,
    )


def test_activity_class_orders_finished_before_running_before_open():
    for status in sorted(FINISHED_STATUSES):
        assert activity_class(task("t", status=status)) == 0, status
    for status in sorted(RUNNING_STATUSES):
        assert activity_class(task("t", status=status)) == 1, status
    for status in ("READY", "BLOCKED", "DEFINED", "PAUSED", "WAITING_INPUT", "FAILED"):
        assert activity_class(task("t", status=status)) == 2, status


def test_a_container_is_classed_by_its_subtree_not_its_own_status():
    done = {"descendants": 4, "running": 0, "active": 0}
    assert activity_class(task("e", status="COMPLETED", container=True), done) == 0
    assert activity_class(task("e", status="DEFINED", container=True), done) == 0
    running = {"descendants": 4, "running": 1, "active": 2}
    assert activity_class(task("e", status="DEFINED", container=True), running) == 1
    assert activity_class(task("e", status="COMPLETED", container=True), running) == 1
    active = {"descendants": 4, "running": 0, "active": 3}
    assert activity_class(task("e", status="COMPLETED", container=True), active) == 2


def test_a_childless_container_never_classes_as_running_from_its_own_status():
    """Containers are forced straight to IN_PROGRESS on release
    (``_release_ready_containers``), so IN_PROGRESS on a container says
    nothing about work running — and an empty phase, an empty standing
    parent or an epic emptied by reparenting is exactly that state. Only a
    LEAF's ASSIGNED/IN_PROGRESS, or a container's ``agg["running"] > 0``,
    earns class 1 (the running band)."""
    for status in sorted(RUNNING_STATUSES):
        assert activity_class(task("e", status=status, container=True)) == 2, status
        empty = {"descendants": 0, "running": 0, "active": 0}
        assert activity_class(task("e", status=status, container=True), empty) == 2, status
        # The same status on a LEAF is the running band.
        assert activity_class(task("c", status=status)) == 1, status
    # A childless container that is itself finished rises to the top.
    assert activity_class(task("e", status="COMPLETED", container=True)) == 0


def test_a_leaf_is_never_classed_by_the_aggregate_row_handed_to_it():
    """The driver hands the engine an aggregate for EVERY child, and a leaf's
    is all zeros. Classing a leaf by that row would make every READY card
    look finished."""
    leaf_agg = {"descendants": 0, "running": 0, "active": 0}
    assert activity_class(task("c", status="READY"), leaf_agg) == 2
    assert activity_class(task("c", status="IN_PROGRESS"), leaf_agg) == 1
    # A childless container has nothing to roll up either.
    assert activity_class(task("e", status="DEFINED", container=True), leaf_agg) == 2


def test_phase_order_outranks_activity():
    finished_phase_one = task("p1", status="COMPLETED", created=1.0, phase_order=1)
    running_phase_two = task("p2", status="IN_PROGRESS", created=2.0, phase_order=2)
    assert tidy_seed_key(finished_phase_one) < tidy_seed_key(running_phase_two)


def test_a_non_phase_sibling_never_precedes_a_phase():
    phase = task("p", status="COMPLETED", created=99.0, phase_order=7)
    loose = task("a", status="IN_PROGRESS", created=0.0)
    assert tidy_seed_key(phase) < tidy_seed_key(loose)
    assert tidy_seed_key(loose)[0] == NO_PHASE


def test_equal_class_falls_back_to_created_at_then_id():
    kids = [task(f"n{i}", created=(i * 7) % 5) for i in range(5)]
    by_key = sorted(kids, key=tidy_seed_key)
    by_today = sorted(kids, key=lambda t: (t.created_at, t.id))
    assert [t.id for t in by_key] == [t.id for t in by_today]
    for t in kids:
        assert tidy_seed_key(t) == (NO_PHASE, 2, t.created_at, t.id)


def test_a_mixed_scope_orders_finished_then_running_then_to_do():
    """The operator's model within one rank (decisions 2026-09-22, OD5 Q1):
    finished at the top, the running band next, to-do at the bottom; a
    finished-but-context stub sits with the finished, and each class keeps
    its created_at order."""
    kids = [
        task("done0", created=1.0, status="COMPLETED"),
        task("ready0", created=2.0),
        # a finished-but-context stub: the driver keeps an UNFINISHED
        # container whose descendants have all finished (``driver.py``,
        # ``_visible``) — its aggregate is all zeros with descendants.
        task("stub", created=3.0, status="IN_PROGRESS", container=True),
        task("run0", created=4.0, status="IN_PROGRESS"),
        task("ready1", created=5.0, status="READY"),
        task("run1", created=6.0, status="ASSIGNED"),
        task("done1", created=7.0, status="CANCELED"),
        task("ready2", created=8.0, status="BLOCKED"),
    ]
    aggs = {
        "stub": {"descendants": 3, "running": 0, "active": 0},
        "ready0": {"descendants": 0, "running": 0, "active": 0},
        "ready1": {"descendants": 0, "running": 0, "active": 0},
        "ready2": {"descendants": 0, "running": 0, "active": 0},
    }
    by_key = sorted(kids, key=lambda t: tidy_seed_key(t, aggs.get(t.id)))
    assert [t.id for t in by_key] == [
        "done0", "stub", "done1", "run0", "run1", "ready0", "ready1", "ready2",
    ]


def test_key_is_deterministic_under_shuffled_aggregate_dicts():
    t = task("e", status="COMPLETED", created=3.0, container=True)
    pairs = [("descendants", 4), ("running", 1), ("active", 2), ("completed", 2)]
    rng = random.Random(7)
    keys = set()
    for _ in range(10):
        shuffled = pairs[:]
        rng.shuffle(shuffled)
        keys.add(tidy_seed_key(t, dict(shuffled)))
    assert keys == {(NO_PHASE, 1, 3.0, "e")}
