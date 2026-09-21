"""Task-graph document model.

The in-memory shape of an ``aq-graph`` document — see
docs/specs/design/supervisor-agent.md §8.2 for the authored format and
docs/specs/implementation/supervisor-agent.md §8 for the module contract.

These are plain dataclasses with no I/O: :mod:`src.task_graph.parser` builds
them, :mod:`src.task_graph.validator` checks them, and
:mod:`src.task_graph.creator` turns a validated one into rows.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Default edge kind for a ``needs`` entry written in shorthand.
DEFAULT_DEP_TYPE = "blocks"


@dataclass
class GraphError:
    """One validation finding.

    ``rule`` is the stable machine-readable name from the design §8.3 table —
    tests and callers match on it, so the strings are part of the contract.
    ``severity`` is ``"error"`` (nothing is created) or ``"warning"``
    (created anyway, reported).
    """

    rule: str
    detail: str
    node: str | None = None
    severity: str = "error"

    @property
    def is_error(self) -> bool:
        return self.severity == "error"

    def to_dict(self) -> dict:
        return {
            "rule": self.rule,
            "node": self.node,
            "detail": self.detail,
            "severity": self.severity,
        }


class GraphParseError(Exception):
    """Raised when a graph document cannot be turned into a :class:`TaskGraph`.

    Carries structured :class:`GraphError` findings so the command surface can
    report them the same way validation findings are reported.
    """

    def __init__(self, errors: list[GraphError]):
        self.errors = errors
        super().__init__("; ".join(f"{e.rule}: {e.detail}" for e in errors) or "invalid graph")


@dataclass
class GraphNeed:
    """A dependency edge declared by a node.

    ``on`` names either another node's ``key`` or an existing task id.
    ``cross_project`` must be set explicitly for an edge onto a task in a
    different project (design §8.3, todo §3b).
    """

    on: str
    dep_type: str = DEFAULT_DEP_TYPE
    cross_project: bool = False

    def to_dict(self) -> dict:
        return {"on": self.on, "dep_type": self.dep_type, "cross_project": self.cross_project}


@dataclass
class GraphContext:
    """One ``task_context`` entry a node carries into execution.

    ``type='spec_ref'`` rows point at a vault spec section; ``type='file'``
    rows name a path in the repo.  Any other type is stored verbatim, which
    keeps the format open for context kinds later specs introduce.
    """

    type: str
    path: str | None = None
    section: str | None = None
    label: str | None = None
    content: str | None = None

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "path": self.path,
            "section": self.section,
            "label": self.label,
            "content": self.content,
        }


@dataclass
class GraphSubtask:
    """One ``task_subtasks`` checklist row a node declares.

    Subtasks are never scheduled or claimed — they are per-task progress
    visibility for the single agent that holds the task.  They live in the
    graph grammar because the subtask write fence only lets a session write
    subtasks on the task it *holds*, so a planner can never seed them after
    the fact: whoever writes the task row must write the checklist with it
    (see the planning-emits-subtasks design §2.1).
    """

    title: str
    context: str = ""

    def to_dict(self) -> dict:
        return {"title": self.title, "context": self.context}


@dataclass
class GraphPhase:
    """One ordered phase container the graph's nodes are filed into.

    A phase is an ordinary container task carrying ``task_metadata.phase``
    plus a ``blocks`` edge onto every earlier sibling phase, so everything in
    phase *N+1* waits for phase *N* with no per-task edge (work-graph §13b).
    ``key`` is graph-local — a node names it in its own ``phase`` field — and
    document order is the phase order, 1..N.
    """

    key: str
    title: str = ""
    label: str | None = None

    def to_dict(self) -> dict:
        return {"key": self.key, "title": self.title, "label": self.label}


@dataclass
class GraphNode:
    """One task in the graph, keyed graph-locally by ``key``."""

    key: str
    title: str = ""
    description: str = ""
    acceptance: list[str] = field(default_factory=list)
    deliverables: list[dict[str, str]] = field(default_factory=list)
    context: list[GraphContext] = field(default_factory=list)
    needs: list[GraphNeed] = field(default_factory=list)
    #: Checklist rows seeded onto this node's task, in document order.
    subtasks: list[GraphSubtask] = field(default_factory=list)
    #: ``key`` of the declared phase this node belongs to.  ``None`` keeps the
    #: node a direct child of the container, beside the phases.
    phase: str | None = None
    labels: list[str] = field(default_factory=list)
    priority: int = 100
    profile: str | None = None
    intelligence_class: str | None = None
    #: ``pin: true`` -- the node's ``profile`` is a pinned provider, not a
    #: preference (provider-failover D9).  Meaningless without a profile.
    pin: bool = False
    #: How ``profile`` was chosen: ``document`` (written on the node or in
    #: ``defaults``), ``fill_in`` (``create_task_graph``'s ``profile_id``) or
    #: ``class_match`` (resolved from the class).  Decides the task's
    #: ``provider_intent``; not part of the document, so never serialised.
    profile_source: str | None = None
    task_type: str | None = None
    #: Present only when the author (wrongly) scoped a node to a project —
    #: graphs are single-project, so the validator rejects it.
    project: str | None = None

    @property
    def provider_intent(self) -> str:
        """The ``tasks.provider_intent`` this node's task is written with (D9).

        A profile the document or the caller named is ``preferred`` --
        ``pinned`` with ``pin: true``; one resolved from the class, or none,
        is ``class_only``.
        """
        if not self.profile or self.profile_source == "class_match":
            return "class_only"
        return "pinned" if self.pin else "preferred"

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "title": self.title,
            "description": self.description,
            "acceptance": list(self.acceptance),
            "deliverables": [dict(item) for item in self.deliverables],
            "context": [c.to_dict() for c in self.context],
            "needs": [n.to_dict() for n in self.needs],
            "subtasks": [s.to_dict() for s in self.subtasks],
            "phase": self.phase,
            "labels": list(self.labels),
            "priority": self.priority,
            "profile": self.profile,
            "intelligence_class": self.intelligence_class,
            "task_type": self.task_type,
            # Only when set, so every document written before the key existed
            # keeps its exact serialised shape (formula snapshots, reports).
            **({"pin": True} if self.pin else {}),
        }


@dataclass
class GraphParent:
    """The container task every graph hangs under."""

    title: str = ""
    description: str = ""
    profile: str | None = None
    labels: list[str] = field(default_factory=list)
    priority: int = 100

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "description": self.description,
            "profile": self.profile,
            "labels": list(self.labels),
            "priority": self.priority,
        }


@dataclass
class TaskGraph:
    """A parsed graph document."""

    version: int = 1
    spec: str | None = None
    vars: dict[str, str] = field(default_factory=dict)
    defaults: dict = field(default_factory=dict)
    parent: GraphParent | None = None
    #: Ordered phase containers, document order = phase order 1..N.  Empty
    #: for every document written before the key existed.
    phases: list[GraphPhase] = field(default_factory=list)
    nodes: list[GraphNode] = field(default_factory=list)
    #: Set when the graph came from ``--from-spec``.  Drives the severity of
    #: the ``spec_ref`` checks: error from a spec, warning from a bare graph
    #: document (design §8.3).
    from_spec: bool = False
    #: Vault path of the spec the graph was extracted from, if any.
    source_path: str | None = None

    def node_keys(self) -> list[str]:
        return [n.key for n in self.nodes]

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "spec": self.spec,
            "vars": dict(self.vars),
            "defaults": dict(self.defaults),
            "parent": self.parent.to_dict() if self.parent else None,
            "phases": [p.to_dict() for p in self.phases],
            "nodes": [n.to_dict() for n in self.nodes],
            "from_spec": self.from_spec,
            "source_path": self.source_path,
        }
