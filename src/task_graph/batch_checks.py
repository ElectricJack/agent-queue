"""Automatic checks for an ingestion batch (held-batches spec L1.3, design §5.1).

One deterministic function judges a batch at propose, after every scoped
edit and inside release: :func:`run` loads the live graph around the batch
and :func:`check` compares the two.  Nothing here scores, calls an LLM or
reads meaning from words.  Every judgement uses declared metadata
(``section_anchor``, ``primary_outcome``, ``acceptance``, ``size``,
``owned_modules``, ``placement``, ``delivery_targets``) or graph structure.
When the metadata a check needs is absent, the report records the gap
instead of guessing.

Hard findings refuse the batch.  ``stale_revision``, ``active_claim`` and
``integration_owned`` are hard but ``retryable``: the batch is sound, but the
world around it has to settle first.  Advisory findings never refuse; the
fidelity reviewer reads them.

Edges follow ``task_dependencies``: ``(task_id, depends_on_task_id,
dep_type)``.  A parent-child link is ``(child, parent, "parent-child")``.
Only :data:`src.models.BLOCKING_DEP_TYPES` (``delivery-of`` included) enter
cycle, endpoint and ancestor checks.  Informational kinds never do.
"""

from __future__ import annotations

import shlex
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import select

from src.database.queries.hierarchy_queries import (
    CONTAINER_KEY,
    CONTAINER_VALUE,
    LIVE_SESSION_STATES,
)
from src.database.queries.proposal_queries import detect_cycles
from src.database.tables import (
    archived_tasks,
    ingest_batch_members,
    ingest_batches,
    sessions,
    task_dependencies,
    task_metadata,
    task_proposals,
    tasks,
)
from src.models import BLOCKING_DEP_TYPES, DepType, TaskStatus
from src.routing.filing import REFUSED_ROUTING_ARGS, is_routing_choice
from src.task_names import MAX_STRUCTURAL_DEPTH

PARENT_CHILD = DepType.PARENT_CHILD.value
DELIVERY_OF = DepType.DELIVERY_OF.value
#: Blocking kinds that order execution between leaves.  ``delivery-of``
#: orders post-landing work after a delivery and is checked on its own.
SERIALIZING_DEP_TYPES = frozenset({DepType.BLOCKS.value, DepType.CONDITIONAL_BLOCKS.value})

HARD_CODES = (
    "authority",
    "membership",
    "parent_not_unique",
    "cross_project",
    "depth_exceeded",
    "cycle",
    "container_endpoint",
    "external_target_invalid",
    "dependency_on_ancestor",
    "post_landing_inside_delivery_epic",
    "delivery_condition_cycle",
    "section_anchor_missing",
    "primary_outcome_missing",
    "acceptance_missing",
    "profile_pinned",
)
RETRYABLE_CODES = ("stale_revision", "active_claim", "integration_owned")
ADVISORY_CODES = (
    "large_leaf",
    "mixed_outcomes",
    "broad_command",
    "duplicate_title",
    "redundant_edge",
    "unexplained_serialization",
    "concentrated_ownership",
)

#: Test runners whose invocation :func:`_broad_command` can read.  This is
#: command syntax, not a vocabulary for guessing what a task means: anything
#: else is left alone.
_RUNNER_PREFIXES = (
    ("aq", "test"),
    ("pytest",),
    ("python", "-m", "pytest"),
    ("python3", "-m", "pytest"),
    ("uv", "run", "pytest"),
)
#: Options whose value narrows a run to named tests.
_SELECTOR_OPTIONS = frozenset({"-k", "--lf", "--last-failed", "--deselect"})

Edge = tuple[str, str, str]


