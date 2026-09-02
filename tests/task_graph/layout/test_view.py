from src.task_graph.layout.model import LayoutRow
from src.task_graph.layout.view import ancestors_of, depth_first_order, resolve_visible


def row(tid, path, depth, kind="card", children=0, rank=0, key="U", x=0.0, y=0.0):
    return LayoutRow(task_id=tid, container_id=ancestors_of(path)[-1] if depth else None,
                     path=path, depth=depth, rank=rank, order_key=key, w=1, h=1,
                     rel_x=x, rel_y=y, abs_x=x, abs_y=y, kind=kind, agg_children=children)


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
