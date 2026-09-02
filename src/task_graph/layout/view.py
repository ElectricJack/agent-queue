"""Viewport resolution over persisted layout rows (spec §5.2-§5.5)."""

from __future__ import annotations

from dataclasses import dataclass, field

from src.task_graph.layout.model import LayoutRow


def ancestors_of(path: str) -> list[str]:
    parts = [p for p in path.split("/") if p]
    return parts[:-1]


@dataclass
class Visible:
    visible: dict[str, str] = field(default_factory=dict)
    collapsed_paths: dict[str, str] = field(default_factory=dict)
    root_path: str | None = None


def resolve_visible(
    rows: dict[str, LayoutRow], *, expanded: set[str], max_depth: int | None,
    root: str | None, forced_expanded: set[str],
) -> Visible:
    out = Visible()
    opened = set(expanded) | set(forced_expanded)
    if root is not None:
        if root not in rows:
            return out
        out.root_path = rows[root].path
        opened.add(root)
    for tid, r in rows.items():
        if out.root_path and not r.path.startswith(out.root_path):
            continue
        anc = ancestors_of(r.path)
        if out.root_path:
            anc = anc[anc.index(root) + 1:] if root in anc else anc
        ok = True
        for a in anc:
            ar = rows.get(a)
            if a not in opened or (max_depth is not None and (ar is None or ar.depth >= max_depth)):
                ok = False
                break
        if not ok or (max_depth is not None and r.depth > max_depth and tid != root):
            continue
        if r.kind != "container":
            out.visible[tid] = r.kind
            continue
        expanded_here = tid in opened and (max_depth is None or r.depth < max_depth or tid == root)
        if r.agg_children == 0 or expanded_here:
            out.visible[tid] = "container"
        else:
            out.visible[tid] = "collapsed"
            out.collapsed_paths[tid] = r.path
    return out


def depth_first_order(rows: dict[str, LayoutRow]) -> list[str]:
    def key(r: LayoutRow) -> tuple:
        parts = [p for p in r.path.split("/") if p]
        return tuple((rows[p].rank, rows[p].order_key) if p in rows else (0, "") for p in parts)
    return sorted(rows, key=lambda t: key(rows[t]))