@dataclass(frozen=True)
class Finding:
    """One check result. ``tasks`` and ``edges`` name what the reader should fix."""

    code: str
    detail: str
    tasks: tuple[str, ...] = ()
    edges: tuple[Edge, ...] = ()
    data: Mapping[str, Any] | None = None

    @property
    def severity(self) -> str:
        return "advisory" if self.code in ADVISORY_CODES else "hard"

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE_CODES

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "code": self.code,
            "severity": self.severity,
            "retryable": self.retryable,
            "detail": self.detail,
            "tasks": list(self.tasks),
            "edges": [
                {"from": frm, "to": to, "dep_type": kind} for frm, to, kind in self.edges
            ],
        }
        if self.data:
            out["data"] = dict(self.data)
        return out


@dataclass(frozen=True)
class CheckReport:
    """Every finding, in check order, plus the metadata gaps that blocked a check."""

    findings: tuple[Finding, ...] = ()
    gaps: tuple[Finding, ...] = ()

    @property
    def hard(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "hard"]

    @property
    def advisory(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "advisory"]

    @property
    def passed(self) -> bool:
        return not self.hard

    @property
    def retryable(self) -> bool:
        """Every hard finding waits on the live world, not on the batch."""
        hard = self.hard
        return bool(hard) and all(f.retryable for f in hard)

    @property
    def codes(self) -> list[str]:
        return [f.code for f in self.findings]

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "retryable": self.retryable,
            "hard": [f.to_dict() for f in self.hard],
            "advisory": [f.to_dict() for f in self.advisory],
            "gaps": [
                {"check": g.code, "detail": g.detail, "tasks": list(g.tasks)} for g in self.gaps
            ],
        }

    def raise_for_hard(self) -> None:
        if self.hard:
            raise BatchCheckFailed(self)


class BatchCheckFailed(ValueError):
    """A report with hard findings; ``code`` is the first one's."""

    def __init__(self, report: CheckReport):
        first = report.hard[0]
        super().__init__(f"{first.code}: {first.detail}")
        self.code = first.code
        self.retryable = report.retryable
        self.report = report


@dataclass(frozen=True)
class Authority:
    """The project and source the server established, never the payload's claim."""

    project_id: str
    source: str | None = None


@dataclass
class Node:
    """One batch member: a payload ``tempId`` or an existing member task id."""

    id: str
    title: str = ""
    #: Every parent the batch declares for this node, in declaration order.
    parents: list[str] = field(default_factory=list)
    section_anchor: Any = None
    primary_outcome: Any = None
    acceptance: Any = None
    #: ``None`` means undeclared, which is a gap; ``[]`` declares no modules.
    owned_modules: list[str] | None = None
    size: str | None = None
    placement: str | None = None
    delivery_targets: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    routing: list[str] = field(default_factory=list)
    existing: bool = False


@dataclass(frozen=True)
class BatchEdge:
    frm: str
    to: str
    kind: str
    reason: str | None = None

    @property
    def key(self) -> Edge:
        return (self.frm, self.to, self.kind)


