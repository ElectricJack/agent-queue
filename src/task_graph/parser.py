"""Graph document parsing — JSON/YAML documents and fenced ``aq-graph`` blocks.

Implements docs/specs/implementation/supervisor-agent.md §8's
:func:`parse_graph` / :func:`extract_graph_from_spec`.

Parsing is *structural* only: it decides what the document says, never
whether it is coherent.  Everything semantic — vars, cycles, profiles, dep
types, spec sections — belongs to :mod:`src.task_graph.validator`, so a
caller can always parse first and report every semantic problem at once
instead of dying on the first one.
"""

from __future__ import annotations

import json
import re
from typing import Any

import yaml

from src.database.queries.task_subtask_queries import (
    MAX_SUBTASK_CONTEXT,
    MAX_SUBTASK_TITLE,
    MAX_SUBTASKS_PER_CALL,
)
from src.task_graph.models import (
    DEFAULT_DEP_TYPE,
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

#: The fence language that marks a spec's graph block (design §8.1).
GRAPH_FENCE_LANG = "aq-graph"

#: Hard ceiling on a graph document, in characters.  5000 nodes parse to well
#: under this; the cap exists to stop a pathological document (deep nesting,
#: expansion bombs) reaching the JSON/YAML parsers at all.  Rejected as a
#: normal structural finding, not an exception the caller has to guess at.
MAX_GRAPH_DOCUMENT_CHARS = 2_000_000

_FENCE_RE = re.compile(
    r"^(?P<fence>`{3,}|~{3,})[ \t]*" + GRAPH_FENCE_LANG + r"[ \t]*\r?$",
    re.MULTILINE,
)


def _err(rule: str, detail: str, node: str | None = None) -> GraphError:
    return GraphError(rule=rule, detail=detail, node=node)


def _as_str_list(value: Any, field_name: str, node_key: str | None) -> tuple[list[str], list[GraphError]]:
    if value is None:
        return [], []
    if isinstance(value, str):
        return [value], []
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value), []
    return [], [
        _err(
            "bad_field_type",
            f"'{field_name}' must be a string or list of strings, got {type(value).__name__}",
            node_key,
        )
    ]


def _parse_need(raw: Any, node_key: str) -> tuple[GraphNeed | None, list[GraphError]]:
    """Parse one ``needs`` entry: a shorthand string or an object."""
    if isinstance(raw, str):
        if not raw.strip():
            return None, [_err("bad_need", "'needs' entry must not be empty", node_key)]
        return GraphNeed(on=raw.strip()), []
    if isinstance(raw, dict):
        on = raw.get("on")
        if not isinstance(on, str) or not on.strip():
            return None, [_err("bad_need", f"'needs' entry is missing 'on': {raw!r}", node_key)]
        dep_type = raw.get("dep_type", DEFAULT_DEP_TYPE)
        if not isinstance(dep_type, str) or not dep_type.strip():
            return None, [
                _err("bad_need", f"'needs.dep_type' must be a string, got {dep_type!r}", node_key)
            ]
        return (
            GraphNeed(
                on=on.strip(),
                dep_type=dep_type.strip(),
                cross_project=bool(raw.get("cross_project", False)),
            ),
            [],
        )
    return None, [
        _err("bad_need", f"'needs' entries must be strings or objects, got {raw!r}", node_key)
    ]


def _parse_context(raw: Any, node_key: str) -> tuple[GraphContext | None, list[GraphError]]:
    if not isinstance(raw, dict):
        return None, [
            _err("bad_context", f"'context' entries must be objects, got {raw!r}", node_key)
        ]
    ctx_type = raw.get("type")
    if not isinstance(ctx_type, str) or not ctx_type.strip():
        return None, [_err("bad_context", f"'context' entry is missing 'type': {raw!r}", node_key)]
    return (
        GraphContext(
            type=ctx_type.strip(),
            path=raw.get("path"),
            section=raw.get("section"),
            label=raw.get("label"),
            content=raw.get("content"),
        ),
        [],
    )


