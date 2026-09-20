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
from typing import Any, NamedTuple

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

    for phase in graph.phases:
        # The key is graph-local plumbing, like ``node.key``, and is left
        # alone; the title and label are what a reader sees.
        phase.title = expand(phase.title) or ""
        phase.label = expand(phase.label)

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
        for subtask in node.subtasks:
            subtask.title = expand(subtask.title) or subtask.title
            subtask.context = expand(subtask.context) or subtask.context

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

    for phase in graph.phases:
        scan(phase.title)
        scan(phase.label)

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
        for subtask in node.subtasks:
            scan(subtask.title)
            scan(subtask.context)

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


def _check_phases(graph: TaskGraph) -> list[GraphError]:
    """The ``phases:``/``phase:`` rules (planning-emits-phases §7.3).

    Three errors — a duplicate phase key, a node naming a phase that was
    never declared, and a **phase-inverted** gating path — and two warnings.
    The warnings are warnings on purpose: an empty phase and a
    belt-and-braces backward edge are both *legal*, and whether they are
    wanted is the planner's judgement, not the validator's (§3.5).

    **The cross-phase rule.** Only *gating* edges count
    (:data:`BLOCKING_DEP_TYPES`); ``related``/``discovered-from`` and friends
    schedule nothing, so they can neither deadlock nor be redundant.

    - Backward, **per edge**: a phased node needing a node in an earlier
      phase is ``redundant_phase_edge`` (warning) — the phase gate already
      orders them.
    - Within one phase: nothing.  That is ordinary ordering inside a stage.
    - Forward, **transitively**: if a gating path from a phased node reaches
      a phased node in a *later* phase — directly, or through any number of
      **unphased** intermediates — it is ``inverted_phase_edge``.

    The forward rule has to be transitive, and the earlier per-edge version
    of it was unsound.  An unphased node is a direct child of the epic, so no
    phase *withholds* it through the ``parent-child`` rule — but it is still
    gated by its own edges.  With ``A`` in phase 1 needing unphased ``U``
    needing ``B`` in phase 2: ``B`` is withheld under phase 2 (DEFINED,
    because its ``blocks`` edge onto phase 1 is unsatisfied), so ``U``'s edge
    never satisfies, so ``A`` never completes, so phase 1 never settles and
    phase 2 never releases.  A permanent deadlock, and one neither
    :func:`_check_cycles` nor a per-edge check can see, because the loop runs
    through the phase containers, which are not edges in this document.

    Propagation stops at a phased node: a phased intermediate is reported by
    its *own* outgoing edge, so carrying its reach further would report the
    same deadlock twice under different names.  The search is a memoised DFS
    over the gating ``needs`` graph.  In a **cyclic** document it cuts back
    edges and may therefore under-report — that is deliberate and harmless:
    :func:`_check_cycles` already errors on a gating cycle, so nothing is
    created either way.

    **Severity follows the weakest edge on the path.** A ``waits-for`` edge
    is vacuously satisfied while its target has no ``parent-child`` children
    (``_waits_for_unsat``, ``src/database/queries/blocked_state.py``) and a
    graph document cannot give a node children — so a path through one is
    reported as a **warning**: it becomes a deadlock only if the target later
    gains children.  Every other gating type is an error.
    """
    findings: list[GraphError] = []
    order: dict[str, int] = {}
    for index, phase in enumerate(graph.phases):
        if phase.key in order:
            findings.append(
                _error("duplicate_phase_key", f"duplicate phase key '{phase.key}'")
            )
            continue
        order[phase.key] = index

    populated: set[str] = set()
    for node in graph.nodes:
        if node.phase is None:
            continue
        if node.phase not in order:
            findings.append(
                _error(
                    "unknown_phase",
                    f"node '{node.key}' names phase '{node.phase}', which the document's "
                    "'phases' does not declare",
                    node.key,
                )
            )
            continue
        populated.add(node.phase)

    for key in order:
        if key not in populated:
            findings.append(
                _error(
                    "phase_without_nodes",
                    f"phase '{key}' has no nodes; it is created anyway and stays open "
                    "until work is filed into it or it is deleted",
                    severity="warning",
                )
            )

    node_phase = {node.key: node.phase for node in graph.nodes}
    for node in graph.nodes:
        if node.phase not in order:
            continue
        for need in node.needs:
            if need.dep_type not in BLOCKING_DEP_TYPES:
                continue
            target = node_phase.get(need.on)
            if target is None or target not in order:
                continue
            if order[target] < order[node.phase]:
                findings.append(
                    _error(
                        "redundant_phase_edge",
                        f"node '{node.key}' needs '{need.on}', which is in the earlier "
                        f"phase '{target}' — the phase gate already covers it",
                        node.key,
                        severity="warning",
                    )
                )

    findings.extend(_check_inverted_phase_paths(graph, order, node_phase))
    return findings


#: The one gating dep type a graph document cannot make bite: ``_waits_for_unsat``
#: is vacuously satisfied while the target has no ``parent-child`` children, and
#: a document cannot give a node children.
_SOFT_GATING_DEP_TYPE = "waits-for"


class _Reach(NamedTuple):
    """The worst later phase reachable from a node over gating edges.

    ``order`` is that phase's index, ``path`` the node keys walked to get
    there (excluding the node itself) and ``soft`` says the walk crossed a
    ``waits-for`` edge, which downgrades the finding to a warning.
    """

    order: int
    path: tuple[str, ...]
    soft: bool


def _worse(a: _Reach | None, b: _Reach | None) -> _Reach | None:
    """The more serious of two reaches: later phase first, then hard over soft."""
    if a is None:
        return b
    if b is None:
        return a
    return b if (b.order, not b.soft) > (a.order, not a.soft) else a