@dataclass
class BatchGraph:
    """The batch as checks see it, from a payload or from stored member rows."""

    nodes: dict[str, Node] = field(default_factory=dict)
    edges: list[BatchEdge] = field(default_factory=list)
    #: Edits, as ``(task_id, refused routing keys, parent_id or None)``.
    edits: list[tuple[str, list[str], str | None]] = field(default_factory=list)
    remove_edges: list[Edge] = field(default_factory=list)
    comments: list[str] = field(default_factory=list)
    declared_project: str | None = None
    declared_source: str | None = None
    #: Read from stored rows: its edges are already live, not new declarations.
    stored: bool = False

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> BatchGraph:
        """Read a ``task_batch_propose`` change set with the L1.2 fields.

        The caller has already rejected malformed shapes (``task_changes``
        normalisation); this reads leniently so a check never crashes on a
        field it does not own.
        """
        graph = cls(
            declared_project=payload.get("project_id"),
            declared_source=payload.get("source"),
        )
        for spec in payload.get("tasks") or []:
            node_id = spec.get("tempId") or spec.get("task_id")
            if not isinstance(node_id, str) or not node_id:
                continue
            graph.nodes[node_id] = _node(node_id, spec, existing=False)
            if spec.get("parent_id"):
                graph.nodes[node_id].parents.append(spec["parent_id"])
        for edge in payload.get("edges") or []:
            graph._add_edge(
                edge.get("from"), edge.get("to"), edge.get("dep_type") or "blocks",
                edge.get("reason") or edge.get("description"),
            )
        for edit in payload.get("edits") or []:
            if isinstance(edit.get("task_id"), str):
                graph.edits.append(
                    (edit["task_id"], _routing_keys(edit), edit.get("parent_id") or None)
                )
        graph.remove_edges = [
            (edge.get("from"), edge.get("to"), edge.get("dep_type") or "blocks")
            for edge in payload.get("remove_edges") or []
        ]
        graph.comments = [
            c["task_id"] for c in payload.get("comments") or [] if isinstance(c.get("task_id"), str)
        ]
        graph._add_delivery_edges()
        return graph

    @classmethod
    def from_members(
        cls, members: Iterable[Mapping[str, Any]], edges: Iterable[Mapping[str, Any]]
    ) -> BatchGraph:
        """Read hydrated ``ingest_batch_members`` rows (the ``batch_digest`` shape).

        Rows may also carry ``owned_modules``, ``placement``,
        ``delivery_targets``, ``commands`` and the task's routing columns.
        Release and post-edit validation call this so the checks see
        exactly what will be released.
        """
        graph = cls(stored=True)
        for member in members:
            if member.get("state", "active") != "active":
                continue
            node = _node(member["task_id"], member, existing=True)
            if member.get("role") == "operational_root" and node.placement is None:
                node.placement = "operational_root"
            if member.get("parent_task_id"):
                node.parents.append(member["parent_task_id"])
            graph.nodes[node.id] = node
        for edge in edges:
            if edge["task_id"] in graph.nodes or edge["depends_on_task_id"] in graph.nodes:
                graph._add_edge(
                    edge["task_id"], edge["depends_on_task_id"], edge["dep_type"],
                    edge.get("description") or edge.get("reason"),
                )
        graph._add_delivery_edges()
        return graph

    def _add_edge(self, frm: Any, to: Any, kind: str, reason: str | None) -> None:
        if not isinstance(frm, str) or not isinstance(to, str):
            return
        self.edges.append(BatchEdge(frm, to, kind, reason or None))
        if kind == PARENT_CHILD and frm in self.nodes and to not in self.nodes[frm].parents:
            self.nodes[frm].parents.append(to)

    def _add_delivery_edges(self) -> None:
        present = {edge.key for edge in self.edges}
        for node in self.nodes.values():
            for target in node.delivery_targets:
                if (node.id, target, DELIVERY_OF) not in present:
                    self.edges.append(BatchEdge(node.id, target, DELIVERY_OF, None))
                    present.add((node.id, target, DELIVERY_OF))

    def references(self) -> set[str]:
        """Every non-member task id the batch names."""
        refs = {p for node in self.nodes.values() for p in node.parents}
        refs.update(t for node in self.nodes.values() for t in node.delivery_targets)
        refs.update(e for edge in self.edges for e in (edge.frm, edge.to))
        refs.update(e for edge in self.remove_edges for e in edge[:2])
        for tid, _, parent in self.edits:
            refs.update(ref for ref in (tid, parent) if ref)
        refs.update(self.comments)
        return {ref for ref in refs if isinstance(ref, str)} - self.nodes.keys()

    def controlled(self, members: Iterable[str] = ()) -> set[str]:
        """Existing tasks this batch changes: they must have no live holder."""
        ids = {node.id for node in self.nodes.values() if node.existing} | set(members)
        ids.update(tid for tid, _, _ in self.edits)
        # Stored snapshots include incoming informational links (including
        # the active reviewer). Reading them does not acquire write authority.
        if not self.stored:
            ids.update(edge.frm for edge in self.edges if edge.frm not in self.nodes)
        ids.update(frm for frm, _, _ in self.remove_edges if frm not in self.nodes)
        return {tid for tid in ids if isinstance(tid, str)}