def _parse_subtask(raw: Any, node_key: str) -> tuple[GraphSubtask | None, list[GraphError]]:
    """Parse one ``subtasks`` entry: a bare title string or ``{title, context}``.

    Length bounds are checked here, before anything is written, because they
    are ``CheckConstraint``s on ``task_subtasks`` — an oversize title reaching
    the insert would abort ``write_plan``'s whole transaction with a database
    error instead of a reportable graph finding.
    """
    if isinstance(raw, str):
        raw = {"title": raw}
    if not isinstance(raw, dict):
        return None, [
            _err(
                "bad_subtask",
                f"'subtasks' entries must be strings or objects, got {raw!r}",
                node_key,
            )
        ]
    title = raw.get("title")
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= MAX_SUBTASK_TITLE:
        return None, [
            _err(
                "bad_subtask",
                f"each subtask title must be a string of 1 to {MAX_SUBTASK_TITLE} characters, "
                f"got {title!r}",
                node_key,
            )
        ]
    # Only an omitted key or an explicit null means "no context" — a bare
    # YAML ``context:`` is the one way to write "unset", the same reading
    # ``src/task_graph/formulas.py`` gives a null.  Everything else must pass
    # the type check: coercing with ``or ""`` would have quietly accepted
    # ``0``, ``False`` and ``[]`` while rejecting ``5``.
    context = raw.get("context")
    if context is None:
        context = ""
    if not isinstance(context, str) or len(context) > MAX_SUBTASK_CONTEXT:
        return None, [
            _err(
                "bad_subtask",
                f"subtask context must be a string of at most {MAX_SUBTASK_CONTEXT} "
                f"characters, got {context!r}",
                node_key,
            )
        ]
    return GraphSubtask(title=title.strip(), context=context), []


