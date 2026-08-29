"""Turn a validated :class:`~src.task_graph.models.TaskGraph` into rows.

Implements docs/specs/implementation/supervisor-agent.md §8's
:func:`create_graph`.  The contract that matters here is the one from §12:
**a graph is created whole or not at all**.  Every insert — parent task, node
tasks, dependency edges, context rows, acceptance criteria, labels — runs
inside one ``engine.begin()`` block, so a failure on node 3 leaves zero rows.

That is why this module reaches for ``db._engine`` instead of composing the
per-method helpers on the query mixins: each of those opens its own
transaction, and a graph built from a dozen independent commits is exactly
the partial-graph failure mode the spec forbids.

Id assignment goes through :func:`assign_child_ids`: a new container's
children get dotted ids ``<container>.1..N`` known at plan time; a graph
created under an existing container gets provisional ``<parent>.?``
placeholders, reserved atomically and rewritten inside ``write_plan``'s
transaction (spec §6).
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import insert

from src.database.tables import (
    task_context,
    task_criteria,
    task_dependencies,
    task_labels,
    tasks,
)
from src.task_graph.models import GraphNode, TaskGraph
from src.task_names import generate_task_id, reserve_child_ordinal

logger = logging.getLogger(__name__)

#: Status the container parent is created with.  IN_PROGRESS keeps it out of
#: the scheduler (which only picks up READY); the container settles via
#: ``settle_containers`` (spec §7) once every subtask finishes — the same
#: idiom plan parents already use.
PARENT_STATUS = "IN_PROGRESS"

#: Nodes are born DEFINED so the cascade's dependency promotion decides when
#: each becomes READY.  A node with no blocking edge is promoted on the next
#: 5s cycle; a node with edges waits for them, which is the whole point of
#: creating a graph rather than a pile of tasks.
NODE_STATUS = "DEFINED"

#: Placeholder suffix for a node id whose ordinal hasn't been reserved yet —
#: only used when the graph is being created under an *existing* container,
#: where the ordinal must be reserved inside ``write_plan``'s transaction.
PROVISIONAL_SUFFIX = ".?"


def assign_child_ids(parent_id: str, keys: list[str], *, provisional: bool) -> dict[str, str]:
    """Dotted ids for every key (spec §6).

    A *new* container's children are numbered ``1..N`` in document order with
    no round trip.  Under an *existing* parent the ordinals are reserved in
    ``write_plan``'s transaction, so the plan shows ``<parent>.?``.
    """
    if provisional:
        return {key: f"{parent_id}{PROVISIONAL_SUFFIX}" for key in keys}
    return {key: f"{parent_id}.{i + 1}" for i, key in enumerate(keys)}


@dataclass
class GraphPlan:
    """Everything a graph will write, resolved but not yet persisted."""

    parent_id: str
    parent_row: dict | None
    node_rows: list[dict] = field(default_factory=list)
    dependency_rows: list[dict] = field(default_factory=list)
    context_rows: list[dict] = field(default_factory=list)
    criteria_rows: list[dict] = field(default_factory=list)
    label_rows: list[dict] = field(default_factory=list)
    #: graph key → assigned (or provisional) task id
    ids: dict[str, str] = field(default_factory=dict)
    #: True when the container already existed and node ids are provisional
    #: (``<parent>.?``) until ``write_plan`` reserves ordinals.
    provisional: bool = False

    @property
    def task_ids(self) -> list[str]:
        return [row["id"] for row in self.node_rows]


def _context_row(task_id: str, ctx, graph: TaskGraph) -> dict:
    """Build one ``task_context`` row for a node context entry.

    ``spec_ref`` rows carry JSON ``{path, section}`` so ``aq prime`` can
    resolve and render the referenced section later (rendering is owned by
    the aq-surface spec); ``file`` rows carry the bare path.
    """
    if ctx.type == "spec_ref":
        path = ctx.path or graph.spec or ""
        content = json.dumps({"path": path, "section": ctx.section})
        label = ctx.label or (ctx.section or path)
    elif ctx.type == "file":
        content = ctx.path or ctx.content or ""
        label = ctx.label or content
    else:
        content = ctx.content if ctx.content is not None else json.dumps(ctx.to_dict())
        label = ctx.label or ctx.type
    return {
        "id": uuid.uuid4().hex[:12],
        "task_id": task_id,
        "type": ctx.type,
        "label": label,
        "content": content,
    }


def _compose_description(node: GraphNode) -> str:
    """Description an agent actually receives.

    Acceptance criteria are also stored as ``task_criteria`` rows, but
    nothing reads that table into a prompt yet, and a task whose acceptance
    an agent can't see is a task written badly (design §4 Rules).  So they
    are appended to the description too.

    .. warning::

       This double-write is **coupled to ``task_criteria`` having no reader.**
       This function is that table's only writer, and
       ``src/database/queries/task_queries.py`` exposes no getter — see the
       comment on ``src/prime/sections.build_task_section``, which carries the
       description precisely because the criteria are unreachable.  The day
       someone adds a getter and wires it into the prime document, section 3
       renders every criterion twice.  Drop this append in the same change.
    """
    description = (node.description or node.title or "").strip()
    criteria = [c.strip() for c in node.acceptance if c and c.strip()]
    if not criteria:
        return description
    rendered = "\n".join(f"- {c}" for c in criteria)
    return f"{description}\n\n## Acceptance Criteria\n{rendered}".strip()


async def build_plan(
    db: Any, graph: TaskGraph, *, project_id: str, parent_id: str | None = None
) -> GraphPlan:
    """Resolve ids and materialise every row the graph will write.

    Pure resolution — nothing is persisted.  ``--dry-run`` returns the report
    built from this, so what a dry run shows is literally what a real run
    inserts.

    Without *parent_id* a brand new container is planned: its id is minted
    now and its children get dotted ids ``<container>.1..N`` in document
    order, no DB round trip.  With *parent_id* the graph is created under an
    *existing* container: no new parent row is planned (``parent_row`` stays
    ``None``) and every node id is the provisional placeholder
    ``<parent>.?`` — the real ordinals are reserved atomically inside
    ``write_plan``'s transaction so concurrent graph creations never race.
    """
    now = time.time()
    provisional = parent_id is not None
    container_id = parent_id or await generate_task_id(db)
    ids = assign_child_ids(container_id, graph.node_keys(), provisional=provisional)

    parent = graph.parent
    parent_row: dict | None = None
    if not provisional:
        parent_title = (parent.title if parent else "") or "Task graph"
        parent_row = {
            "id": container_id,
            "project_id": project_id,
            "parent_task_id": None,
            "title": parent_title,
            "description": (parent.description if parent else "") or parent_title,
            "priority": parent.priority if parent else 100,
            "status": PARENT_STATUS,
            "verification_type": "auto_test",
            "retry_count": 0,
            "max_retries": 3,
            "requires_approval": 0,
            "is_plan_subtask": 0,
            "profile_id": parent.profile if parent else None,
            "attachments": "[]",
            "auto_approve_plan": 0,
            "skip_verification": 0,
            "is_blocked": 0,
            "next_child_ordinal": len(graph.nodes) + 1,
            "created_at": now,
            "updated_at": now,
        }

    plan = GraphPlan(
        parent_id=container_id, parent_row=parent_row, ids=ids, provisional=provisional
    )

    if parent_row is not None and parent:
        for label in parent.labels:
            if label.strip():
                plan.label_rows.append({"task_id": container_id, "label": label.strip()})

    for node in graph.nodes:
        task_id = ids[node.key]
        plan.node_rows.append(
            {
                "id": task_id,
                "project_id": project_id,
                "parent_task_id": None,  # set_parent (in write_plan) writes it
                "title": node.title,
                "description": _compose_description(node),
                "priority": node.priority,
                "status": NODE_STATUS,
                "verification_type": "auto_test",
                "retry_count": 0,
                "max_retries": 3,
                "requires_approval": 0,
                "is_plan_subtask": 0,
                "task_type": node.task_type,
                "profile_id": node.profile,
                "attachments": "[]",
                "auto_approve_plan": 0,
                "skip_verification": 0,
                "is_blocked": 0,
                "created_at": now,
                "updated_at": now,
                "_key": node.key,
            }
        )

        for need in node.needs:
            # ``need.on`` is a graph-internal key only when it resolved into
            # ``ids``; an id already pointing at an existing task is kept
            # verbatim and carries no ``_dep_key`` (nothing to rewrite).
            dep_key = need.on if need.on in ids else None
            depends_on = ids.get(need.on, need.on)
            plan.dependency_rows.append(
                {
                    "task_id": task_id,
                    "depends_on_task_id": depends_on,
                    "dep_type": need.dep_type,
                    "_task_key": node.key,
                    "_dep_key": dep_key,
                }
            )

        for ctx in node.context:
            row = _context_row(task_id, ctx, graph)
            row["_key"] = node.key
            plan.context_rows.append(row)

        for order, criterion in enumerate(c for c in node.acceptance if c and c.strip()):
            plan.criteria_rows.append(
                {
                    "id": uuid.uuid4().hex[:12],
                    "task_id": task_id,
                    "type": "acceptance",
                    "content": criterion.strip(),
                    "sort_order": order,
                    "_key": node.key,
                }
            )

        for label in node.labels:
            if label.strip():
                plan.label_rows.append(
                    {"task_id": task_id, "label": label.strip(), "_key": node.key}
                )

    return plan


def _strip_private(row: dict) -> dict:
    """Drop every key starting with ``_`` — plan-internal bookkeeping only."""
    return {k: v for k, v in row.items() if not k.startswith("_")}


async def _insert_task(conn, row: dict) -> None:
    """Insert one task row.

    Factored out — and module-level — so the single-transaction guarantee can
    be tested by patching this to fail partway through (§12).
    """
    await conn.execute(insert(tasks).values(**_strip_private(row)))


def _rewrite_ids(plan: GraphPlan, real: dict[str, str]) -> None:
    """Replace provisional ids with reserved ones in every row of *plan*.

    ``build_plan`` stamps each row with the graph key(s) it came from
    (``_key``, ``_task_key``, ``_dep_key``) so this is a lookup, never a
    reverse search over ids.
    """
    plan.ids = dict(real)
    for row in plan.node_rows:
        row["id"] = real[row["_key"]]
    for row in plan.dependency_rows:
        row["task_id"] = real.get(row.get("_task_key"), row["task_id"])
        row["depends_on_task_id"] = real.get(row.get("_dep_key"), row["depends_on_task_id"])
    for coll in (plan.context_rows, plan.criteria_rows, plan.label_rows):
        for row in coll:
            row["task_id"] = real.get(row.get("_key"), row["task_id"])


async def write_plan(db: Any, plan: GraphPlan) -> None:
    """Persist a :class:`GraphPlan` in exactly one transaction.

    Any exception propagates with the transaction rolled back, so no partial
    graph survives a mid-write failure.  A brand new container is inserted
    and flagged (spec §6); an existing one is left untouched.  When the plan
    is provisional, ordinals are reserved here — inside this transaction —
    and every provisional id is rewritten to the real one before anything
    else is inserted.  Every node is then linked to the container via
    :meth:`HierarchyQueryMixin.set_parent`, which writes the ``parent-child``
    edge, the ``tasks.parent_task_id`` cache and recomputes/settles as it
    goes; the returned flip/settle info is ignored here because a brand new
    (or still-open) container has nothing left to settle.
    """
    async with db._engine.begin() as conn:
        if plan.parent_row is not None:
            await _insert_task(conn, plan.parent_row)
            await db.mark_container(plan.parent_id, conn=conn)
        if plan.provisional:
            real: dict[str, str] = {}
            for key in plan.ids:
                ordinal = await reserve_child_ordinal(conn, plan.parent_id)
                real[key] = f"{plan.parent_id}.{ordinal}"
            _rewrite_ids(plan, real)
        for row in plan.node_rows:
            await _insert_task(conn, row)
        for row in plan.node_rows:
            await db.set_parent(row["id"], plan.parent_id, conn=conn)
        if plan.dependency_rows:
            await conn.execute(
                insert(task_dependencies), [_strip_private(r) for r in plan.dependency_rows]
            )
        if plan.context_rows:
            await conn.execute(insert(task_context), [_strip_private(r) for r in plan.context_rows])
        if plan.criteria_rows:
            await conn.execute(
                insert(task_criteria), [_strip_private(r) for r in plan.criteria_rows]
            )
        if plan.label_rows:
            # Duplicate (task_id, label) pairs would violate the PK; the
            # author's intent for a repeated label is one label.
            seen: set[tuple[str, str]] = set()
            unique = []
            for row in plan.label_rows:
                clean = _strip_private(row)
                key = (clean["task_id"], clean["label"])
                if key in seen:
                    continue
                seen.add(key)
                unique.append(clean)
            await conn.execute(insert(task_labels), unique)
        await db.recompute_blocked(set(plan.task_ids), conn=conn)


def build_report(graph: TaskGraph, plan: GraphPlan, *, dry_run: bool) -> dict:
    """The shape ``_cmd_create_task_graph`` returns on success."""
    return {
        "parent_id": plan.parent_id,
        "parent_title": plan.parent_row["title"] if plan.parent_row is not None else None,
        "provisional": plan.provisional,
        "task_ids": plan.task_ids,
        "nodes": [
            {
                "key": node.key,
                "task_id": plan.ids[node.key],
                "title": node.title,
                "needs": [
                    {
                        "on": need.on,
                        "task_id": plan.ids.get(need.on, need.on),
                        "dep_type": need.dep_type,
                    }
                    for need in node.needs
                ],
            }
            for node in graph.nodes
        ],
        "dependency_count": len(plan.dependency_rows),
        "context_count": len(plan.context_rows),
        "dry_run": dry_run,
        "created": not dry_run,
    }


async def create_graph(
    handler: Any,
    graph: TaskGraph,
    *,
    project_id: str,
    dry_run: bool = False,
    parent_id: str | None = None,
) -> dict:
    """Create the graph, or report what creating it would do.

    *handler* is the ``CommandHandler`` (its ``db`` property is the only
    thing used).  Returns the report from :func:`build_report`; the caller
    layers validation warnings on top.  *parent_id* creates the graph under
    an existing container instead of minting a new one.
    """
    db = handler.db
    plan = await build_plan(db, graph, project_id=project_id, parent_id=parent_id)
    if dry_run:
        return build_report(graph, plan, dry_run=True)
    await write_plan(db, plan)
    logger.info(
        "Created task graph parent=%s nodes=%d deps=%d project=%s",
        plan.parent_id,
        len(plan.node_rows),
        len(plan.dependency_rows),
        project_id,
    )
    return build_report(graph, plan, dry_run=False)
