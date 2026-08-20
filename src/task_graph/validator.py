"""Deterministic graph validation — the design §8.3 rule table.

The daemon never "interprets" a graph: every rule here is a mechanical check
over rows, files and the dep-type registry.  Errors block creation; warnings
are reported and the graph is created anyway.

Rule names (``GraphError.rule``) are part of the contract — golden tests and
the ``--dry-run`` report match on them, so renaming one is a breaking change.
"""

from __future__ import annotations

import os
import re
from collections import deque
from typing import Any

from src.database.tables import TASK_DEP_TYPES
from src.task_graph.models import GraphError, GraphNode, TaskGraph

#: Edge kinds that gate readiness — only these can form a forbidden cycle.
#: Mirrors the work-graph spec's registry (docs/specs/design/work-graph.md).
BLOCKING_DEP_TYPES: frozenset[str] = frozenset(
    {"blocks", "parent-child", "waits-for", "conditional-blocks"}
)

#: ``{var}`` reference syntax.  Deliberately narrow so JSON/code braces in a
#: description are not mistaken for variable references.
_VAR_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_.\-]*)\}")

#: Markdown ATX heading, e.g. ``## 3. Schema``.
_HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})[ \t]+(?P<text>.+?)[ \t]*#*[ \t]*$", re.MULTILINE)