def _parse_node(raw: Any, index: int, defaults: dict) -> tuple[GraphNode | None, list[GraphError]]:
    errors: list[GraphError] = []
    if not isinstance(raw, dict):
        return None, [_err("bad_node", f"node #{index} must be an object, got {type(raw).__name__}")]

    key = raw.get("key")
    if not isinstance(key, str) or not key.strip():
        return None, [_err("missing_key", f"node #{index} is missing a 'key'")]
    key = key.strip()

    node = GraphNode(key=key)
    node.title = raw.get("title", defaults.get("title", "")) or ""
    node.description = raw.get("description", defaults.get("description", "")) or ""

    acceptance, errs = _as_str_list(
        raw.get("acceptance", defaults.get("acceptance")), "acceptance", key
    )
    node.acceptance = acceptance
    errors.extend(errs)

    from src.commands.task_commands import normalize_deliverables

    deliverables, deliverables_error = normalize_deliverables(
        raw.get("deliverables", defaults.get("deliverables"))
    )
    if deliverables_error:
        errors.append(_err("bad_deliverable", deliverables_error, key))
    else:
        node.deliverables = deliverables

    labels, errs = _as_str_list(raw.get("labels", defaults.get("labels")), "labels", key)
    node.labels = labels
    errors.extend(errs)

    priority = raw.get("priority", defaults.get("priority", 100))
    if isinstance(priority, bool) or not isinstance(priority, int):
        errors.append(_err("bad_field_type", f"'priority' must be an integer, got {priority!r}", key))
    else:
        node.priority = priority

    profile = raw.get("profile", defaults.get("profile"))
    if profile is not None and not isinstance(profile, str):
        errors.append(_err("bad_field_type", f"'profile' must be a string, got {profile!r}", key))
    else:
        node.profile = profile
        if profile is not None:
            node.profile_source = "document"

    # ``pin: true`` makes the profile a pinned provider (provider-failover D9).
    # Whether the node has a profile to pin is checked after ``create_task_graph``
    # fills one in (``validate_graph``'s ``pin_without_profile``).
    pin = raw.get("pin", defaults.get("pin", False))
    if pin is None:
        pin = False
    if not isinstance(pin, bool):
        errors.append(_err("bad_field_type", f"'pin' must be true or false, got {pin!r}", key))
    else:
        node.pin = pin

    intelligence_class = raw.get("intelligence_class", defaults.get("intelligence_class"))
    if intelligence_class is not None and (
        not isinstance(intelligence_class, str) or not intelligence_class.strip()
    ):
        errors.append(_err("bad_field_type", "'intelligence_class' must be a nonempty string", key))
    else:
        node.intelligence_class = intelligence_class

    # Worker affinity and model/provider pins are not graph fields. Report
    # attempted routing instead of accepting it and discarding user intent.
    for unsupported in ("affinity_agent_id", "agent_id", "assigned_agent_id", "model", "provider", "harness"):
        if unsupported in raw or unsupported in defaults:
            errors.append(_err(
                "unsupported_routing", f"'{unsupported}' is not supported in graphs; use profile and intelligence_class", key,
            ))

    task_type = raw.get("task_type", defaults.get("task_type"))
    if task_type is not None and not isinstance(task_type, str):
        errors.append(
            _err("bad_field_type", f"'task_type' must be a string, got {task_type!r}", key)
        )
    else:
        node.task_type = task_type

    phase = raw.get("phase", defaults.get("phase"))
    if phase is not None and (not isinstance(phase, str) or not phase.strip()):
        errors.append(
            _err("bad_field_type", f"'phase' must be a nonempty string, got {phase!r}", key)
        )
    elif isinstance(phase, str):
        node.phase = phase.strip()

    project = raw.get("project", raw.get("project_id"))
    if project is not None and not isinstance(project, str):
        errors.append(_err("bad_field_type", f"'project' must be a string, got {project!r}", key))
    else:
        node.project = project

    raw_needs = raw.get("needs", defaults.get("needs")) or []
    if isinstance(raw_needs, (str, dict)):
        raw_needs = [raw_needs]
    if not isinstance(raw_needs, list):
        errors.append(_err("bad_need", f"'needs' must be a list, got {type(raw_needs).__name__}", key))
        raw_needs = []
    for entry in raw_needs:
        need, errs = _parse_need(entry, key)
        errors.extend(errs)
        if need:
            node.needs.append(need)

    raw_context = raw.get("context", defaults.get("context")) or []
    if isinstance(raw_context, dict):
        raw_context = [raw_context]
    if not isinstance(raw_context, list):
        errors.append(
            _err("bad_context", f"'context' must be a list, got {type(raw_context).__name__}", key)
        )
        raw_context = []
    for entry in raw_context:
        ctx, errs = _parse_context(entry, key)
        errors.extend(errs)
        if ctx:
            node.context.append(ctx)

    raw_subtasks = raw.get("subtasks", defaults.get("subtasks")) or []
    if isinstance(raw_subtasks, (str, dict)):
        raw_subtasks = [raw_subtasks]
    if not isinstance(raw_subtasks, list):
        errors.append(
            _err(
                "bad_subtask",
                f"'subtasks' must be a list, got {type(raw_subtasks).__name__}",
                key,
            )
        )
        raw_subtasks = []
    if len(raw_subtasks) > MAX_SUBTASKS_PER_CALL:
        # One node's list is one authoring act, so it is capped the way one
        # ``task_subtask_add`` call is.  The durable per-task ceiling
        # (``MAX_SUBTASKS_PER_TASK``) is unreachable from a graph alone — the
        # task is brand new and the base ordinal is 0 — and stays what
        # ``add_task_subtasks`` raises for a later ``subtask-add``.
        errors.append(
            _err(
                "bad_subtask",
                f"node has {len(raw_subtasks)} subtasks; at most {MAX_SUBTASKS_PER_CALL} "
                "may be declared on one node",
                key,
            )
        )
        raw_subtasks = []
    for entry in raw_subtasks:
        subtask, errs = _parse_subtask(entry, key)
        errors.extend(errs)
        if subtask:
            node.subtasks.append(subtask)

    return node, errors


