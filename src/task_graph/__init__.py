"""Task graphs — parse, validate, and create a whole work graph atomically.

The authored format lives in docs/specs/design/supervisor-agent.md §8: a
JSON/YAML document passed to ``aq task create --graph``, or a fenced
``aq-graph`` block inside a vault spec passed to ``--from-spec``.

The pipeline is deliberately three separable steps:

    parse_graph / extract_graph_from_spec  →  validate_graph  →  create_graph

so ``--dry-run`` can stop after validation and report exactly what a real run
would insert, and so nothing about a graph is ever "interpreted" — every
decision is a mechanical check (design §8.3).
"""

from typing import TYPE_CHECKING

from src.task_graph.models import (
    GraphContext,
    GraphError,
    GraphNeed,
    GraphNode,
    GraphParent,
    GraphParseError,
    GraphPhase,
    GraphSubtask,
    TaskGraph,
)
if TYPE_CHECKING:
    from src.task_graph.creator import assign_child_ids, build_report, create_graph
    from src.task_graph.parser import (
        GRAPH_FENCE_LANG,
        extract_graph_block,
        extract_graph_from_spec,
        parse_graph,
    )
    from src.task_graph.validator import (
        BLOCKING_DEP_TYPES,
        split_findings,
        substitute_vars,
        validate_graph,
    )


def __getattr__(name: str):
    # Layout imports need only their own helpers, not graph authoring services.
    if name in {"assign_child_ids", "build_report", "create_graph"}:
        from src.task_graph.creator import assign_child_ids, build_report, create_graph

        exports = {
            "assign_child_ids": assign_child_ids,
            "build_report": build_report,
            "create_graph": create_graph,
        }
    elif name in {"GRAPH_FENCE_LANG", "extract_graph_block", "extract_graph_from_spec", "parse_graph"}:
        from src.task_graph.parser import (
            GRAPH_FENCE_LANG,
            extract_graph_block,
            extract_graph_from_spec,
            parse_graph,
        )

        exports = {
            "GRAPH_FENCE_LANG": GRAPH_FENCE_LANG,
            "extract_graph_block": extract_graph_block,
            "extract_graph_from_spec": extract_graph_from_spec,
            "parse_graph": parse_graph,
        }
    elif name in {"BLOCKING_DEP_TYPES", "split_findings", "substitute_vars", "validate_graph"}:
        from src.task_graph.validator import (
            BLOCKING_DEP_TYPES,
            split_findings,
            substitute_vars,
            validate_graph,
        )

        exports = {
            "BLOCKING_DEP_TYPES": BLOCKING_DEP_TYPES,
            "split_findings": split_findings,
            "substitute_vars": substitute_vars,
            "validate_graph": validate_graph,
        }
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    globals().update(exports)
    return exports[name]


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))

__all__ = [
    "BLOCKING_DEP_TYPES",
    "GRAPH_FENCE_LANG",
    "GraphContext",
    "GraphError",
    "GraphNeed",
    "GraphNode",
    "GraphParent",
    "GraphParseError",
    "GraphPhase",
    "GraphSubtask",
    "TaskGraph",
    "assign_child_ids",
    "build_report",
    "create_graph",
    "extract_graph_block",
    "extract_graph_from_spec",
    "parse_graph",
    "split_findings",
    "substitute_vars",
    "validate_graph",
]