def _node(node_id: str, spec: Mapping[str, Any], *, existing: bool) -> Node:
    modules = spec.get("owned_modules")
    return Node(
        id=node_id,
        title=spec.get("title") or "",
        section_anchor=spec.get("section_anchor"),
        primary_outcome=spec.get("primary_outcome"),
        acceptance=spec.get("acceptance"),
        owned_modules=[str(m) for m in modules] if isinstance(modules, list) else None,
        size=spec.get("size"),
        placement=spec.get("placement"),
        delivery_targets=[t for t in spec.get("delivery_targets") or [] if isinstance(t, str)],
        commands=[c for c in spec.get("commands") or [] if isinstance(c, str)],
        routing=_routing_keys(spec),
        existing=existing,
    )


def _routing_keys(spec: Mapping[str, Any]) -> list[str]:
    return [key for key in REFUSED_ROUTING_ARGS if key in spec and is_routing_choice(spec[key])]


@dataclass
class LiveGraph:
    """The committed graph around a batch. Tests build one directly."""

    project_id: str
    #: Project tasks by id: ``project_id``, ``status``, ``parent_task_id``,
    #: ``title``, ``assigned_agent_id``.
    tasks: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Referenced ids that live in another project, mapped to that project.
    foreign: dict[str, str] = field(default_factory=dict)
    archived: set[str] = field(default_factory=set)
    #: Tasks flagged ``container`` in metadata (children are added by checks).
    containers: set[str] = field(default_factory=set)
    edges: set[Edge] = field(default_factory=set)
    #: Tasks with a live session or an assigned agent.
    claimed: set[str] = field(default_factory=set)
    integration_owner: dict[str, Any] | None = None
    #: The stored batch, when the check runs against one.
    batch_project_id: str | None = None
    batch_source: str | None = None
    revision: int | None = None
    members: set[str] = field(default_factory=set)


class BatchNotFound(LookupError):
    pass


async def load_live_graph(
    conn, project_id: str, graph: BatchGraph, *, batch_id: str | None = None
) -> LiveGraph:
    """Read the project graph, claims and integration ownership around *graph*."""
    live = LiveGraph(project_id=project_id)
    if batch_id is not None:
        row = (
            await conn.execute(
                select(
                    ingest_batches.c.project_id,
                    ingest_batches.c.structure_revision,
                    task_proposals.c.source,
                )
                .select_from(
                    ingest_batches.join(
                        task_proposals, task_proposals.c.id == ingest_batches.c.batch_id
                    )
                )
                .where(ingest_batches.c.batch_id == batch_id)
            )
        ).mappings().first()
        if row is None:
            raise BatchNotFound(f"ingest batch '{batch_id}' not found")
        live.batch_project_id = row["project_id"]
        live.batch_source = row["source"]
        live.revision = row["structure_revision"]
        live.members = set(
            (
                await conn.execute(
                    select(ingest_batch_members.c.task_id).where(
                        ingest_batch_members.c.batch_id == batch_id,
                        ingest_batch_members.c.state == "active",
                    )
                )
            ).scalars()
        )
    rows = (
        await conn.execute(
            select(
                tasks.c.id,
                tasks.c.project_id,
                tasks.c.status,
                tasks.c.parent_task_id,
                tasks.c.title,
                tasks.c.assigned_agent_id,
            ).where(tasks.c.project_id == project_id)
        )
    ).mappings().all()
    live.tasks = {row["id"]: dict(row) for row in rows}
    elsewhere = sorted(graph.references() - live.tasks.keys())
    if elsewhere:
        live.foreign = {
            row["id"]: row["project_id"]
            for row in (
                await conn.execute(
                    select(tasks.c.id, tasks.c.project_id).where(tasks.c.id.in_(elsewhere))
                )
            ).mappings()
        }
        live.archived = set(
            (
                await conn.execute(
                    select(archived_tasks.c.id).where(archived_tasks.c.id.in_(elsewhere))
                )
            ).scalars()
        )
    live.edges = {
        (row[0], row[1], row[2])
        for row in await conn.execute(
            select(
                task_dependencies.c.task_id,
                task_dependencies.c.depends_on_task_id,
                task_dependencies.c.dep_type,
            )
            .join(tasks, tasks.c.id == task_dependencies.c.task_id)
            .where(tasks.c.project_id == project_id)
        )
    }
    live.containers = set(
        (
            await conn.execute(
                select(task_metadata.c.task_id)
                .join(tasks, tasks.c.id == task_metadata.c.task_id)
                .where(
                    tasks.c.project_id == project_id,
                    task_metadata.c.key == CONTAINER_KEY,
                    task_metadata.c.value == CONTAINER_VALUE,
                )
            )
        ).scalars()
    )
    controlled = sorted(graph.controlled(live.members))
    if controlled:
        live.claimed = set(
            (
                await conn.execute(
                    select(sessions.c.task_id).where(
                        sessions.c.task_id.in_(controlled),
                        sessions.c.state.in_(LIVE_SESSION_STATES),
                    )
                )
            ).scalars()
        )
        live.claimed |= {
            tid for tid in controlled if (live.tasks.get(tid) or {}).get("assigned_agent_id")
        }
        # Imported here: the integration package builds on the task graph.
        from src.integration.delegate_release import live_integration_owner

        live.integration_owner = await live_integration_owner(conn, controlled)
    return live


