from src.task_graph.layout import engine as engine_module
from src.task_graph.layout.cost import count_crossings
from src.task_graph.layout.engine import _ordered_from, layout_container
from src.task_graph.layout.model import ContainerScope, SnapTask
from src.task_graph.layout.constants import CARD_H, CARD_W


def task(i, created=0.0, status="READY"):
    return SnapTask(id=i, parent_id=None, is_container=False, status=status, created_at=created)


def scope(children, edges, aggregates=None):
    kids = {t.id: t for t in children}
    return ContainerScope(
        container_id="epic", container_path="/epic/", depth=1, children=kids, existing={},
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
    res = layout_container(scope(kids, edges), mode="tidy")
    assert crossings_of(res, edges) == 0


def test_tidy_pinned_bound_on_fixture():
    # Two interleaved chains plus cross links: bound pinned at 2 crossings.
    kids = [task(f"n{i}", created=(i * 7) % 12) for i in range(12)]
    edges = [(f"n{i}", f"n{i-2}") for i in range(2, 12)] + [("n5", "n0"), ("n11", "n4")]
    res = layout_container(scope(kids, edges), mode="tidy")
    assert crossings_of(res, edges) <= 2


def test_tidy_is_deterministic():
    kids = [task(f"n{i}", created=i) for i in range(20)]
    edges = [(f"n{i}", f"n{(i * 3) % 20}") for i in range(1, 20) if (i * 3) % 20 < i]
    a = layout_container(scope(kids, edges), mode="tidy", seed=1)
    b = layout_container(scope(kids, edges), mode="tidy", seed=1)
    assert {c: r.ordinal for c, r in a.rows.items()} == {c: r.ordinal for c, r in b.rows.items()}


def test_tidy_puts_running_work_first_and_finished_last():
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
    res = layout_container(scope(kids, []), mode="tidy")
    assert reading_order(res) == ["run0", "ready0", "ready1", "ready2", "done0", "done1"]


def test_tidy_output_is_unchanged_when_every_sibling_is_one_class(monkeypatch):
    """Every task here is READY, so the new key reduces to ``(created_at, id)``
    and the published ordinals must be byte-identical to the pre-change seed."""
    kids = [task(f"n{i}", created=(i * 7) % 12) for i in range(12)]
    edges = [(f"n{i}", f"n{i-2}") for i in range(2, 12)] + [("n5", "n0"), ("n11", "n4")]
    after = layout_container(scope(kids, edges), mode="tidy", seed=3)

    monkeypatch.setattr(
        engine_module, "tidy_seed_key", lambda t, agg=None: (t.created_at, t.id)
    )
    before = layout_container(scope(kids, edges), mode="tidy", seed=3)

    assert {c: r.ordinal for c, r in after.rows.items()} == {
        c: r.ordinal for c, r in before.rows.items()
    }