def _normalise_heading(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _error(rule: str, detail: str, node: str | None = None, severity: str = "error") -> GraphError:
    return GraphError(rule=rule, detail=detail, node=node, severity=severity)


# ---------------------------------------------------------------------------
# Variable substitution
# ---------------------------------------------------------------------------


def substitute_vars(graph: TaskGraph) -> tuple[set[str], set[str]]:
    """Expand ``{var}`` references across the graph, **in place**.

    ``{spec}`` is implicit: it resolves to the graph's ``spec`` path so a
    spec-authored graph never repeats its own path (design §8.2).

    Returns ``(used, unknown)`` — the declared var names that were actually
    referenced, and the referenced names with no value.  Running this twice is
    harmless: after the first pass every resolvable reference is gone.
    """
    values: dict[str, str] = dict(graph.vars)
    if graph.spec and "spec" not in values:
        values["spec"] = graph.spec

    used: set[str] = set()
    unknown: set[str] = set()

    def expand(text: str | None) -> str | None:
        if not text or "{" not in text:
            return text

        def repl(match: re.Match) -> str:
            name = match.group(1)
            if name in values:
                used.add(name)
                return values[name]
            unknown.add(name)
            return match.group(0)

        return _VAR_RE.sub(repl, text)

    if graph.parent is not None:
        graph.parent.title = expand(graph.parent.title) or ""
        graph.parent.description = expand(graph.parent.description) or ""
        graph.parent.labels = [expand(v) or "" for v in graph.parent.labels]

    for node in graph.nodes:
        node.title = expand(node.title) or ""
        node.description = expand(node.description) or ""
        node.acceptance = [expand(v) or "" for v in node.acceptance]
        node.labels = [expand(v) or "" for v in node.labels]
        node.profile = expand(node.profile)
        node.task_type = expand(node.task_type)
        for ctx in node.context:
            ctx.path = expand(ctx.path)
            ctx.section = expand(ctx.section)
            ctx.label = expand(ctx.label)
            ctx.content = expand(ctx.content)
        for need in node.needs:
            need.on = expand(need.on) or need.on

    return used, unknown


# ---------------------------------------------------------------------------
# spec_ref resolution
# ---------------------------------------------------------------------------


def resolve_spec_path(path: str, *, vault_root: str | None, source_path: str | None) -> str | None:
    """Resolve a ``spec_ref`` path to a file on disk, or ``None``.

    Accepts the three forms a supervisor plausibly writes: an absolute path,
    a vault-root-relative path (``projects/<pid>/specs/x.md``, with or without
    a leading ``vault/``), or a path relative to the spec that references it.
    """
    if not path:
        return None
    candidates: list[str] = []
    if os.path.isabs(path):
        candidates.append(path)
    else:
        if vault_root:
            candidates.append(os.path.join(vault_root, path))
            normalised = path.replace("\\", "/")
            if normalised.startswith("vault/"):
                candidates.append(os.path.join(vault_root, normalised[len("vault/") :]))
        if source_path:
            candidates.append(os.path.join(os.path.dirname(source_path), path))
        candidates.append(path)

    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return None


def spec_has_section(spec_file: str, section: str) -> bool:
    """True when *spec_file* contains a markdown heading matching *section*."""
    try:
        with open(spec_file, encoding="utf-8") as handle:
            content = handle.read()
    except OSError:
        return False
    wanted = _normalise_heading(section)
    return any(_normalise_heading(m.group("text")) == wanted for m in _HEADING_RE.finditer(content))


# ---------------------------------------------------------------------------
# Individual rules
# ---------------------------------------------------------------------------


def _check_keys(graph: TaskGraph) -> list[GraphError]:
    errors: list[GraphError] = []
    seen: set[str] = set()
    for node in graph.nodes:
        if node.key in seen:
            errors.append(_error("duplicate_key", f"duplicate node key '{node.key}'", node.key))
        seen.add(node.key)
    return errors


def _check_titles(graph: TaskGraph) -> list[GraphError]:
    return [
        _error("missing_title", f"node '{node.key}' has no title", node.key)
        for node in graph.nodes
        if not node.title.strip()
    ]


def _check_acceptance(graph: TaskGraph) -> list[GraphError]:
    return [
        _error(
            "no_acceptance",
            f"node '{node.key}' has no acceptance criteria",
            node.key,
            severity="warning",
        )
        for node in graph.nodes
        if not [a for a in node.acceptance if a.strip()]
    ]


def _check_dep_types(graph: TaskGraph) -> list[GraphError]:
    errors: list[GraphError] = []
    for node in graph.nodes:
        for need in node.needs:
            if need.dep_type not in TASK_DEP_TYPES:
                errors.append(
                    _error(
                        "bad_dep_type",
                        f"dep_type '{need.dep_type}' is not in the registry "
                        f"({', '.join(TASK_DEP_TYPES)})",
                        node.key,
                    )
                )
    return errors


def _check_cycles(graph: TaskGraph) -> list[GraphError]:
    """Kahn's algorithm over in-graph blocking edges."""
    keys = {node.key for node in graph.nodes}
    indegree: dict[str, int] = {key: 0 for key in keys}
    adjacency: dict[str, list[str]] = {key: [] for key in keys}

    for node in graph.nodes:
        for need in node.needs:
            if need.dep_type not in BLOCKING_DEP_TYPES:
                continue
            if need.on not in keys:
                continue  # external dependency — can't close a cycle in-graph
            adjacency[need.on].append(node.key)
            indegree[node.key] += 1

    queue = deque(sorted(k for k, deg in indegree.items() if deg == 0))
    visited = 0
    while queue:
        current = queue.popleft()
        visited += 1
        for downstream in adjacency[current]:
            indegree[downstream] -= 1
            if indegree[downstream] == 0:
                queue.append(downstream)

    if visited == len(keys):
        return []
    stuck = sorted(k for k, deg in indegree.items() if deg > 0)
    return [
        _error(
            "cycle",
            f"blocking dependency cycle among nodes: {', '.join(stuck)}",
        )
    ]


def _check_foreign_projects(graph: TaskGraph, project_id: str) -> list[GraphError]:
    errors: list[GraphError] = []
    for node in graph.nodes:
        if node.project and node.project != project_id:
            errors.append(
                _error(
                    "foreign_project_node",
                    f"node '{node.key}' targets project '{node.project}' — "
                    f"graphs are single-project (this graph is '{project_id}')",
                    node.key,
                )
            )
    return errors


async def _check_needs(graph: TaskGraph, project_id: str, db: Any) -> list[GraphError]:
    """Resolve every ``needs.on``: graph key, then existing task, else error."""
    errors: list[GraphError] = []
    keys = {node.key for node in graph.nodes}
    for node in graph.nodes:
        for need in node.needs:
            if need.on in keys:
                continue
            task = await db.get_task(need.on) if db is not None else None
            if task is None:
                errors.append(
                    _error(
                        "unresolved_need",
                        f"'{need.on}' is neither a node key in this graph nor an existing task id",
                        node.key,
                    )
                )
                continue
            if getattr(task, "project_id", project_id) != project_id and not need.cross_project:
                errors.append(
                    _error(
                        "cross_project_need",
                        f"'{need.on}' belongs to project "
                        f"'{getattr(task, 'project_id', '?')}' — set cross_project: true "
                        "to declare the edge explicitly",
                        node.key,
                    )
                )
    return errors


async def _check_profiles(graph: TaskGraph, project_id: str, db: Any) -> list[GraphError]:
    """Every referenced profile must exist, project override included.

    Resolution also **rewrites** the reference to the id that actually
    exists: a project override lives under ``project:<pid>:<name>``, and the
    created task's ``profile_id`` FK has to name that row, not the bare
    agent-type the author wrote.
    """
    if db is None:
        return []
    errors: list[GraphError] = []
    cache: dict[str, str | None] = {}

    async def resolve(profile_id: str) -> str | None:
        if profile_id not in cache:
            scoped = f"project:{project_id}:{profile_id}"
            resolved: str | None = None
            if await db.get_profile(scoped) is not None:
                resolved = scoped
            elif await db.get_profile(profile_id) is not None:
                resolved = profile_id
            cache[profile_id] = resolved
        return cache[profile_id]

    def report(profile_id: str, node_key: str | None) -> None:
        errors.append(
            _error(
                "unknown_profile",
                f"profile '{profile_id}' is not defined for project '{project_id}' "
                "(checked the project override and the system profile)",
                node_key,
            )
        )

    if graph.parent and graph.parent.profile:
        resolved = await resolve(graph.parent.profile)
        if resolved is None:
            report(graph.parent.profile, None)
        else:
            graph.parent.profile = resolved

    for node in graph.nodes:
        if not node.profile:
            continue
        resolved = await resolve(node.profile)
        if resolved is None:
            report(node.profile, node.key)
        else:
            node.profile = resolved

    return errors


def _check_spec_refs(
    graph: TaskGraph,
    *,
    vault_root: str | None,
) -> list[GraphError]:
    """Check ``spec_ref`` paths and section headings.

    Severity follows design §8.3: an unresolvable reference is an **error**
    when the graph came from a spec (the author had the file in hand) and a
    **warning** for a standalone ``--graph`` document.
    """
    severity = "error" if graph.from_spec else "warning"
    errors: list[GraphError] = []
    for node in graph.nodes:
        for ctx in node.context:
            if ctx.type != "spec_ref":
                continue
            path = ctx.path or graph.spec
            if not path:
                errors.append(
                    _error(
                        "missing_spec_ref",
                        "spec_ref has no 'path' and the graph declares no 'spec'",
                        node.key,
                        severity,
                    )
                )
                continue
            resolved = resolve_spec_path(
                path, vault_root=vault_root, source_path=graph.source_path
            )
            if resolved is None:
                errors.append(
                    _error(
                        "missing_spec_ref",
                        f"spec_ref path '{path}' does not resolve to a file in the vault",
                        node.key,
                        severity,
                    )
                )
                continue
            if ctx.section and not spec_has_section(resolved, ctx.section):
                errors.append(
                    _error(
                        "missing_spec_section",
                        f"spec '{path}' has no heading matching '{ctx.section}'",
                        node.key,
                        severity,
                    )
                )
    return errors


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def validate_graph(
    graph: TaskGraph,
    *,
    project_id: str,
    db: Any,
    vault_root: str | None = None,
) -> list[GraphError]:
    """Validate *graph* for *project_id*, returning every finding.

    Substitutes ``{var}`` references **in place** first, then applies the
    §8.3 rule table.  The returned list mixes errors and warnings; callers
    split on :attr:`GraphError.is_error`.
    """
    findings: list[GraphError] = []

    used, unknown = substitute_vars(graph)
    for name in sorted(unknown):
        findings.append(_error("unknown_var", f"reference to undeclared var '{{{name}}}'"))
    for name in sorted(set(graph.vars) - used):
        findings.append(
            _error("unused_var", f"declared var '{name}' is never referenced", severity="warning")
        )

    findings.extend(_check_keys(graph))
    findings.extend(_check_titles(graph))
    findings.extend(_check_acceptance(graph))
    findings.extend(_check_dep_types(graph))
    findings.extend(_check_cycles(graph))
    findings.extend(_check_foreign_projects(graph, project_id))
    findings.extend(await _check_needs(graph, project_id, db))
    findings.extend(await _check_profiles(graph, project_id, db))
    findings.extend(_check_spec_refs(graph, vault_root=vault_root))

    return findings


def split_findings(findings: list[GraphError]) -> tuple[list[GraphError], list[GraphError]]:
    """Split findings into ``(errors, warnings)``."""
    return (
        [f for f in findings if f.is_error],
        [f for f in findings if not f.is_error],
    )


__all__ = [
    "BLOCKING_DEP_TYPES",
    "GraphNode",
    "resolve_spec_path",
    "spec_has_section",
    "split_findings",
    "substitute_vars",
    "validate_graph",
]
