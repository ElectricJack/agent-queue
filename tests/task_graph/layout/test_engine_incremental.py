import time

from src.task_graph.layout import engine as engine_module
from src.task_graph.layout.constants import (
    CARD_H,
    CARD_W,
    LINE_GAP,
    SIBLING_GAP,
    TARGET_ROW_WIDTH_ROOT,
)
from src.task_graph.layout.engine import layout_container
from src.task_graph.layout.model import ContainerScope, SnapTask


def task(i, created=0.0, container=False, status="READY", phase_order=None):
    return SnapTask(
        id=i,
        parent_id=None,
        is_container=container,
        status=status,
        created_at=created,
        phase_order=phase_order,
    )


def scope(children, edges=(), existing=None, sizes=None, origin=(0.0, 0.0)):
    kids = {t.id: t for t in children}
    return ContainerScope(
        container_id=None, container_path="/", depth=0, children=kids,
        existing=existing or {}, sibling_edges=list(edges),
        child_sizes=sizes or {t.id: (CARD_W, CARD_H) for t in children}, origin=origin,
    )


def test_fresh_layout_places_dependents_below_blockers():
    s = scope([task("a"), task("b")], edges=[("b", "a")])
    res = layout_container(s, mode="incremental")
    assert res.rows["a"].rank == 0 and res.rows["b"].rank == 1
    assert res.rows["b"].rel_y > res.rows["a"].rel_y
    assert res.rows["a"].path == "/a/" and res.rows["a"].depth == 0


def test_long_serial_dependencies_fold_into_serpentine_rows_without_changing_ordinals():
    ids = [chr(ord("a") + i) for i in range(8)]
    res = layout_container(
        scope([task(task_id, created=index) for index, task_id in enumerate(ids)], edges=list(zip(ids[1:], ids))),
        mode="incremental",
    )
    assert [res.rows[task_id].rank for task_id in ids] == sorted(
        res.rows[task_id].rank for task_id in ids
    )
    # The root has the wider target, so six cards fill its first line.
    assert res.rows["f"].rel_x == 5 * (CARD_W + SIBLING_GAP)
    assert res.rows["g"].rel_x == TARGET_ROW_WIDTH_ROOT - CARD_W
    assert res.rows["g"].rel_y == CARD_H + LINE_GAP
    assert res.rows["h"].rel_x < res.rows["g"].rel_x


def test_insert_into_large_container_moves_no_existing_ordinal():
    ids = [f"t{i}" for i in range(1000)]
    first = layout_container(scope([task(i, created=k) for k, i in enumerate(ids)]), mode="incremental")
    before = {cid: r.ordinal for cid, r in first.rows.items()}
    kids = [task(i, created=k) for k, i in enumerate(ids)] + [task("new", created=9999)]
    second = layout_container(scope(kids, existing=first.rows), mode="incremental")
    assert second.changed_ordinals == {"new"}
    for cid, ordinal in before.items():
        assert second.rows[cid].ordinal == ordinal


def test_new_node_with_blockers_lands_under_barycenter():
    kids = [task("a"), task("b"), task("c")]
    first = layout_container(scope(kids), mode="incremental")
    kids2 = kids + [task("n", created=5)]
    s = scope(kids2, edges=[("n", "a"), ("n", "c")], existing=first.rows)
    res = layout_container(s, mode="incremental")
    assert res.rows["n"].rank == 1
    xa, xc = res.rows["a"].rel_x, res.rows["c"].rel_x
    assert xa <= res.rows["n"].rel_x <= xc + CARD_W
    # Strengthen: an existing rank-1 node "m" keyed to sit at the far right
    # (rooted under c only) must not attract n — n, blocked by a AND c,
    # lands under their median, to the left of m.
    with_m = kids + [task("m", created=1)]
    second = layout_container(
        scope(with_m, edges=[("m", "c")], existing=first.rows), mode="incremental"
    )
    with_n = with_m + [task("n", created=5)]
    res2 = layout_container(
        scope(with_n, edges=[("m", "c"), ("n", "a"), ("n", "c")], existing=second.rows),
        mode="incremental",
    )
    assert res2.rows["n"].rank == 1
    assert res2.rows["n"].rel_x < res2.rows["m"].rel_x