async def run(
    conn,
    payload: Mapping[str, Any] | BatchGraph,
    live_graph: LiveGraph | None = None,
    *,
    authority: Authority,
    expected_revision: int | None = None,
    batch_id: str | None = None,
) -> CheckReport:
    """Check *payload* (a change set or a :class:`BatchGraph`) against the live graph.

    Call it inside the caller's transaction so the live rows it reads are
    the ones the caller's write will see.
    """
    graph = payload if isinstance(payload, BatchGraph) else BatchGraph.from_payload(payload)
    if live_graph is None:
        live_graph = await load_live_graph(conn, authority.project_id, graph, batch_id=batch_id)
    return check(graph, live_graph, authority=authority, expected_revision=expected_revision)


def check(
    graph: BatchGraph,
    live: LiveGraph,
    *,
    authority: Authority,
    expected_revision: int | None = None,
) -> CheckReport:
    """Every §5.1 check, pure: the same inputs give the same report."""
    findings: list[Finding] = []
    gaps: list[Finding] = []

    def add(code: str, detail: str, ids: Iterable[str] = (), edges: Iterable[Edge] = (),
            data: Mapping[str, Any] | None = None) -> None:
        findings.append(Finding(code, detail, tuple(ids), tuple(edges), data))

    def gap(check_name: str, detail: str, ids: Iterable[str]) -> None:
        gaps.append(Finding(check_name, detail, tuple(sorted(ids))))

    nodes = graph.nodes
    allowed = nodes.keys() | live.members
    removed = set(graph.remove_edges)

    # -- authority ------------------------------------------------------
    for claimed, actual, what in (
        (graph.declared_project, authority.project_id, "project"),
        (live.project_id, authority.project_id, "project"),
        (live.batch_project_id, authority.project_id, "project"),
        (graph.declared_source, authority.source, "source"),
        (live.batch_source, authority.source, "source"),
    ):
        if claimed is not None and actual is not None and claimed != actual:
            add("authority", f"{what} '{claimed}' does not match the authenticated {what} "
                f"'{actual}'", data={"declared": claimed, "authenticated": actual})

    # -- references: project, existence, membership ---------------------
    for ref in sorted(graph.references()):
        if ref in live.tasks:
            continue
        if ref in live.foreign:
            add("cross_project", f"{ref} belongs to project '{live.foreign[ref]}', not "
                f"'{authority.project_id}'", [ref])
        elif ref in live.archived:
            add("external_target_invalid", f"{ref} is archived", [ref])
        else:
            add("external_target_invalid", f"{ref} does not exist", [ref])
    for edge in graph.edges:
        status = (live.tasks.get(edge.to) or {}).get("status")
        if edge.to not in nodes and status == TaskStatus.FAILED.value:
            add("external_target_invalid", f"{edge.frm} depends on FAILED task {edge.to}",
                [edge.to], [edge.key])
    for node in nodes.values():
        for parent in node.parents:
            if parent not in nodes:
                add("membership", f"{node.id}'s parent {parent} is outside the batch; parents "
                    "and children are created together", [node.id, parent])
    for edge in graph.edges:
        if edge.frm not in allowed and not (graph.stored and edge.kind not in BLOCKING_DEP_TYPES):
            add("membership", f"edge from {edge.frm} changes a task outside the batch",
                [edge.frm], [edge.key])
    for tid, _, _ in graph.edits:
        if tid not in allowed:
            add("membership", f"edit targets {tid}, which is not a batch member", [tid])
    for tid in graph.comments:
        if tid not in allowed:
            add("membership", f"comment targets {tid}, which is not a batch member", [tid])
    for edge in graph.remove_edges:
        if edge[0] not in allowed:
            add("membership", f"edge removal from {edge[0]} changes a task outside the batch",
                [edge[0]], [edge])

    # -- parents and depth ----------------------------------------------
    parent: dict[str, str | None] = {
        tid: row.get("parent_task_id") for tid, row in live.tasks.items()
    }
    for child, pid, kind in removed:
        if kind == PARENT_CHILD and parent.get(child) == pid:
            parent[child] = None
    for node in nodes.values():
        distinct = list(dict.fromkeys(node.parents))
        if len(distinct) > 1:
            add("parent_not_unique", f"{node.id} declares parents {distinct}",
                [node.id, *distinct])
        parent[node.id] = distinct[0] if distinct else None
    for tid, _, pid in graph.edits:
        if pid is not None:
            parent[tid] = pid

    def ancestors(tid: str) -> list[str]:
        chain, seen, current = [], {tid}, parent.get(tid)
        while current and current not in seen:
            chain.append(current)
            seen.add(current)
            current = parent.get(current)
        return chain

    for node in nodes.values():
        chain = ancestors(node.id)
        if len(chain) + 1 > MAX_STRUCTURAL_DEPTH:
            add("depth_exceeded", f"{node.id} sits at depth {len(chain) + 1}; the maximum "
                f"structural depth is {MAX_STRUCTURAL_DEPTH}", [node.id, *chain])

    containers = live.containers | {pid for pid in parent.values() if pid}
    leaves = [nid for nid in nodes if nid not in containers]

    # -- execution edges ------------------------------------------------
    batch_blocking = [e for e in graph.edges if e.kind in BLOCKING_DEP_TYPES]
    execution = {
        e for e in live.edges
        if e[2] in BLOCKING_DEP_TYPES and e[2] != PARENT_CHILD and e not in removed
    } | {e.key for e in batch_blocking if e.kind != PARENT_CHILD}
    structural = {(c, p, PARENT_CHILD) for c, p in parent.items() if p}
    batch_ids = set(nodes) | {e.frm for e in graph.edges} | {e.to for e in graph.edges}

    reported: set[frozenset[str]] = set()

    def report_cycles(edges: set[Edge], *, delivery_only: bool) -> None:
        for scc in detect_cycles(edges, [], []):
            members = frozenset(scc)
            if members in reported or not members & batch_ids:
                continue
            inside = sorted(e for e in edges if e[0] in members and e[1] in members)
            delivery = any(e[2] == DELIVERY_OF for e in inside)
            if delivery_only and not delivery:
                continue
            reported.add(members)
            if delivery:
                add("delivery_condition_cycle", "a delivery condition waits on work that "
                    "waits on it: post-landing work cannot feed its own build",
                    sorted(members), inside)
            else:
                add("cycle", "blocking edges form a cycle", sorted(members), inside)

    # The structural graph is the one task_changes.final_graph refuses on.
    report_cycles(execution | structural, delivery_only=False)
    # A delivery completes only when its whole subtree has: model each
    # container as waiting on its children so a leaf of the target that waits
    # on the post-landing root closes the loop.
    report_cycles(
        execution | {(p, c, PARENT_CHILD) for c, p in parent.items() if p},
        delivery_only=True,
    )

    for edge in batch_blocking:
        if edge.kind == PARENT_CHILD:
            continue
        # waits-for fans in on a container's children and delivery-of waits
        # on a delivery epic: only their dependent end must be a leaf.
        fan_in = edge.kind in (DepType.WAITS_FOR.value, DELIVERY_OF)
        ends = [edge.frm] if fan_in else [edge.frm, edge.to]
        if hit := [end for end in ends if end in containers]:
            add("container_endpoint", f"{edge.kind} edge {edge.frm} -> {edge.to} touches "
                f"container(s) {hit}; execution edges connect leaves", hit, [edge.key])
        if edge.to in ancestors(edge.frm):
            add("dependency_on_ancestor", f"{edge.frm} depends on its ancestor {edge.to}, "
                "which completes only after its descendants", [edge.frm, edge.to], [edge.key])

    # -- placement ------------------------------------------------------
    for node in nodes.values():
        post_landing = node.placement == "operational_root" or node.delivery_targets or any(
            e.frm == node.id and e.kind == DELIVERY_OF for e in graph.edges
        )
        if post_landing and parent.get(node.id):
            add("post_landing_inside_delivery_epic", f"{node.id} is post-landing work but is "
                f"placed under {parent[node.id]}; operational roots have no parent",
                [node.id, parent[node.id]])

    # -- fidelity fields ------------------------------------------------
    for node in nodes.values():
        if _blank(node.section_anchor):
            add("section_anchor_missing", f"{node.id} names no spec section", [node.id])
    for nid in leaves:
        if _blank(nodes[nid].primary_outcome):
            add("primary_outcome_missing", f"leaf {nid} declares no primary outcome", [nid])
        if _blank(nodes[nid].acceptance):
            add("acceptance_missing", f"leaf {nid} declares no acceptance", [nid])
    for node in nodes.values():
        if node.routing:
            add("profile_pinned", f"{node.id} chooses a route ({', '.join(node.routing)}); "
                "routing is the router's", [node.id], data={"keys": node.routing})
    for tid, keys, _ in graph.edits:
        if keys:
            add("profile_pinned", f"edit of {tid} chooses a route ({', '.join(keys)})", [tid],
                data={"keys": keys})

    # -- retryable: the live world must settle --------------------------
    if expected_revision is not None and live.revision is not None and (
        expected_revision != live.revision
    ):
        add("stale_revision", f"batch is at structure revision {live.revision}, not "
            f"{expected_revision}; reload and retry",
            data={"expected": expected_revision, "current": live.revision})
    controlled = graph.controlled(live.members)
    if held := sorted(controlled & live.claimed):
        add("active_claim", f"live holders must stop before the batch changes: {held}", held)
    if live.integration_owner:
        owner = live.integration_owner
        add("integration_owned", f"{owner.get('task_id')} is owned by integration operation "
            f"{owner.get('operation_id')} ({owner.get('state')})",
            [owner["task_id"]] if owner.get("task_id") else [], data=dict(owner))

    # -- advisory -------------------------------------------------------
    sizeless = [nid for nid in leaves if nodes[nid].size is None]
    if sizeless:
        gap("large_leaf", "leaves without a declared size", sizeless)
    for nid in leaves:
        if nodes[nid].size == "l":
            add("large_leaf", f"leaf {nid} is declared size l; consider splitting it", [nid])

    for node in nodes.values():
        for name in ("primary_outcome", "section_anchor"):
            values = _distinct(getattr(node, name))
            if len(values) > 1:
                add("mixed_outcomes", f"{node.id} declares {len(values)} {name} values; one "
                    "task should land one outcome", [node.id], data={name: values})

    for node in nodes.values():
        for command in [*node.commands, *_backticked(node.acceptance)]:
            if _broad_command(command):
                add("broad_command", f"{node.id} runs '{command}', which names no test file "
                    "or selector", [node.id], data={"command": command})

    by_title: dict[str, list[str]] = defaultdict(list)
    for node in nodes.values():
        if node.title.strip():
            by_title[_norm(node.title)].append(node.id)
    open_titles: dict[str, list[str]] = defaultdict(list)
    for tid, row in sorted(live.tasks.items()):
        if tid in allowed or row.get("status") in (TaskStatus.COMPLETED.value,
                                                   TaskStatus.FAILED.value):
            continue
        if row.get("title"):
            open_titles[_norm(row["title"])].append(tid)
    for title, ids in by_title.items():
        same = ids + open_titles.get(title, [])
        if len(same) > 1:
            add("duplicate_title", f"title '{title}' repeats across {same}", same)

    counts: dict[Edge, int] = defaultdict(int)
    for edge in graph.edges:
        counts[edge.key] += 1
    adjacency: dict[str, set[str]] = defaultdict(set)
    for frm, to, _ in execution:
        adjacency[frm].add(to)
    for edge in dict.fromkeys(e.key for e in batch_blocking if e.kind != PARENT_CHILD):
        if counts[edge] > 1 or (not graph.stored and edge in live.edges):
            add("redundant_edge", f"{edge[0]} -> {edge[1]} ({edge[2]}) is declared twice",
                [edge[0], edge[1]], [edge])
        elif _reaches(adjacency, edge[0], edge[1], skip=(edge[0], edge[1])):
            add("redundant_edge", f"{edge[0]} -> {edge[1]} is implied by a longer path",
                [edge[0], edge[1]], [edge])

    leaf_set = set(leaves)
    for edge in graph.edges:
        if (edge.kind in SERIALIZING_DEP_TYPES and edge.frm in leaf_set
                and edge.to in leaf_set and not (edge.reason or "").strip()):
            add("unexplained_serialization", f"{edge.frm} waits on {edge.to} with no stated "
                "reason; leaves run in parallel unless one needs the other",
                [edge.frm, edge.to], [edge.key])

    unowned = [nid for nid in leaves if nodes[nid].owned_modules is None]
    if unowned:
        gap("concentrated_ownership", "leaves without declared owned_modules", unowned)
    owners: dict[str, list[str]] = defaultdict(list)
    for nid in leaves:
        for module in dict.fromkeys(nodes[nid].owned_modules or []):
            owners[module].append(nid)
    for module, ids in sorted(owners.items()):
        if len(ids) > 1:
            add("concentrated_ownership", f"{module} is owned by {len(ids)} leaves {ids}",
                ids, data={"module": module})

    return CheckReport(tuple(findings), tuple(gaps))