def _check_inverted_phase_paths(
    graph: TaskGraph, order: dict[str, int], node_phase: dict[str, str | None]
) -> list[GraphError]:
    """One ``inverted_phase_edge`` per phased node that reaches a later phase.

    See :func:`_check_phases` for why this is transitive and where it stops.
    """
    nodes = {node.key: node for node in graph.nodes}
    memo: dict[str, _Reach | None] = {}
    walking: set[str] = set()

    def reach(key: str) -> _Reach | None:
        """Worst later-phase reach from *key*'s own outgoing gating edges."""
        if key in memo:
            return memo[key]
        if key in walking:
            # A gating cycle; ``_check_cycles`` reports it and blocks
            # creation, so cutting the back edge here is safe.
            return None
        walking.add(key)
        best: _Reach | None = None
        for need in nodes[key].needs:
            if need.dep_type not in BLOCKING_DEP_TYPES or need.on not in nodes:
                # Non-gating, or an id naming a task outside this document —
                # which no phase declared here withholds.
                continue
            soft = need.dep_type == _SOFT_GATING_DEP_TYPE
            target_phase = node_phase.get(need.on)
            if target_phase in order:
                # Stop here: a phased node answers for its own edges.
                best = _worse(best, _Reach(order[target_phase], (need.on,), soft))
                continue
            onward = reach(need.on)
            if onward is not None:
                best = _worse(
                    best, _Reach(onward.order, (need.on, *onward.path), soft or onward.soft)
                )
        walking.discard(key)
        memo[key] = best
        return best

    findings: list[GraphError] = []
    for node in graph.nodes:
        if node.phase not in order:
            # An unphased node's own start is gated by nothing, so it is only
            # ever the *carrier* of someone else's deadlock, never its owner.
            continue
        worst = reach(node.key)
        if worst is None or worst.order <= order[node.phase]:
            continue
        target_key = worst.path[-1]
        target_phase = node_phase[target_key]
        route = " -> ".join((node.key, *worst.path))
        if worst.soft:
            findings.append(
                _error(
                    "inverted_phase_edge",
                    f"node '{node.key}' in phase '{node.phase}' waits for '{target_key}' in "
                    f"the later phase '{target_phase}' ({route}). A 'waits-for' is satisfied "
                    "while its target has no children, so this runs today — but it deadlocks "
                    f"the moment '{target_key}' gains any, because phase '{target_phase}' "
                    f"cannot start until phase '{node.phase}' completes",
                    node.key,
                    severity="warning",
                )
            )
            continue
        findings.append(
            _error(
                "inverted_phase_edge",
                f"node '{node.key}' in phase '{node.phase}' depends on '{target_key}' in the "
                f"later phase '{target_phase}' ({route}) — that deadlocks: phase "
                f"'{target_phase}' cannot start until phase '{node.phase}' completes, which "
                f"waits on '{node.key}'. Move one of them, or drop the phases and order the "
                "tasks with needs/blocks edges",
                node.key,
            )
        )
    return findings


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
    cache: dict[str, Any | None] = {}

    async def resolve(profile_id: str) -> Any | None:
        """Resolve to the profile that actually exists, or ``None``."""
        if profile_id not in cache:
            cache[profile_id] = await db.get_profile(profile_id)
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
            return None
        from src.profiles.task_execution import task_execution_profile_error

        if error := task_execution_profile_error(resolved):
            errors.append(_error("supervisor_profile", error, node_key))
            return None
        return resolved.id

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


async def _check_subtasks_reportable(graph: TaskGraph, db: Any) -> list[GraphError]:
    """Warn when a node's checklist is one its worker could not tick off.

    ``ensure_default_profiles`` is write-if-absent, so a vault upgraded from
    before the subtask grants existed has profiles without
    ``task_subtask_update``.  Such a worker still *sees* the checklist in
    prime — it just is not told to report progress — so the checklist is
    still worth writing and this is a **warning**, not an error.  Saying so
    at ``--dry-run`` time is what lets a planner fix the grants first.

    Runs after :func:`_check_profiles`, which rewrites ``node.profile`` to the
    id that actually resolved, so the reseed command this names is the real
    profile id.  A node with no profile resolves to nothing and is skipped —
    ``profile_allows_command`` fails open there, and so does this.
    """
    if db is None:
        return []
    from src.prime.sections import SUBTASK_UPDATE_COMMAND, profile_allows_command

    findings: list[GraphError] = []
    allowed: dict[str, bool] = {}
    for node in graph.nodes:
        if not node.subtasks or not node.profile:
            continue
        if node.profile not in allowed:
            allowed[node.profile] = await profile_allows_command(
                db, node.profile, SUBTASK_UPDATE_COMMAND
            )
        if allowed[node.profile]:
            continue
        findings.append(
            _error(
                "subtasks_unreportable",
                f"profile '{node.profile}' cannot run '{SUBTASK_UPDATE_COMMAND}', so the "
                "worker will see this checklist but not be told to tick it off — "
                f"run `aq agent profile-reseed --profile-id {node.profile} --grants-only`",
                node.key,
                severity="warning",
            )
        )
    return findings


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
    findings.extend(_check_phases(graph))
    findings.extend(_check_titles(graph))
    findings.extend(_check_acceptance(graph))
    findings.extend(_check_dep_types(graph))
    findings.extend(_check_self_edges(graph))
    findings.extend(_check_cycles(graph))
    findings.extend(_check_foreign_projects(graph, project_id))
    findings.extend(await _check_needs(graph, project_id, db))
    findings.extend(await _check_profiles(graph, project_id, db))
    findings.extend(await _check_subtasks_reportable(graph, db))
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
