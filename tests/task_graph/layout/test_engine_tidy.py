from dataclasses import replace

import pytest

from src.task_graph.layout import engine as engine_module
from src.task_graph.layout.constants import CARD_H, CARD_W
from src.task_graph.layout.cost import count_crossings
from src.task_graph.layout.engine import _ordered_from, layout_container
from src.task_graph.layout.model import ContainerScope, SnapTask


def task(i, created=0.0, status="READY", *, container=False):
    return SnapTask(id=i, parent_id=None, is_container=container, status=status, created_at=created)


def scope(children, edges, aggregates=None, *, root=False):
    kids = {t.id: t for t in children}
    return ContainerScope(
        container_id=None if root else "epic", container_path="/" if root else "/epic/",
        depth=0 if root else 1, children=kids, existing={},
        sibling_edges=edges, child_sizes={t.id: (CARD_W, CARD_H) for t in children},
        child_aggregates=aggregates or {},
    )


def reading_order(res):
    return [c for c, _ in sorted(res.rows.items(), key=lambda kv: (kv[1].rel_y, kv[1].rel_x))]


def crossings_of(res, edges):
    ordinals = {c: r.ordinal for c, r in res.rows.items()}
    ordered = _ordered_from(ordinals)
    positions = {c: (r.rel_x, r.rel_y) for c, r in res.rows.items()}
    return count_crossings(ordered, positions, edges)


def test_tidy_untangles_a_reversed_ladder():
    # 4 blockers a0..a3, 4 dependents b0..b3 created in reverse order so the
    # created_at seed is maximally crossed; tidy must reach zero crossings.
    kids = [task(f"a{i}", created=i) for i in range(4)] + [task(f"b{i}", created=10 - i) for i in range(4)]
    edges = [(f"b{i}", f"a{i}") for i in range(4)]
    res = layout_container(scope(kids, edges, root=True), mode="tidy")
    assert crossings_of(res, edges) == 0


def test_tidy_pinned_bound_on_fixture():
    # Two interleaved chains plus cross links: bound pinned at 2 crossings.
    kids = [task(f"n{i}", created=(i * 7) % 12) for i in range(12)]
    edges = [(f"n{i}", f"n{i-2}") for i in range(2, 12)] + [("n5", "n0"), ("n11", "n4")]
    res = layout_container(scope(kids, edges, root=True), mode="tidy")
    assert crossings_of(res, edges) <= 2


def test_tidy_is_deterministic():
    kids = [task(f"n{i}", created=i) for i in range(20)]
    edges = [(f"n{i}", f"n{(i * 3) % 20}") for i in range(1, 20) if (i * 3) % 20 < i]
    a = layout_container(scope(kids, edges), mode="tidy", seed=1)
    b = layout_container(scope(kids, edges), mode="tidy", seed=1)
    assert {c: r.ordinal for c, r in a.rows.items()} == {c: r.ordinal for c, r in b.rows.items()}


def test_tidy_puts_finished_work_first_and_running_next():
    # created_at is interleaved so creation order alone would scatter the
    # three classes; the seed key must group them.
    kids = [
        task("done0", created=1.0, status="COMPLETED"),
        task("ready0", created=2.0),
        task("done1", created=3.0, status="COMPLETED"),
        task("ready1", created=4.0),
        task("run0", created=5.0, status="IN_PROGRESS"),
        task("ready2", created=6.0),
    ]
    res = layout_container(scope(kids, [], root=True), mode="tidy")
    assert reading_order(res) == ["done0", "done1", "run0", "ready0", "ready1", "ready2"]


def test_tidy_container_aggregate_order_is_deterministic_across_input_permutations():
    """Aggregate-map order must not leak into ranks, keys, or coordinates.

    The old test only shuffled fields for one container and compared one seed
    key.  This drives the full engine with several containers whose rollups
    produce distinct activity classes, varies both outer and inner dictionary
    insertion order, and preserves a deterministic same-class tie.
    """
    children = {
        "running": task("running", created=9.0, status="COMPLETED", container=True),
        "active-a": task("active-a", created=3.0, container=True),
        "active-b": task("active-b", created=3.0, container=True),
        "finished": task("finished", created=1.0, status="COMPLETED", container=True),
    }
    aggregate_values = {
        "running": {"descendants": 4, "running": 1, "active": 3},
        "active-a": {"descendants": 3, "running": 0, "active": 2},
        "active-b": {"descendants": 2, "running": 0, "active": 1},
        "finished": {"descendants": 2, "running": 0, "active": 0},
    }

    def run(child_order, field_order):
        aggregates = {
            child_id: {field: aggregate_values[child_id][field] for field in field_order}
            for child_id in child_order
        }
        result = layout_container(
            scope([children[child_id] for child_id in child_order], [], aggregates, root=True),
            mode="tidy", seed=17
        )
        return {
            child_id: (
                row.rank,
                row.order_key,
                row.rel_x,
                row.rel_y,
                row.abs_x,
                row.abs_y,
            )
            for child_id, row in result.rows.items()
        }

    snapshots = {
        tuple(sorted(run(order, fields).items()))
        for order, fields in (
            (("running", "active-a", "active-b", "finished"), ("descendants", "running", "active")),
            (("finished", "active-b", "running", "active-a"), ("active", "descendants", "running")),
            (("active-a", "finished", "active-b", "running"), ("running", "active", "descendants")),
        )
    }
    assert len(snapshots) == 1
    baseline = dict(next(iter(snapshots)))
    assert [child_id for child_id, _ in sorted(baseline.items(), key=lambda item: (item[1][3], item[1][2]))] == [
        "finished",
        "running",
        "active-a",
        "active-b",
    ]

    # This control proves aggregates participate in the sort: making the
    # formerly-running container to-do moves both active containers ahead,
    # while the finished band still leads under the binding OD5 Q1 policy.
    changed = dict(aggregate_values)
    changed["running"] = {"descendants": 4, "running": 0, "active": 3}
    result = layout_container(
        scope(list(children.values()), [], changed, root=True), mode="tidy", seed=17
    )
    assert reading_order(result) == ["finished", "active-a", "active-b", "running"]


