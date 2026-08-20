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

from src.task_graph.models import (
    DEFAULT_DEP_TYPE,
    GraphContext,
    GraphError,
    GraphNeed,
    GraphNode,
    GraphParent,
    GraphParseError,
    TaskGraph,
)

#: The fence language that marks a spec's graph block (design §8.1).
GRAPH_FENCE_LANG = "aq-graph"

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

    task_type = raw.get("task_type", defaults.get("task_type"))
    if task_type is not None and not isinstance(task_type, str):
        errors.append(
            _err("bad_field_type", f"'task_type' must be a string, got {task_type!r}", key)
        )
    else:
        node.task_type = task_type

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

    return node, errors


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