def _parse_phase(raw: Any, index: int) -> tuple[GraphPhase | None, list[GraphError]]:
    """Parse one ``phases`` entry: ``{key, title, label?}``.

    Unlike a node there is no shorthand form — a phase is written out, because
    its ``key`` is what every node in it references and a bare string would
    have to serve as both key and title.
    """
    if not isinstance(raw, dict):
        return None, [
            _err("bad_phase", f"phase #{index} must be an object, got {type(raw).__name__}")
        ]
    key = raw.get("key")
    if not isinstance(key, str) or not key.strip():
        return None, [_err("missing_phase_key", f"phase #{index} is missing a 'key'")]
    key = key.strip()
    title = raw.get("title", "") or ""
    if not isinstance(title, str):
        return None, [_err("bad_phase", f"'phases.title' must be a string, got {title!r}")]
    label = raw.get("label")
    if label is not None and not isinstance(label, str):
        return None, [_err("bad_phase", f"'phases.label' must be a string, got {label!r}")]
    return GraphPhase(key=key, title=title, label=label), []


def _parse_parent(raw: Any) -> tuple[GraphParent | None, list[GraphError]]:
    if raw is None:
        return None, []
    if isinstance(raw, str):
        return GraphParent(title=raw), []
    if not isinstance(raw, dict):
        return None, [_err("bad_parent", f"'parent' must be an object, got {type(raw).__name__}")]
    errors: list[GraphError] = []
    labels, errs = _as_str_list(raw.get("labels"), "parent.labels", None)
    errors.extend(errs)
    priority = raw.get("priority", 100)
    if isinstance(priority, bool) or not isinstance(priority, int):
        errors.append(_err("bad_field_type", f"'parent.priority' must be an integer, got {priority!r}"))
        priority = 100
    return (
        GraphParent(
            title=raw.get("title", "") or "",
            description=raw.get("description", "") or "",
            profile=raw.get("profile"),
            labels=labels,
            priority=priority,
        ),
        errors,
    )


def _load_document(source: str, fmt: str) -> dict:
    """Turn text into a mapping, trying JSON then YAML for ``fmt="auto"``."""
    text = source.strip()
    if not text:
        raise GraphParseError([_err("empty_document", "graph document is empty")])

    if len(text) > MAX_GRAPH_DOCUMENT_CHARS:
        raise GraphParseError(
            [
                _err(
                    "document_too_large",
                    f"graph document is {len(text)} characters, over the "
                    f"{MAX_GRAPH_DOCUMENT_CHARS} limit",
                )
            ]
        )

    attempts: list[str] = {"auto": ["json", "yaml"], "json": ["json"], "yaml": ["yaml"]}.get(
        fmt, []
    )
    if not attempts:
        raise GraphParseError(
            [_err("bad_format", f"unknown fmt '{fmt}' (expected auto, json, or yaml)")]
        )

    last_detail = ""
    for attempt in attempts:
        try:
            if attempt == "json":
                data = json.loads(text)
            else:
                data = yaml.safe_load(text)
        except (json.JSONDecodeError, yaml.YAMLError) as exc:
            last_detail = str(exc)
            continue
        except RecursionError:
            # Both parsers recurse on nesting depth; `"["*4000 + "]"*4000` is
            # under the size cap and still blows the stack.  A RecursionError
            # escaping parse_graph reaches the CLI (`--graph -`) with no
            # containing net at all, so it becomes a structural finding here.
            last_detail = "document nesting is too deep to parse"
            continue
        if isinstance(data, dict):
            return data
        last_detail = f"top level must be an object, got {type(data).__name__}"
    raise GraphParseError([_err("bad_document", f"could not parse graph document: {last_detail}")])