def test_new_edge_forces_rank_repair_of_dependent_chain_only():
    kids = [task("a"), task("b"), task("c"), task("d")]
    first = layout_container(scope(kids, edges=[("c", "b")]), mode="incremental")
    assert first.rows["c"].rank == 1
    # New edge: b depends on a → b and c must move down; a and d must not change.
    res = layout_container(
        scope(kids, edges=[("c", "b"), ("b", "a")], existing=first.rows), mode="incremental"
    )
    assert res.rows["a"].ordinal == first.rows["a"].ordinal
    assert res.rows["d"].ordinal == first.rows["d"].ordinal
    assert res.rows["b"].rank == 1 and res.rows["c"].rank == 2
    assert res.changed_ordinals == {"b", "c"}


def test_incremental_repairs_a_saved_rank_zero_phase_to_its_rank_floor():
    first = layout_container(scope([task("late")]), mode="incremental")
    assert first.rows["late"].rank == 0

    # A phase can be created after predecessors are completed, leaving no
    # sibling gate edge.  Incremental layout must still repair an old
    # rank-zero row immediately rather than waiting for a Tidy.
    repaired = layout_container(
        scope([task("late", phase_order=2)], existing=first.rows), mode="incremental"
    )
    assert repaired.rows["late"].rank == 2
    assert repaired.changed_ordinals == {"late"}


def test_removed_node_closes_gap_without_changing_keys():
    kids = [task("a"), task("b"), task("c")]
    first = layout_container(scope(kids), mode="incremental")
    res = layout_container(scope([task("a"), task("c")], existing=first.rows), mode="incremental")
    assert res.rows["c"].order_key == first.rows["c"].order_key
    assert res.rows["c"].rel_x == first.rows["b"].rel_x
    assert "b" not in res.rows


def test_resize_mode_keeps_ordinals_and_recomputes_coordinates():
    kids = [task("a", container=True), task("b")]
    first = layout_container(scope(kids), mode="incremental")
    grown = {"a": (3.0, 3.0), "b": (CARD_W, CARD_H)}
    res = layout_container(scope(kids, existing=first.rows, sizes=grown), mode="resize")
    assert res.changed_ordinals == set()
    assert res.rows["b"].rel_x == first.rows["b"].rel_x + 2.0
    assert res.rows["a"].w == 3.0 and res.rows["a"].kind == "container"


def test_abs_coordinates_include_origin():
    res = layout_container(scope([task("a")], origin=(10.0, 20.0)), mode="incremental")
    assert (res.rows["a"].abs_x, res.rows["a"].abs_y) == (10.0, 20.0)


def test_stub_children_are_card_sized_stubs():
    s = scope([task("epic", container=True)])
    s.stub_ids = frozenset({"epic"})
    res = layout_container(s, mode="incremental")
    assert res.rows["epic"].kind == "stub"
    assert (res.rows["epic"].w, res.rows["epic"].h) == (CARD_W, CARD_H)


def test_deterministic():
    kids = [task(f"t{i}", created=i) for i in range(30)]
    edges = [(f"t{i}", f"t{i-3}") for i in range(3, 30)]
    a = layout_container(scope(kids, edges=edges), mode="incremental", seed=7)
    b = layout_container(scope(kids, edges=edges), mode="incremental", seed=7)
    assert {k: (r.ordinal, r.rel_x, r.rel_y) for k, r in a.rows.items()} == \
           {k: (r.ordinal, r.rel_x, r.rel_y) for k, r in b.rows.items()}