def test_tidy_output_is_unchanged_when_every_sibling_is_one_class(monkeypatch):
    """Every task here is READY, so the new key reduces to ``(created_at, id)``
    and the published ordinals must be byte-identical to the pre-change seed."""
    kids = [task(f"n{i}", created=(i * 7) % 12) for i in range(12)]
    edges = [(f"n{i}", f"n{i-2}") for i in range(2, 12)] + [("n5", "n0"), ("n11", "n4")]
    after = layout_container(scope(kids, edges, root=True), mode="tidy", seed=3)

    monkeypatch.setattr(
        engine_module, "tidy_seed_key", lambda t, agg=None: (t.created_at, t.id)
    )
    before = layout_container(scope(kids, edges, root=True), mode="tidy", seed=3)

    assert {c: r.ordinal for c, r in after.rows.items()} == {
        c: r.ordinal for c, r in before.rows.items()
    }

@pytest.mark.parametrize("mode", ["tidy", "incremental", "resize"])
def test_epic_execution_rows_include_completed_work_and_stable_parallel_ties(mode):
    # Created out of execution order; completed work must not be sorted ahead
    # of its blocker. Parallel siblings tie on age and use id consistently.
    kids = [
        task("last", created=0, status="COMPLETED"),
        task("parallel-b", created=1, status="COMPLETED"),
        task("parallel-a", created=1, status="IN_PROGRESS"),
        task("first", created=9),
    ]
    edges = [("last", "parallel-b"), ("parallel-b", "first"), ("parallel-a", "first")]
    s = scope(kids, edges)
    old = layout_container(scope(kids, []), mode="tidy")
    # Simulate ordinals saved by the old optimizer, including rank slack.
    s.existing = {cid: replace(row, rank=7) for cid, row in old.rows.items()}
    result = layout_container(s, mode=mode)
    assert reading_order(result) == ["first", "parallel-a", "parallel-b", "last"]
    assert [result.rows[cid].rank for cid in reading_order(result)] == [0, 1, 1, 2]
    assert result.rows["parallel-a"].rel_y == result.rows["parallel-b"].rel_y

    s.children = {t.id: replace(t, status="COMPLETED") for t in reversed(kids)}
    s.sibling_edges.reverse()
    s.existing = result.rows
    refreshed = layout_container(s, mode=mode, seed=99)
    assert reading_order(refreshed) == reading_order(result)
    assert refreshed.changed_ordinals == set()
    assert {cid: (row.rel_x, row.rel_y) for cid, row in refreshed.rows.items()} == {
        cid: (row.rel_x, row.rel_y) for cid, row in result.rows.items()
    }


@pytest.mark.parametrize("count", [4, 5, 20, 60])
def test_epic_wraps_each_dependency_depth_at_four_cards(count):
    kids = [task(f"a{i:02}", created=i) for i in range(count)]
    kids += [task(f"b{i:02}", created=i, status="COMPLETED") for i in range(6)]
    edges = [(f"b{i:02}", "a00") for i in range(6)]
    result = layout_container(scope(kids, edges), mode="tidy")
    ordered = reading_order(result)
    assert ordered == [t.id for t in kids]
    lines = {}
    for row in result.rows.values():
        lines.setdefault(row.rel_y, []).append(row)
    assert max(map(len, lines.values())) == 4
    assert len(lines) == (count + 3) // 4 + 2
    assert all(len({row.rank for row in line}) == 1 for line in lines.values())


def test_epic_serial_chain_keeps_execution_depth_in_reading_order():
    kids = [task(f"n{i}", created=8 - i, status="COMPLETED") for i in range(8)]
    edges = [(kids[i].id, kids[i - 1].id) for i in range(1, len(kids))]
    result = layout_container(scope(kids, edges), mode="tidy")
    assert reading_order(result) == [t.id for t in kids]
    assert len({row.rel_y for row in result.rows.values()}) == len(kids)