def parse_graph(source: str | dict, *, fmt: str = "auto") -> TaskGraph:
    """Parse a standalone graph document.

    *source* is either a mapping (already decoded) or JSON/YAML text.  Raises
    :class:`~src.task_graph.models.GraphParseError` carrying every structural
    finding — malformed nodes are collected, not fatal one at a time.
    """
    data = source if isinstance(source, dict) else _load_document(str(source), fmt)

    errors: list[GraphError] = []

    version = data.get("version", 1)
    if isinstance(version, bool) or not isinstance(version, int):
        errors.append(_err("bad_version", f"'version' must be an integer, got {version!r}"))
        version = 1
    elif version != 1:
        errors.append(_err("bad_version", f"unsupported graph version {version} (this build reads 1)"))

    raw_vars = data.get("vars") or {}
    graph_vars: dict[str, str] = {}
    if not isinstance(raw_vars, dict):
        errors.append(_err("bad_vars", f"'vars' must be an object, got {type(raw_vars).__name__}"))
    else:
        for name, value in raw_vars.items():
            if not isinstance(name, str):
                errors.append(_err("bad_vars", f"var names must be strings, got {name!r}"))
                continue
            graph_vars[name] = "" if value is None else str(value)

    defaults = data.get("defaults") or {}
    if not isinstance(defaults, dict):
        errors.append(
            _err("bad_defaults", f"'defaults' must be an object, got {type(defaults).__name__}")
        )
        defaults = {}

    parent, parent_errors = _parse_parent(data.get("parent"))
    errors.extend(parent_errors)

    raw_phases = data.get("phases")
    phases: list[GraphPhase] = []
    if raw_phases is not None:
        if not isinstance(raw_phases, list):
            errors.append(
                _err("bad_phase", f"'phases' must be a list, got {type(raw_phases).__name__}")
            )
        else:
            for index, raw_phase in enumerate(raw_phases):
                phase, phase_errors = _parse_phase(raw_phase, index)
                errors.extend(phase_errors)
                if phase:
                    phases.append(phase)

    raw_nodes = data.get("nodes")
    if raw_nodes is None:
        errors.append(_err("no_nodes", "graph has no 'nodes'"))
        raw_nodes = []
    elif not isinstance(raw_nodes, list):
        errors.append(_err("no_nodes", f"'nodes' must be a list, got {type(raw_nodes).__name__}"))
        raw_nodes = []
    elif not raw_nodes:
        errors.append(_err("no_nodes", "graph has no nodes"))

    nodes: list[GraphNode] = []
    for index, raw_node in enumerate(raw_nodes):
        node, node_errors = _parse_node(raw_node, index, defaults)
        errors.extend(node_errors)
        if node:
            nodes.append(node)

    spec = data.get("spec")
    if spec is not None and not isinstance(spec, str):
        errors.append(_err("bad_field_type", f"'spec' must be a string, got {spec!r}"))
        spec = None

    if errors:
        raise GraphParseError(errors)

    return TaskGraph(
        version=version,
        spec=spec,
        vars=graph_vars,
        defaults=defaults,
        parent=parent,
        phases=phases,
        nodes=nodes,
    )


def extract_graph_block(markdown: str) -> str | None:
    """Return the body of the first fenced ``aq-graph`` block, or ``None``."""
    match = _FENCE_RE.search(markdown)
    if not match:
        return None
    fence = match.group("fence")
    body_start = match.end()
    close_re = re.compile(r"^" + re.escape(fence[0]) + r"{" + str(len(fence)) + r",}[ \t]*\r?$", re.MULTILINE)
    close = close_re.search(markdown, body_start)
    body = markdown[body_start : close.start()] if close else markdown[body_start:]
    return body.strip("\n")


def extract_graph_from_spec(markdown: str, spec_path: str) -> TaskGraph:
    """Parse the fenced ``aq-graph`` block out of a vault spec.

    ``spec`` is implied from *spec_path* when the block doesn't set it, which
    is what makes ``{spec}`` usable in a spec-authored graph without repeating
    the path (design §8.2).
    """
    body = extract_graph_block(markdown)
    if body is None:
        raise GraphParseError(
            [
                _err(
                    "no_graph_block",
                    f"{spec_path} has no fenced ```{GRAPH_FENCE_LANG} block",
                )
            ]
        )

    graph = parse_graph(body, fmt="auto")
    graph.from_spec = True
    graph.source_path = spec_path
    if not graph.spec:
        graph.spec = spec_path
    return graph