def test_forced_repair_avoids_key_collision():
    # b, z at rank 0 (both key "U"-adjacent, first-ever keys); c depends on
    # z so c is at rank 1 with key "U" (its rank's first-ever key too).
    kids = [task("b"), task("z"), task("c")]
    first = layout_container(scope(kids, edges=[("c", "z")]), mode="incremental")
    assert first.rows["b"].rank == 0 and first.rows["z"].rank == 0
    assert first.rows["c"].rank == 1
    # New edge b->z forces b down to rank 1, where it would collide with
    # c's key under the old "keep the old key" repair. Also insert n,
    # blocked by z, in the same pass.
    kids2 = kids + [task("n", created=5)]
    res = layout_container(
        scope(kids2, edges=[("c", "z"), ("b", "z"), ("n", "z")], existing=first.rows),
        mode="incremental",
    )
    ordinals = [r.ordinal for r in res.rows.values()]
    assert len(ordinals) == len(set(ordinals))  # no exception, no collisions
    assert res.rows["b"].rank == 1


def test_wallclock_stub_does_not_change_result(monkeypatch):
    kids = [task(f"t{i}", created=i) for i in range(30)]
    edges = [(f"t{i}", f"t{i-3}") for i in range(3, 30)]
    baseline = layout_container(scope(kids, edges=edges), mode="incremental", seed=7)

    real_monotonic = time.monotonic
    calls = {"n": 0}

    def fake_monotonic():
        calls["n"] += 1
        return real_monotonic() + calls["n"] * 0.01

    monkeypatch.setattr(engine_module.time, "monotonic", fake_monotonic)
    stubbed = layout_container(scope(kids, edges=edges), mode="incremental", seed=7)

    assert {k: (r.ordinal, r.rel_x, r.rel_y) for k, r in baseline.rows.items()} == \
           {k: (r.ordinal, r.rel_x, r.rel_y) for k, r in stubbed.rows.items()}


def test_target_is_computed_once_per_container_pass(monkeypatch):
    """``_tidy_sweep`` calls ``_evaluate`` thousands of times. The row target
    depends only on the children's sizes, so recomputing it inside the hot
    loop would be a pure regression (reorganisation design §3.1). The
    never-grows clamp is likewise once per pass, and its own cost is bounded
    by the ladder: ``1 + len(row_target_rungs(floor, up_to=target))`` flow
    passes (t24 finding F1)."""
    from src.task_graph.layout import flow as flow_module

    calls = {"row_target": 0, "clamp": 0, "flows": 0}
    real_row_target = flow_module.row_target
    real_clamp = flow_module.clamp_row_target
    real_flow_container = flow_module.flow_container

    def counting_row_target(*a, **kw):
        calls["row_target"] += 1
        return real_row_target(*a, **kw)

    def counting_clamp(*a, **kw):
        calls["clamp"] += 1
        return real_clamp(*a, **kw)

    def counting_flow_container(*a, **kw):
        calls["flows"] += 1
        return real_flow_container(*a, **kw)

    monkeypatch.setattr(flow_module, "row_target", counting_row_target)
    monkeypatch.setattr(engine_module, "row_target", counting_row_target)
    monkeypatch.setattr(engine_module, "clamp_row_target", counting_clamp)
    monkeypatch.setattr(engine_module, "flow_container", counting_flow_container)

    ids = [f"t{i}" for i in range(20)]
    kids = [task(i, created=k) for k, i in enumerate(ids)]
    edges = [(ids[i], ids[i - 4]) for i in range(4, 20)]
    # A real container, not the root: the root is deliberately not clamped.
    inner = ContainerScope(
        container_id="e",
        container_path="/e/",
        depth=1,
        children={t.id: t for t in kids},
        existing={},
        sibling_edges=list(edges),
        child_sizes={t.id: (CARD_W, CARD_H) for t in kids},
        origin=(0.0, 0.0),
    )
    layout_container(inner, mode="tidy")

    assert calls["flows"] > 1  # the sweep really did run
    assert calls["row_target"] == 1
    assert calls["clamp"] == 1


