from src.task_graph.layout.constants import CARD_H, CARD_W
from src.task_graph.layout.engine import layout_container
from src.task_graph.layout.model import ContainerScope, SnapTask


def task(i, created=0.0, container=False, status="READY"):
    return SnapTask(id=i, parent_id=None, is_container=container, status=status, created_at=created)


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
