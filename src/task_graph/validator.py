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

from src.aq_uri import path_is_within
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

#: A project-scoped profile id, ``project:<pid>:<agent-type>`` (see
#: ``src/profiles/``).  The scope is captured so a graph can be stopped from
#: borrowing another project's override.
_SCOPED_PROFILE_RE = re.compile(r"^project:([^:]+):(.+)$")

#: Markdown ATX heading, e.g. ``## 3. Schema``.
_HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})[ \t]+(?P<text>.+?)[ \t]*#*[ \t]*$", re.MULTILINE)


def _normalise_heading(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _error(rule: str, detail: str, node: str | None = None, severity: str = "error") -> GraphError:
    return GraphError(rule=rule, detail=detail, node=node, severity=severity)


def _profile_scope(profile_id: str) -> str | None:
    """Project id a profile reference is scoped to, or ``None`` if unqualified."""
    match = _SCOPED_PROFILE_RE.match(profile_id or "")
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# Variable substitution
# ---------------------------------------------------------------------------


#: How many times a single string is re-scanned for ``{var}`` references.
#: A var whose *value* is itself a reference (``{a: "{b}", b: "boom"}``) needs
#: more than one pass; the bound keeps a self-referential var from looping.
_MAX_VAR_PASSES = 8


def substitute_vars(graph: TaskGraph) -> tuple[set[str], set[str]]:
    """Expand ``{var}`` references across the graph, **in place**.

    ``{spec}`` is implicit: it resolves to the graph's ``spec`` path so a
    spec-authored graph never repeats its own path (design §8.2).

    Expansion runs to a fixed point (bounded by :data:`_MAX_VAR_PASSES`), so a
    var whose value references another var resolves fully and *both* names
    count as used.  A single pass would leave the inner ``{b}`` literal in the
    output, report no ``unknown_var``, and then flag ``b`` as ``unused_var``.

    Returns ``(used, unknown)`` — the declared var names that were actually
    referenced, and the referenced names with no value (plus any name still
    unresolved after the pass bound, i.e. a reference cycle).  Running this
    twice is harmless: after the first call every resolvable reference is gone.
    """
    values: dict[str, str] = dict(graph.vars)
    if graph.spec and "spec" not in values:
        values["spec"] = graph.spec

    used: set[str] = set()
    unknown: set[str] = set()

    def repl(match: re.Match) -> str:
        name = match.group(1)
        if name in values:
            used.add(name)
            return values[name]
        unknown.add(name)
        return match.group(0)

    def expand(text: str | None) -> str | None:
        if not text or "{" not in text:
            return text
        for _ in range(_MAX_VAR_PASSES):
            expanded = _VAR_RE.sub(repl, text)
            if expanded == text:
                return text
            text = expanded
        return text

    if graph.parent is not None:
        graph.parent.title = expand(graph.parent.title) or ""
        graph.parent.description = expand(graph.parent.description) or ""
        graph.parent.labels = [expand(v) or "" for v in graph.parent.labels]
        # parent.profile feeds the same unknown_profile check as node.profile;
        # leaving it unexpanded rejected a correct document with a bogus
        # `unknown_profile '{p}'` *and* a bogus `unused_var 'p'`.
        graph.parent.profile = expand(graph.parent.profile)

    for node in graph.nodes:
        node.title = expand(node.title) or ""
        node.description = expand(node.description) or ""
        node.acceptance = [expand(v) or "" for v in node.acceptance]
        node.labels = [expand(v) or "" for v in node.labels]
        node.profile = expand(node.profile)
        node.intelligence_class = expand(node.intelligence_class)
        node.task_type = expand(node.task_type)
        for ctx in node.context:
            ctx.type = expand(ctx.type) or ctx.type
            ctx.path = expand(ctx.path)
            ctx.section = expand(ctx.section)
            ctx.label = expand(ctx.label)
            ctx.content = expand(ctx.content)
        for need in node.needs:
            need.on = expand(need.on) or need.on

    unknown |= _surviving_var_names(graph)
    return used, unknown


def _surviving_var_names(graph: TaskGraph) -> set[str]:
    """Every ``{name}`` still present after substitution.

    A survivor is by definition unresolvable — either undeclared (already in
    ``unknown``, deduped by the set union) or part of a reference cycle that
    the pass bound gave up on.  Either way the author must hear about it: the
    alternative is a task created with a literal ``{b}`` in its title.
    """
    names: set[str] = set()

    def scan(text: str | None) -> None:
        if text and "{" in text:
            names.update(m.group(1) for m in _VAR_RE.finditer(text))

    if graph.parent is not None:
        scan(graph.parent.title)
        scan(graph.parent.description)
        scan(graph.parent.profile)
        for label in graph.parent.labels:
            scan(label)

    for node in graph.nodes:
        scan(node.title)
        scan(node.description)
        scan(node.profile)
        scan(node.intelligence_class)
        scan(node.task_type)
        for value in list(node.acceptance) + list(node.labels):
            scan(value)
        for ctx in node.context:
            scan(ctx.type)
            scan(ctx.path)
            scan(ctx.section)
            scan(ctx.label)
            scan(ctx.content)
        for need in node.needs:
            scan(need.on)

    return names


# ---------------------------------------------------------------------------
# spec_ref resolution
# ---------------------------------------------------------------------------


def resolve_spec_path_checked(
    path: str, *, vault_root: str | None, source_path: str | None
) -> tuple[str | None, str | None]:
    """Resolve a ``spec_ref`` path to a file **inside the vault**.

    Returns ``(resolved, reason)``.  ``reason`` is ``None`` on success,
    ``"outside_vault"`` when no candidate stays inside *vault_root* (decided
    before existence, so a traversal attempt reads as one whether or not the
    target happens to exist), and ``"not_found"`` when a contained candidate
    was possible but no such file is there.

    Containment is the security boundary here: graph documents are authored
    by an LLM from spec text that may itself be attacker-influenced, and a
    resolved path is later **inlined into another agent's prompt** by
    ``src/prime/sections._render_spec_ref``.  So ``../``, an absolute path
    outside the vault, and a symlink pointing out of the vault are all
    rejected — not merely "not found", which would hide the attempt.

    Accepted forms: a vault-root-relative path (``projects/<pid>/specs/x.md``,
    with or without a leading ``vault/``), a path relative to the spec that
    references it, or an absolute path that still lands inside the vault.
    """
    if not path:
        return None, "not_found"
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

    # Containment is decided *before* existence, so a traversal attempt is
    # reported as one whether or not the target happens to exist right now.
    # No vault to contain against (a bare `--graph` document in a harness)
    # falls back to the working directory rather than trusting the path.
    root = vault_root or os.getcwd()
    contained = [c for c in candidates if path_is_within(c, root)]
    if not contained:
        return None, "outside_vault"

    for candidate in contained:
        if os.path.isfile(candidate):
            return candidate, None
    return None, "not_found"


def resolve_spec_path(path: str, *, vault_root: str | None, source_path: str | None) -> str | None:
    """Containment-enforcing resolution; ``None`` when unusable.

    Thin wrapper over :func:`resolve_spec_path_checked` for callers that
    don't need to distinguish "missing" from "outside the vault".
    """
    return resolve_spec_path_checked(path, vault_root=vault_root, source_path=source_path)[0]


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


def _check_self_edges(graph: TaskGraph) -> list[GraphError]:
    """A node may not depend on itself — **whatever** the dep type.

    ``_check_cycles`` only walks blocking edges, so a non-blocking self-edge
    (``needs: [{on: "a", dep_type: "related"}]`` on node ``a``) slipped past
    validation and died at insert against
    ``CheckConstraint("task_id != depends_on_task_id")`` — an error naming a
    table the graph author never saw.
    """
    return [
        _error(
            "self_edge",
            f"node '{node.key}' declares a '{need.dep_type}' dependency on itself",
            node.key,
        )
        for node in graph.nodes
        for need in node.needs
        if need.on == node.key
    ]


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
    """Every referenced profile must exist.

    Profiles are global — a durable worker is shared between projects — so a
    reference is just the agent-type name.  A retired ``project:<pid>:<name>``
    reference is reported rather than resolved: the override it named no
    longer exists, and silently falling back to the system profile would hide
    a graph that still encodes the old scoping.
    """
    if db is None:
        return []
    errors: list[GraphError] = []
    cache: dict[str, str | None] = {}

    async def resolve(profile_id: str) -> str | None:
        """Resolve to the profile id that actually exists, or ``None``."""
        if profile_id not in cache:
            cache[profile_id] = (
                profile_id if await db.get_profile(profile_id) is not None else None
            )
        return cache[profile_id]

    def report(profile_id: str, node_key: str | None) -> None:
        errors.append(
            _error(
                "unknown_profile",
                f"profile '{profile_id}' is not defined for project '{project_id}'",
                node_key,
            )
        )

    def report_retired(profile_id: str, scope: str, node_key: str | None) -> None:
        errors.append(
            _error(
                "retired_project_profile",
                f"profile '{profile_id}' uses the retired project-scoped form "
                f"(scope '{scope}') — project-scoped profiles were removed; "
                "reference the agent-type by name",
                node_key,
            )
        )

    async def check(profile_id: str, node_key: str | None) -> str | None:
        scope = _profile_scope(profile_id)
        if scope is not None:
            report_retired(profile_id, scope, node_key)
            return None
        resolved = await resolve(profile_id)
        if resolved is None:
            report(profile_id, node_key)
        return resolved

    if graph.parent and graph.parent.profile:
        resolved = await check(graph.parent.profile, None)
        if resolved is not None:
            graph.parent.profile = resolved

    for node in graph.nodes:
        if not node.profile:
            continue
        resolved = await check(node.profile, node.key)
        if resolved is not None:
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
            resolved, reason = resolve_spec_path_checked(
                path, vault_root=vault_root, source_path=graph.source_path
            )
            if reason == "outside_vault":
                # Always an error, never a warning: this is a containment
                # violation, not a typo, and the resolved file would have
                # been inlined verbatim into an agent's prompt.
                errors.append(
                    _error(
                        "spec_ref_outside_vault",
                        f"spec_ref path '{path}' resolves outside the vault — "
                        "spec references must stay inside the vault root",
                        node.key,
                    )
                )
                continue
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
    findings.extend(_check_self_edges(graph))
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
    "resolve_spec_path_checked",
    "spec_has_section",
    "split_findings",
    "substitute_vars",
    "validate_graph",
]