def _blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple)):
        return all(_blank(item) for item in value)
    return False


def _distinct(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return list(dict.fromkeys(str(v).strip() for v in value if not _blank(v)))


def _norm(title: str) -> str:
    return " ".join(title.lower().split())


def _backticked(acceptance: Any) -> list[str]:
    texts = acceptance if isinstance(acceptance, (list, tuple)) else [acceptance]
    spans: list[str] = []
    for text in texts:
        if isinstance(text, str):
            spans.extend(text.split("`")[1::2])
    return spans


def _broad_command(command: str) -> bool:
    """A recognised test-runner call that names no file and no selector."""
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    for prefix in _RUNNER_PREFIXES:
        if tuple(argv[: len(prefix)]) == prefix:
            args = argv[len(prefix):]
            break
    else:
        return False
    for arg in args:
        if arg in _SELECTOR_OPTIONS or arg.startswith("-k") or "::" in arg:
            return False
        if not arg.startswith("-") and PurePosixPath(arg).suffix:
            return False
    return True


def _reaches(adjacency: Mapping[str, set[str]], start: str, goal: str,
             *, skip: tuple[str, str]) -> bool:
    """Whether *goal* is reachable from *start* without the direct edge *skip*."""
    stack = [n for n in adjacency.get(start, ()) if (start, n) != skip]
    seen = set(stack)
    while stack:
        current = stack.pop()
        if current == goal:
            return True
        for nxt in adjacency.get(current, ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return False