def test_deterministic_under_shuffled_inputs():
    """Dict insertion order is not a layout input: ``_sizes`` rebuilds the
    size map by walking ``scope.children``, so it is the CHILDREN's order
    that reaches the row target's float summation."""
    ids = [f"t{i}" for i in range(12)]
    sizes = {i: (1.0 + (k % 5), 1.0 + (k % 3)) for k, i in enumerate(ids)}
    kids = [task(i, created=k, container=True) for k, i in enumerate(ids)]
    forward = layout_container(scope(kids, sizes=dict(sizes)), mode="tidy")
    reverse = layout_container(
        scope(
            list(reversed(kids)),
            sizes={k: sizes[k] for k in reversed(ids)},
        ),
        mode="tidy",
    )
    assert {k: (r.ordinal, r.rel_x, r.rel_y) for k, r in forward.rows.items()} == \
           {k: (r.ordinal, r.rel_x, r.rel_y) for k, r in reverse.rows.items()}
    assert forward.allocated == reverse.allocated


def test_a_container_never_publishes_a_bigger_box_than_the_floor_would(monkeypatch):
    """The clamp reaches the published geometry, not just ``flow.py``.

    ``{pkg (3.0, 6.0), 4 unit cards}`` is one of F1's counterexamples: its
    ideal target is 11.8, which draws a 12 x 12 box where the floor draws
    6 x 12. The engine must publish the floor's box.
    """
    kids = [task("pkg", container=True)] + [task(f"c{i}", created=i) for i in range(4)]
    s = ContainerScope(
        container_id="e",
        container_path="/e/",
        depth=1,
        children={t.id: t for t in kids},
        existing={},
        sibling_edges=[],
        child_sizes={"pkg": (3.0, 6.0)},
        origin=(0.0, 0.0),
    )
    res = layout_container(s, mode="tidy")
    assert res.allocated == (6.0, 12.0)
    assert res.allocated[0] * res.allocated[1] == 72.0


def test_the_root_is_not_clamped_so_a_wide_epic_keeps_its_line_mates():
    """Symptom 1's fix is a ROOT effect. The root is never banded, so the
    clamp — which compares drawn boxes — must not be applied to it, or the
    operator's screenshot goes straight back to three ragged lines.  The
    finer growth ladder now yields two lines rather than the old one-line
    target, but it remains an unclamped improvement over the floor."""
    kids = [task("epic", container=True)] + [task(f"c{i}", created=i + 1) for i in range(8)]
    s = scope(kids, sizes={"epic": (12.0, 6.0)})
    res = layout_container(s, mode="tidy")
    assert len({r.rel_y for r in res.rows.values()}) == 2  # fewer than the floor's three


def test_incremental_ordering_ignores_activity_and_aggregates():
    """The activity-aware seed is a TIDY-only change (reorganisation design
    §3.2): incremental placement is still pure creation order, whatever the
    children's statuses are and whatever aggregates the scope carries."""
    plain = [task(f"n{i}", created=i) for i in range(6)]
    mixed = [
        SnapTask(
                id=f"n{i}",
                parent_id=None,
                is_container=i % 2 == 0,
                status=("COMPLETED", "IN_PROGRESS", "READY")[i % 3],
                created_at=i,
            )
        for i in range(6)
    ]
    aggs = {f"n{i}": {"descendants": 3, "running": i % 2, "active": 3} for i in range(6)}
    baseline = layout_container(scope(plain), mode="incremental")
    s = scope(mixed)
    s.child_aggregates = aggs
    with_activity = layout_container(s, mode="incremental")
    assert {c: r.ordinal for c, r in with_activity.rows.items()} == {
        c: r.ordinal for c, r in baseline.rows.items()
    }
