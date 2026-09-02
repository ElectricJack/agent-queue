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
class GraphNode:
    """One task in the graph, keyed graph-locally by ``key``."""

    key: str
    title: str = ""
    description: str = ""
    acceptance: list[str] = field(default_factory=list)
    deliverables: list[dict[str, str]] = field(default_factory=list)
    context: list[GraphContext] = field(default_factory=list)
    needs: list[GraphNeed] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    priority: int = 100
    profile: str | None = None
    intelligence_class: str | None = None
    task_type: str | None = None
    #: Present only when the author (wrongly) scoped a node to a project —
    #: graphs are single-project, so the validator rejects it.
    project: str | None = None

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "title": self.title,
            "description": self.description,
            "acceptance": list(self.acceptance),
            "deliverables": [dict(item) for item in self.deliverables],
            "context": [c.to_dict() for c in self.context],
            "needs": [n.to_dict() for n in self.needs],
            "labels": list(self.labels),
            "priority": self.priority,
            "profile": self.profile,
            "intelligence_class": self.intelligence_class,
            "task_type": self.task_type,
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
            "nodes": [n.to_dict() for n in self.nodes],
            "from_spec": self.from_spec,
            "source_path": self.source_path,
        }
