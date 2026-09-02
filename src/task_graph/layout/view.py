"""Viewport resolution over persisted layout rows (spec §5.2-§5.5)."""

from __future__ import annotations

from collections import defaultdict
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


DRAWN_TYPES = frozenset({"blocks", "waits-for", "conditional-blocks", "discovered-from"})


def owner_map(rows_in_collapsed: dict[str, LayoutRow], collapsed_paths: dict[str, str]) -> dict[str, str]:
    by_len = sorted(collapsed_paths.items(), key=lambda kv: -len(kv[1]))
    out: dict[str, str] = {}
    for tid, r in rows_in_collapsed.items():
        for cid, p in by_len:
            if r.path.startswith(p):
                out[tid] = cid
                break
    return out


def remap_edges(edges, visible, hidden_owner):
    agg: dict[tuple[str, str, str], dict] = {}
    orphans: set[str] = set()

    def target(x: str) -> str:
        if x in visible:
            return x
        if x in hidden_owner:
            return hidden_owner[x]
        orphans.add(x)
        return x

    for dep, blocker, typ, desc in edges:
        if typ not in DRAWN_TYPES:
            continue
        f, t = target(dep), target(blocker)
        if f == t:
            continue
        key = (f, t, typ)
        if key in agg:
            agg[key]["count"] += 1
            if agg[key]["description"] is None and desc:
                agg[key]["description"] = desc
        else:
            agg[key] = {"from": f, "to": t, "dep_type": typ, "description": desc, "count": 1}
    wire = sorted(agg.values(), key=lambda e: (e["from"], e["to"], e["dep_type"]))
    # An orphan that never survived (self-loop) is not a stub candidate.
    used = {e["from"] for e in wire} | {e["to"] for e in wire}
    return wire, {o for o in orphans if o in used}


def cap_stubs(edges, stub_rows, visible, limit=8):
    per_node_dir: dict[tuple[str, str], int] = defaultdict(int)
    kept: list[dict] = []
    stubs: dict[str, dict] = {}
    more: dict[tuple[str, str], int] = defaultdict(int)
    for e in edges:
        f, t = e["from"], e["to"]
        far = None
        anchor = None
        direction = None
        if f in visible and t not in visible:
            far, anchor, direction = t, f, "out"
        elif t in visible and f not in visible:
            far, anchor, direction = f, t, "in"
        if far is None:
            kept.append(e)
            continue
        if far not in stub_rows:
            continue  # far endpoint has no row in this variant: drop
        if per_node_dir[(anchor, direction)] >= limit:
            more[(anchor, direction)] += 1
            continue
        per_node_dir[(anchor, direction)] += 1
        kept.append(e)
        r = stub_rows[far]
        stubs.setdefault(far, {"id": far, "x": r.abs_x, "y": r.abs_y, "w": r.w, "h": r.h})
    more_list = [{"node_id": n, "direction": d, "more": c} for (n, d), c in sorted(more.items())]
    return kept, list(stubs.values()), more_list


def dock_workers(agents, visible, hidden_owner):
    out = []
    for a in agents:
        cur = a.get("current_task_id")
        if not cur:
            continue
        if cur in visible:
            out.append({"agent": a, "docked_at": cur, "in_collapsed": False})
        elif cur in hidden_owner:
            out.append({"agent": a, "docked_at": hidden_owner[cur], "in_collapsed": True})
    return out


def forced_expansion_for(matches, rows):
    out: set[str] = set()
    for m in matches:
        r = rows.get(m)
        if r:
            out.update(ancestors_of(r.path))
    return out
