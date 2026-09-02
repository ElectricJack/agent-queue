from src.task_graph.layout.model import LayoutRow
from src.task_graph.layout.view import (
    ancestors_of,
    cap_stubs,
    depth_first_order,
    dock_workers,
    forced_expansion_for,
    owner_map,
    remap_edges,
    resolve_visible,
)


def row(tid, path, depth, kind="card", children=0, rank=0, key="U", x=0.0, y=0.0):
    return LayoutRow(
        task_id=tid,
        container_id=ancestors_of(path)[-1] if depth else None,
        path=path,
        depth=depth,
        rank=rank,
        order_key=key,
        w=1,
        h=1,
        rel_x=x,
        rel_y=y,
        abs_x=x,
        abs_y=y,
        kind=kind,
        agg_children=children,
    )


ROWS = {
    "e": row("e", "/e/", 0, "container", children=2),
    "p": row("p", "/e/p/", 1, "container", children=1),
    "t": row("t", "/e/p/t/", 2),
    "z": row("z", "/z/", 0),
    "empty": row("empty", "/empty/", 0, "container", children=0),
}


def test_ancestors_of():
    assert ancestors_of("/e/p/t/") == ["e", "p"]
    assert ancestors_of("/e/") == []


def test_default_collapsed_shows_only_top_level():
    v = resolve_visible(ROWS, expanded=set(), max_depth=None, root=None, forced_expanded=set())
    assert v.visible == {"e": "collapsed", "z": "card", "empty": "container"}
    assert v.collapsed_paths == {"e": "/e/"}


def test_expanding_reveals_one_level():
    v = resolve_visible(ROWS, expanded={"e"}, max_depth=None, root=None, forced_expanded=set())
    assert v.visible["e"] == "container" and v.visible["p"] == "collapsed" and "t" not in v.visible


def test_max_depth_collapses_deeper_containers():
    v = resolve_visible(ROWS, expanded={"e", "p"}, max_depth=1, root=None, forced_expanded=set())
    assert v.visible["p"] == "collapsed" and "t" not in v.visible


def test_root_restricts_and_expands_itself():
    v = resolve_visible(ROWS, expanded=set(), max_depth=None, root="e", forced_expanded=set())
    assert set(v.visible) == {"e", "p"} and v.visible["e"] == "container"
    assert v.root_path == "/e/"


def test_root_below_top_level_keeps_root_row_itself():
    v = resolve_visible(ROWS, expanded=set(), max_depth=None, root="p", forced_expanded=set())
    assert v.visible == {"p": "container", "t": "card"}


def test_forced_expanded_acts_like_expanded():
    v = resolve_visible(ROWS, expanded=set(), max_depth=None, root=None, forced_expanded={"e", "p"})
    assert "t" in v.visible


def test_depth_first_order_uses_ordinals():
    rows = {
        "b": row("b", "/b/", 0, rank=0, key="A"),
        "a": row("a", "/a/", 0, rank=0, key="B"),
        "a1": row("a1", "/a/a1/", 1, rank=1, key="U"),
        "a0": row("a0", "/a/a0/", 1, rank=0, key="U"),
    }
    assert depth_first_order(rows) == ["b", "a", "a0", "a1"]


def test_owner_map_longest_prefix():
    paths = {"t": "/e/p/t/", "p": "/e/p/"}
    assert owner_map(paths, {"e": "/e/", "p": "/e/p/"}) == {"t": "p", "p": "p"}


def test_remap_dedupes_and_drops_hierarchy_edges():
    visible = {"e": "collapsed", "z": "card"}
    owner = {"t1": "e", "t2": "e"}
    edges = [
        ("z", "t1", "blocks", None),
        ("z", "t2", "blocks", None),
        ("t1", "e", "parent-child", None),
        ("t1", "t2", "blocks", None),
    ]
    wire, orphans = remap_edges(edges, visible, owner)
    assert wire == [{"from": "z", "to": "e", "dep_type": "blocks", "description": None, "count": 2}]
    assert orphans == set()


def test_remap_reports_orphans_for_stubs():
    wire, orphans = remap_edges([("z", "far", "blocks", None)], {"z": "card"}, {})
    assert orphans == {"far"} and wire[0]["to"] == "far"


def test_cap_stubs_keeps_eight_then_summarizes():
    hub = {"hub": "card"}
    edges = [
        {"from": f"d{i}", "to": "hub", "dep_type": "blocks", "description": None, "count": 1}
        for i in range(12)
    ]
    stub_rows = {f"d{i}": row(f"d{i}", f"/d{i}/", 0, x=float(i)) for i in range(12)}
    kept, stubs, more = cap_stubs(edges, stub_rows, set(hub), limit=8)
    assert len(kept) == 8 and len(stubs) == 8
    assert more == [{"node_id": "hub", "direction": "in", "more": 4}]


def test_cap_stubs_drops_edges_with_no_visible_endpoint():
    kept, stubs, _more = cap_stubs(
        [{"from": "x", "to": "y", "dep_type": "blocks", "description": None, "count": 1}],
        {},
        {"z"},
    )
    assert kept == [] and stubs == []


def test_cap_stubs_counts_distinct_far_nodes_not_edges():
    hub = {"hub": "card"}
    dep_types = ["blocks", "waits-for", "conditional-blocks", "discovered-from"]
    edges = [
        {"from": "far", "to": "hub", "dep_type": t, "description": None, "count": 1}
        for t in dep_types
    ]
    stub_rows = {"far": row("far", "/far/", 0)}
    kept, stubs, more = cap_stubs(edges, stub_rows, set(hub), limit=2)
    assert len(kept) == 4 and len(stubs) == 1
    assert more == []


def test_dock_workers_on_visible_ancestor():
    agents = [
        {"id": "a1", "current_task_id": "t"},
        {"id": "a2", "current_task_id": "z"},
        {"id": "a3", "current_task_id": None},
    ]
    docked = dock_workers(agents, {"e", "z"}, {"t": "e"})
    assert [(d["docked_at"], d["in_collapsed"]) for d in docked] == [("e", True), ("z", False)]


def test_forced_expansion_is_all_ancestors():
    assert forced_expansion_for({"t"}, ROWS) == {"e", "p"}
