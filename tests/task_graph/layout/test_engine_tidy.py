from src.task_graph.layout.cost import count_crossings
from src.task_graph.layout.engine import _ordered_from, layout_container
from src.task_graph.layout.model import ContainerScope, SnapTask
from src.task_graph.layout.constants import CARD_H, CARD_W


def task(i, created=0.0):
    return SnapTask(id=i, parent_id=None, is_container=False, status="READY", created_at=created)


def scope(children, edges):
    kids = {t.id: t for t in children}
    return ContainerScope(
        container_id="epic", container_path="/epic/", depth=1, children=kids, existing={},
        sibling_edges=edges, child_sizes={t.id: (CARD_W, CARD_H) for t in children},
    )


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
