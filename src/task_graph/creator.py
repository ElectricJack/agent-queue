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

Id assignment goes through :func:`assign_child_ids`, the interface the
work-graph spec will later back with hierarchical ids (``<parent>.1``).
Today it returns this codebase's flat adjective-noun ids, which is the
explicitly sanctioned interim (impl spec §1) — swapping the body changes no
caller and no stored format.
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
from src.task_names import generate_task_id

logger = logging.getLogger(__name__)

#: Status the container parent is created with.  IN_PROGRESS keeps it out of
#: the scheduler (which only picks up READY) while
#: ``Orchestrator._check_plan_parent_completion`` auto-completes it once every
#: subtask finishes — the same idiom plan parents already use.
PARENT_STATUS = "IN_PROGRESS"

#: Nodes are born DEFINED so the cascade's dependency promotion decides when
#: each becomes READY.  A node with no blocking edge is promoted on the next
#: 5s cycle; a node with edges waits for them, which is the whole point of
#: creating a graph rather than a pile of tasks.
NODE_STATUS = "DEFINED"


async def assign_child_ids(db: Any, parent_id: str, keys: list[str]) -> dict[str, str]:
    """Assign a task id to every graph key.

    The work-graph spec owns hierarchical ids; until it lands this returns
    flat ids from the existing generator, behind the interface that spec
    names.  *parent_id* is accepted (and unused) so the hierarchical
    implementation is a drop-in replacement.
    """
    assigned: dict[str, str] = {}
    for key in keys:
        assigned[key] = await generate_task_id(db)
    return assigned


@dataclass
class GraphPlan:
    """Everything a graph will write, resolved but not yet persisted."""

    parent_id: str
    parent_row: dict
    node_rows: list[dict] = field(default_factory=list)
    dependency_rows: list[dict] = field(default_factory=list)
    context_rows: list[dict] = field(default_factory=list)
    criteria_rows: list[dict] = field(default_factory=list)
    label_rows: list[dict] = field(default_factory=list)
    #: graph key → assigned task id
    ids: dict[str, str] = field(default_factory=dict)

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


async def build_plan(db: Any, graph: TaskGraph, *, project_id: str) -> GraphPlan:
    """Resolve ids and materialise every row the graph will write.

    Pure resolution — nothing is persisted.  ``--dry-run`` returns the report
    built from this, so what a dry run shows is literally what a real run
    inserts.
    """
    now = time.time()
    parent_id = await generate_task_id(db)
    ids = await assign_child_ids(db, parent_id, graph.node_keys())

    parent = graph.parent
    parent_title = (parent.title if parent else "") or "Task graph"
    parent_row = {
        "id": parent_id,
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
        "created_at": now,
        "updated_at": now,
    }

    plan = GraphPlan(parent_id=parent_id, parent_row=parent_row, ids=ids)

    if parent:
        for label in parent.labels:
            if label.strip():
                plan.label_rows.append({"task_id": parent_id, "label": label.strip()})

    for node in graph.nodes:
        task_id = ids[node.key]
        plan.node_rows.append(
            {
                "id": task_id,
                "project_id": project_id,
                "parent_task_id": parent_id,
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
            }
        )

        for need in node.needs:
            depends_on = ids.get(need.on, need.on)
            plan.dependency_rows.append(
                {
                    "task_id": task_id,
                    "depends_on_task_id": depends_on,
                    "dep_type": need.dep_type,
                }
            )

        for ctx in node.context:
            plan.context_rows.append(_context_row(task_id, ctx, graph))

        for order, criterion in enumerate(c for c in node.acceptance if c and c.strip()):
            plan.criteria_rows.append(
                {
                    "id": uuid.uuid4().hex[:12],
                    "task_id": task_id,
                    "type": "acceptance",
                    "content": criterion.strip(),
                    "sort_order": order,
                }
            )

        for label in node.labels:
            if label.strip():
                plan.label_rows.append({"task_id": task_id, "label": label.strip()})

    return plan


async def _insert_task(conn, row: dict) -> None:
    """Insert one task row.

    Factored out — and module-level — so the single-transaction guarantee can
    be tested by patching this to fail partway through (§12).
    """
    await conn.execute(insert(tasks).values(**row))


async def write_plan(db: Any, plan: GraphPlan) -> None:
    """Persist a :class:`GraphPlan` in exactly one transaction.

    Any exception propagates with the transaction rolled back, so no partial
    graph survives a mid-write failure.
    """
    async with db._engine.begin() as conn:
        await _insert_task(conn, plan.parent_row)
        for row in plan.node_rows:
            await _insert_task(conn, row)
        if plan.dependency_rows:
            await conn.execute(insert(task_dependencies), plan.dependency_rows)
        if plan.context_rows:
            await conn.execute(insert(task_context), plan.context_rows)
        if plan.criteria_rows:
            await conn.execute(insert(task_criteria), plan.criteria_rows)
        if plan.label_rows:
            # Duplicate (task_id, label) pairs would violate the PK; the
            # author's intent for a repeated label is one label.
            seen: set[tuple[str, str]] = set()
            unique = []
            for row in plan.label_rows:
                key = (row["task_id"], row["label"])
                if key in seen:
                    continue
                seen.add(key)
                unique.append(row)
            await conn.execute(insert(task_labels), unique)


def build_report(graph: TaskGraph, plan: GraphPlan, *, dry_run: bool) -> dict:
    """The shape ``_cmd_create_task_graph`` returns on success."""
    return {
        "parent_id": plan.parent_id,
        "parent_title": plan.parent_row["title"],
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
) -> dict:
    """Create the graph, or report what creating it would do.

    *handler* is the ``CommandHandler`` (its ``db`` property is the only
    thing used).  Returns the report from :func:`build_report`; the caller
    layers validation warnings on top.
    """
    db = handler.db
    plan = await build_plan(db, graph, project_id=project_id)
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
