"""Blocked-state projection — ``tasks.is_blocked`` recompute.

Implements docs/specs/design/work-graph.md §4 and
docs/specs/implementation/work-graph.md §3.

``tasks.is_blocked`` is a **pure projection**, never authored:

    is_blocked(t) = 1  iff  any blocking edge out of t is unsatisfied
                            or any gate attached to t is not resolved

It is *graph* blockedness only.  Transient capacity reasons (no idle agent,
workspace locked, budget, cooldown) are deliberately not persisted — they
change per tick and belong to explain, not the row.

The recompute runs **inside the caller's transaction**: every mutating query
method that can change blockedness does read + write + recompute in one
``self._engine.begin()`` block, so a reader never observes a mutation
without its projection.
"""

from __future__ import annotations

import logging
import time

from sqlalchemy import and_, case, false, func, insert, literal, not_, or_, select, update

from src.database.queries.ingest_hold import not_ingest_held
from src.database.tables import (
    events,
    gates,
    projects,
    repos,
    task_completion_records,
    task_delivery_receipts,
    task_dependencies,
    task_gates,
    task_labels,
    task_metadata,
    tasks,
)
from src.models import BLOCKING_DEP_TYPES, HOLD_LABEL_PREFIX, DepType, Task, TaskStatus

logger = logging.getLogger(__name__)

__all__ = [
    "BLOCKING_DEP_TYPES",
    "PROJECTION_INPUT_COLUMNS",
    "BlockedStateMixin",
    "apply_label_filters",
    "blocked_predicate",
    "delivery_landed",
    "unmet_dependency_predicate",
]


# ``tasks`` columns the predicate reads.  A write to any of them can change
# some row's projection, so every mutating query method must recompute when
# its value set touches one — not just on ``status``: the
# ``conditional-blocks`` clause reads ``retry_count >= max_retries`` to tell a
# transient failure from a terminal one, so bumping a retry counter alone can
# flip a contingency task.
PROJECTION_INPUT_COLUMNS = frozenset({
    "status", "retry_count", "max_retries", "branch_name", "repo_id",
})


# Statuses of a ``parent-child`` container that keep its children withheld.
# Anything else counts as "released" (design §3.1).
_WITHHOLDING_PARENT_STATUSES = (
    TaskStatus.DEFINED.value,
)


# -- clause factories -------------------------------------------------------
#
# One factory per blocking rule of design §3.1.  Each builds a *fresh*
# correlated ``EXISTS`` over **anonymous** aliases, so the same factory can be
# called from several predicates — even twice inside one statement — without
# alias collisions: SQLAlchemy names them ``anon_1``, ``anon_2``, … per
# compiled statement.  The per-edge rule itself lives in
# :func:`unmet_dependency_predicate`; this lets the explain read path ask the
# exact same question as the persisted projection instead of approximating
# every edge as ``status != COMPLETED``.


#: ``task_metadata`` key an obsolete close writes (``aq task close --obsolete``):
#: the task's work was superseded, so there is nothing of it to publish.  JSON:
#: ``reason``, ``closed_by``, ``closed_at``, ``previous_status`` and the
#: ``cleanup`` it still owes (src/sessions/obsolete.py).
#: Evidence-preserving retirement also records ``replacement_task_id``,
#: ``satisfied_by`` and the transactional ``rewire`` plan. These are audit
#: evidence, never dependency satisfaction: dependent edges must be rewritten.
OBSOLETE_META_KEY = "obsolete"


def obsolete_marker(task):
    """``EXISTS``: the correlated *task* (table or alias) was closed as obsolete."""
    marker = task_metadata.alias()
    return (
        select(literal(1))
        .where(marker.c.task_id == task.c.id, marker.c.key == OBSOLETE_META_KEY)
        .correlate(task)
        .exists()
    )


#: Receipt dispositions that deliver the source: landed code, or a verified
#: no-op.  They mirror ``DeliveryEvidence.satisfied`` (``CONTAINED`` and
#: ``NO_CHANGE``); ``ineligible``, ``skipped`` and ``failed`` deliver nothing.
LANDED_RECEIPT_DISPOSITIONS = ("code", "noop")


def _default_branch_name(column):
    return func.regexp_replace(column, "^refs/heads/", "")


def delivery_landed(depends_on):
    """``EXISTS``: the correlated *depends_on* task's work reached its default branch.

    The receipt is the durable record of a landing that the delivery observer
    confirms in git (src/integration/delivery_truth.py); git evidence itself is
    never persisted, so the projection reads the receipt.  A landed receipt is
    a *root* receipt (``target_task_id`` NULL) into the default branch of the
    project's integration repository, with a delivering disposition.

    A code leaf nested in an epic is only ever delivered to its parent's
    branch; it lands when an ancestor does.  So the receipt may name the task
    or any ancestor, provided it was recorded no earlier than the task's latest
    completion: an epic that landed before a later generation of the leaf
    closed did not carry that generation.
    """
    seed = tasks.alias()
    step = tasks.alias()
    lineage = (
        select(seed.c.id.label("id"), seed.c.parent_task_id.label("parent_task_id"))
        .where(seed.c.id == depends_on.c.id)
        .correlate(depends_on)
        .cte(recursive=True, nesting=True)
    )
    lineage = lineage.union(
        select(step.c.id, step.c.parent_task_id).where(step.c.id == lineage.c.parent_task_id)
    )
    receipt = task_delivery_receipts.alias()
    project = projects.alias()
    repository = repos.alias()
    completion = task_completion_records.alias()
    latest_completion = (
        select(func.max(completion.c.completed_at))
        .where(completion.c.task_id == depends_on.c.id)
        .correlate(depends_on)
        .scalar_subquery()
    )
    return (
        select(literal(1))
        .select_from(
            receipt.join(project, project.c.integration_repository_id == receipt.c.repository_id)
            .join(repository, repository.c.id == project.c.integration_repository_id)
        )
        .where(
            and_(
                project.c.id == depends_on.c.project_id,
                receipt.c.source_task_id.in_(select(lineage.c.id)),
                receipt.c.target_task_id.is_(None),
                receipt.c.disposition.in_(LANDED_RECEIPT_DISPOSITIONS),
                _default_branch_name(receipt.c.target_branch)
                == _default_branch_name(repository.c.default_branch),
                receipt.c.created_at >= func.coalesce(latest_completion, 0.0),
            )
        )
        .correlate(depends_on)
        .exists()
    )


def unmet_dependency_predicate(dependency, depends_on, *, dep_types=None):
    """Return whether one typed dependency edge is currently unsatisfied.

    ``dependency`` is a ``task_dependencies`` table or alias and
    ``depends_on`` is the joined target ``tasks`` table or alias.  Supplying
    ``dep_types`` narrows the expression for projection clause factories;
    omitting it covers every blocking type and is used by diagnostic queries.
    Provenance edge types deliberately produce ``false``.
    """
    effective = BLOCKING_DEP_TYPES if dep_types is None else dep_types
    clauses = []

    if DepType.BLOCKS.value in effective:
        clauses.append(
            and_(
                dependency.c.dep_type == DepType.BLOCKS.value,
                depends_on.c.status != TaskStatus.COMPLETED.value,
            )
        )

    if DepType.PARENT_CHILD.value in effective:
        hold = task_metadata.alias()
        approval_held = (
            select(literal(1))
            .select_from(hold)
            .where(
                and_(
                    hold.c.task_id == depends_on.c.id,
                    hold.c.key == "manual_pause_withholds_children",
                    hold.c.value == "true",
                )
            )
            .exists()
        )
        clauses.append(
            and_(
                dependency.c.dep_type == DepType.PARENT_CHILD.value,
                or_(
                    depends_on.c.status.in_(_WITHHOLDING_PARENT_STATUSES),
                    and_(depends_on.c.status == TaskStatus.PAUSED.value, approval_held),
                ),
            )
        )

    if DepType.WAITS_FOR.value in effective:
        parent_child = task_dependencies.alias()
        child = tasks.alias()
        open_child = (
            select(literal(1))
            .select_from(parent_child.join(child, child.c.id == parent_child.c.task_id))
            .where(
                and_(
                    parent_child.c.dep_type == DepType.PARENT_CHILD.value,
                    parent_child.c.depends_on_task_id == dependency.c.depends_on_task_id,
                    child.c.status != TaskStatus.COMPLETED.value,
                )
            )
            .exists()
        )
        clauses.append(
            and_(
                dependency.c.dep_type == DepType.WAITS_FOR.value,
                open_child,
            )
        )

    if DepType.CONDITIONAL_BLOCKS.value in effective:
        terminal_failure = or_(
            depends_on.c.status == TaskStatus.BLOCKED.value,
            and_(
                depends_on.c.status == TaskStatus.FAILED.value,
                depends_on.c.retry_count >= depends_on.c.max_retries,
            ),
        )
        clauses.append(
            and_(
                dependency.c.dep_type == DepType.CONDITIONAL_BLOCKS.value,
                not_(terminal_failure),
            )
        )

    if DepType.DELIVERY_OF.value in effective:
        # Independent of task completion: unknown (no receipt) and failed
        # delivery both stay unmet until a landing is recorded.
        clauses.append(
            and_(
                dependency.c.dep_type == DepType.DELIVERY_OF.value,
                not_(delivery_landed(depends_on)),
            )
        )

    return or_(*clauses) if clauses else false()


def _blocks_unsat():
    """``blocks`` — satisfied once the dependency is COMPLETED (graph completion only)."""
    bd = task_dependencies.alias()
    bt = tasks.alias()
    return (
        select(literal(1))
        .select_from(bd.join(bt, bt.c.id == bd.c.depends_on_task_id))
        .where(
            and_(
                bd.c.task_id == tasks.c.id,
                unmet_dependency_predicate(bd, bt, dep_types={DepType.BLOCKS.value}),
            )
        )
        .exists()
    )


def _parent_child_unsat():
    """``parent-child`` — satisfied once the container has been released."""
    pd = task_dependencies.alias()
    pt = tasks.alias()
    return (
        select(literal(1))
        .select_from(pd.join(pt, pt.c.id == pd.c.depends_on_task_id))
        .where(
            and_(
                pd.c.task_id == tasks.c.id,
                unmet_dependency_predicate(pd, pt, dep_types={DepType.PARENT_CHILD.value}),
            )
        )
        .exists()
    )


def _waits_for_unsat():
    """``waits-for`` — dynamic fan-in over the container's children.

    Unsatisfied while any ``parent-child`` child of the container is not
    COMPLETED.  Vacuously satisfied when the container has no children.
    """
    wd = task_dependencies.alias()
    return (
        select(literal(1))
        .select_from(wd)
        .where(
            and_(
                wd.c.task_id == tasks.c.id,
                unmet_dependency_predicate(wd, tasks, dep_types={DepType.WAITS_FOR.value}),
            )
        )
        .exists()
    )


def _conditional_unsat():
    """``conditional-blocks`` — satisfied only on *terminal* failure.

    A transiently FAILED dependency about to be retried does not satisfy it.
    """
    cd = task_dependencies.alias()
    ct = tasks.alias()
    return (
        select(literal(1))
        .select_from(cd.join(ct, ct.c.id == cd.c.depends_on_task_id))
        .where(
            and_(
                cd.c.task_id == tasks.c.id,
                unmet_dependency_predicate(cd, ct, dep_types={DepType.CONDITIONAL_BLOCKS.value}),
            )
        )
        .exists()
    )


def _failure_reason(evidence) -> str:
    """The operator-facing reason a ``failed`` receipt's evidence records."""
    if isinstance(evidence, dict):
        for key in ("reason", "error", "detail"):
            if evidence.get(key):
                return str(evidence[key])
    return "delivery failed"


def _delivery_of_unsat():
    """``delivery-of`` — satisfied once the target's delivery has landed."""
    dd = task_dependencies.alias()
    dt = tasks.alias()
    return (
        select(literal(1))
        .select_from(dd.join(dt, dt.c.id == dd.c.depends_on_task_id))
        .where(
            and_(
                dd.c.task_id == tasks.c.id,
                unmet_dependency_predicate(dd, dt, dep_types={DepType.DELIVERY_OF.value}),
            )
        )
        .exists()
    )


def _gate_open():
    """Gates — an attached gate blocks until it is ``resolved``.

    ``expired`` keeps blocking on purpose: a timed-out approval must never
    silently self-approve (design §5.4).
    """
    tg = task_gates.alias()
    gt = gates.alias()
    return (
        select(literal(1))
        .select_from(tg.join(gt, gt.c.id == tg.c.gate_id))
        .where(and_(tg.c.task_id == tasks.c.id, gt.c.status != "resolved"))
        .exists()
    )


def blocked_predicate():
    """Return the SQL boolean expression for "this ``tasks`` row is blocked".

    Correlates against the ``tasks`` table itself, so it can be dropped into
    a ``CASE`` inside an ``UPDATE tasks`` or into a ``SELECT ... WHERE``.
    Built from correlated ``EXISTS`` subqueries.

    One clause per blocking rule, in the order of design §3.1.
    Development delivery of ``blocks`` edges is observed asynchronously by
    integration.admission outside SQL transactions; a ``delivery-of`` edge
    reads only the durable landing receipt (:func:`delivery_landed`), and the
    delivery observer recomputes its dependents when a landing is observed.
    """
    return or_(
        _blocks_unsat(),
        _parent_child_unsat(),
        _waits_for_unsat(),
        _conditional_unsat(),
        _delivery_of_unsat(),
        _gate_open(),
    )


class BlockedStateMixin:
    """Recompute and query the persisted blocked-state projection.

    Mixed into the database adapters alongside the other query mixins.
    Expects ``self._engine``.
    """

    # -- recompute ------------------------------------------------------

    async def recompute_blocked(self, seed_task_ids: set[str], *, conn) -> set[str]:
        """Refresh ``is_blocked`` for everything the seeds can affect.

        **Requires** the caller's open connection — it never opens its own
        transaction, so the mutation and its projection commit together.

        Returns the ids whose ``is_blocked`` value actually flipped.  The
        caller emits ``task.blocked`` / ``task.unblocked`` for them *after*
        the transaction commits (see :meth:`log_blocked_flips`).

        One wave is exact: within a transaction every other task's *status*
        is fixed, so the predicate is a pure function of rows that are not
        changing, and blockedness is deliberately not transitive through
        blockedness (design §4.3).  Callers that change several statuses in
        one transaction — bulk graph creation, the conditional auto-close
        cascade — re-seed with the tasks whose status changed;
        :meth:`recompute_blocked_waves` does that.
        """
        if not seed_task_ids:
            return set()

        affected = await self._collect_affected(seed_task_ids, conn)
        if not affected:
            return set()

        # An IN-list's order does not determine PostgreSQL's UPDATE scan or
        # lock order. Lock explicitly in id order, as recursive deletion does.
        # NO KEY UPDATE protects the projection without blocking FK readers.
        ordered = sorted(affected)

        before = {
            r[0]: r[1]
            for r in (
                await conn.execute(
                    select(tasks.c.id, tasks.c.is_blocked)
                    .where(tasks.c.id.in_(ordered))
                    .order_by(tasks.c.id)
                    .with_for_update(key_share=True)
                )
            ).fetchall()
        }
        if not before:
            return set()

        # NOTE: `updated_at` is deliberately NOT touched.  It means "time in
        # current state" and drives stuck-task detection; a projection
        # refresh is not a state change.
        await conn.execute(
            update(tasks)
            .where(tasks.c.id.in_(ordered))
            .values(is_blocked=case((blocked_predicate(), 1), else_=0))
        )

        after = {
            r[0]: r[1]
            for r in (
                await conn.execute(
                    select(tasks.c.id, tasks.c.is_blocked).where(tasks.c.id.in_(ordered))
                )
            ).fetchall()
        }

        return {tid for tid, old in before.items() if after.get(tid) != old}

    async def recompute_blocked_waves(
        self, seed_task_ids: set[str], *, conn, extra_waves: list[set[str]] | None = None
    ) -> set[str]:
        """Run :meth:`recompute_blocked` once per supplied seed set.

        Exactly ``1 + len(extra_waves)`` waves, in order — this is a driver
        for callers that mutate several statuses inside one transaction, not
        an iteration to a fixpoint.  ``extra_waves`` names the seeds each
        later wave needs (e.g. the tasks the conditional cascade auto-closed
        after the first wave's seed set was already chosen).

        No termination question arises, and none needs to: ``is_blocked`` is
        a pure function of statuses, edges and gates, and blockedness is not
        transitive through blockedness (design §4.3).  One wave per fixed set
        of statuses is therefore *exact*, and a wave can only be needed
        because the transaction changed a status the previous wave could not
        have seen.  Nothing a wave writes can make an earlier wave's answer
        wrong, so re-seeding from the flipped set would be dead work.
        """
        pending = set(seed_task_ids)
        queued = list(extra_waves or [])
        flipped_all: set[str] = set()
        while pending:
            flipped = await self.recompute_blocked(pending, conn=conn)
            flipped_all |= flipped
            pending = queued.pop(0) if queued else set()
        return flipped_all

    async def _collect_affected(self, seeds: set[str], conn) -> set[str]:
        """Expand the seed set to everything whose projection can change.

        ``A = S`` ∪ direct dependents of ``S`` over any blocking edge ∪
        ``waits-for`` waiters on any container that a seed is a child of
        (implementation spec §3.2).  Gate waiters are seeded directly by the
        gate mutations themselves.
        """
        affected = set(seeds)
        seed_list = sorted(seeds)

        # Direct dependents over blocking edges.
        rows = await conn.execute(
            select(task_dependencies.c.task_id).where(
                and_(
                    task_dependencies.c.depends_on_task_id.in_(seed_list),
                    task_dependencies.c.dep_type.in_(sorted(BLOCKING_DEP_TYPES)),
                )
            )
        )
        affected.update(r[0] for r in rows.fetchall())

        # Fan-in waiters: containers the seeds are children of, then the
        # `waits-for` edges pointing at those containers.
        rows = await conn.execute(
            select(task_dependencies.c.depends_on_task_id).where(
                and_(
                    task_dependencies.c.task_id.in_(seed_list),
                    task_dependencies.c.dep_type == DepType.PARENT_CHILD.value,
                )
            )
        )
        containers = sorted({r[0] for r in rows.fetchall()})
        if containers:
            rows = await conn.execute(
                select(task_dependencies.c.task_id).where(
                    and_(
                        task_dependencies.c.depends_on_task_id.in_(containers),
                        task_dependencies.c.dep_type == DepType.WAITS_FOR.value,
                    )
                )
            )
            affected.update(r[0] for r in rows.fetchall())

        return affected

    # -- post-commit events ---------------------------------------------

    async def log_blocked_flips(self, flipped: set[str]) -> None:
        """Write ``task.blocked`` / ``task.unblocked`` for flipped rows.

        Called by mutating methods *after* their transaction commits, so the
        audit log never claims a flip that was rolled back.  Best-effort:
        a logging failure must not fail the mutation that caused it.

        Read **and** write happen in one transaction with a single
        ``executemany`` insert.  Calling ``log_event`` per row would open one
        write transaction each, which dominates the whole recompute: on a
        10 000-task chain, 1 000 flips cost 13.9 s that way versus 0.06 s
        batched, against a 0.38 s recompute ``UPDATE`` (spec §11 budgets the
        full backfill at < 5 s, and ``recompute_all_blocked`` is the
        ``aq doctor`` repair path).

        Callers that bypass ``_apply_transition`` (``add_dependency``,
        ``remove_dependency``, ``resolve_gate``, ``remove_all_dependencies_on``,
        ``recompute_all_blocked``) are responsible for their own ``task.ready``
        frontier bookkeeping via ``_note_frontier_entry`` inside the same
        transaction that computed ``flipped`` — this method only ever writes
        ``task.blocked`` / ``task.unblocked``.
        """
        if not flipped:
            return
        now = time.time()
        try:
            async with self._engine.begin() as conn:
                rows = (
                    await conn.execute(
                        select(tasks.c.id, tasks.c.project_id, tasks.c.is_blocked).where(
                            tasks.c.id.in_(sorted(flipped))
                        )
                    )
                ).fetchall()
                if not rows:
                    return
                await conn.execute(
                    insert(events),
                    [
                        {
                            "event_type": "task.blocked" if is_blocked else "task.unblocked",
                            "project_id": project_id,
                            "task_id": task_id,
                            "agent_id": None,
                            "payload": "graph",
                            "timestamp": now,
                        }
                        for task_id, project_id, is_blocked in rows
                    ],
                )
        except Exception:  # pragma: no cover — defensive
            logger.debug("log_blocked_flips: could not write flip events", exc_info=True)

    # -- read side -------------------------------------------------------

    async def get_blocked_map(self, task_ids: list[str] | None = None) -> dict[str, bool]:
        """Return ``{task_id: is_blocked}`` — the persisted projection."""
        stmt = select(tasks.c.id, tasks.c.is_blocked)
        if task_ids is not None:
            if not task_ids:
                return {}
            stmt = stmt.where(tasks.c.id.in_(sorted(task_ids)))
        async with self._engine.begin() as conn:
            rows = (await conn.execute(stmt)).fetchall()
        return {r[0]: bool(r[1]) for r in rows}

    async def evaluate_blocked(self, task_ids: list[str] | None = None) -> dict[str, bool]:
        """Evaluate the predicate live, without writing.

        The brute-force reference used by ``aq doctor`` drift checks and by
        the property test that compares incremental recompute against full
        evaluation.
        """
        stmt = select(tasks.c.id, case((blocked_predicate(), 1), else_=0))
        if task_ids is not None:
            if not task_ids:
                return {}
            stmt = stmt.where(tasks.c.id.in_(sorted(task_ids)))
        async with self._engine.begin() as conn:
            rows = (await conn.execute(stmt)).fetchall()
        return {r[0]: bool(r[1]) for r in rows}

    async def recompute_all_blocked(self) -> set[str]:
        """Recompute the projection for **every** task in one statement.

        The backfill/repair entry point (``aq doctor``, the data migration's
        Python-side twin).  Returns the ids that were stale.
        """
        async with self._engine.begin() as conn:
            before = {
                r[0]: r[1]
                for r in (
                    await conn.execute(
                        select(tasks.c.id, tasks.c.is_blocked)
                        .order_by(tasks.c.id)
                        .with_for_update(key_share=True)
                    )
                ).fetchall()
            }
            await conn.execute(
                update(tasks)
                .where(tasks.c.id.in_(sorted(before)))
                .values(is_blocked=case((blocked_predicate(), 1), else_=0))
            )
            after = {
                r[0]: r[1]
                for r in (await conn.execute(select(tasks.c.id, tasks.c.is_blocked))).fetchall()
            }
            flipped = {tid for tid, old in before.items() if after.get(tid) != old}
            ready_ids = await self._note_frontier_entry(conn, set(flipped), reason="unblocked")
        await self.log_blocked_flips(flipped)
        await self._notify_ready([(tid, "unblocked") for tid in ready_ids])
        return flipped

    async def recompute_delivery_dependents(self, task_ids) -> set[str]:
        """Recompute the blocked ``delivery-of`` dependents of *task_ids*.

        The delivery observer's hook: a landing changes no row the dependent's
        other recomputes are seeded from, so the observer seeds them when it
        sees a target delivered.  Returns the ids that flipped.
        """
        ids = sorted(set(task_ids))
        if not ids:
            return set()
        async with self._engine.begin() as conn:
            waiters = set(
                (
                    await conn.execute(
                        select(task_dependencies.c.task_id)
                        .select_from(
                            task_dependencies.join(
                                tasks, tasks.c.id == task_dependencies.c.task_id
                            )
                        )
                        .where(
                            task_dependencies.c.depends_on_task_id.in_(ids),
                            task_dependencies.c.dep_type == DepType.DELIVERY_OF.value,
                            tasks.c.is_blocked == 1,
                        )
                    )
                ).scalars()
            )
            if not waiters:
                return set()
            flipped = await self.recompute_blocked(waiters, conn=conn)
            ready_ids = await self._note_frontier_entry(conn, set(flipped), reason="unblocked")
        await self.log_blocked_flips(flipped)
        await self._notify_ready([(tid, "unblocked") for tid in ready_ids])
        return flipped

    async def delivery_failures(self, task_ids) -> dict[str, str]:
        """The newest failed landing of each of *task_ids* that is still current.

        ``task_id -> reason`` from a ``failed`` root receipt recorded no
        earlier than the task's latest completion, for explain.
        """
        ids = sorted(set(task_ids))
        if not ids:
            return {}
        completion = task_completion_records.alias()
        latest_completion = (
            select(func.max(completion.c.completed_at))
            .where(completion.c.task_id == task_delivery_receipts.c.source_task_id)
            .scalar_subquery()
        )
        async with self._engine.begin() as conn:
            rows = await conn.execute(
                select(
                    task_delivery_receipts.c.source_task_id,
                    task_delivery_receipts.c.resolution_evidence,
                )
                .where(
                    task_delivery_receipts.c.source_task_id.in_(ids),
                    task_delivery_receipts.c.target_task_id.is_(None),
                    task_delivery_receipts.c.disposition == "failed",
                    task_delivery_receipts.c.created_at
                    >= func.coalesce(latest_completion, 0.0),
                )
                .order_by(
                    task_delivery_receipts.c.created_at.desc(),
                    task_delivery_receipts.c.id.desc(),
                )
            )
            failures: dict[str, str] = {}
            for task_id, evidence in rows.fetchall():
                failures.setdefault(task_id, _failure_reason(evidence))
        return failures

    async def tasks_with_graph_blockers(self, task_ids: list[str]) -> set[str]:
        """Of *task_ids*, which carry at least one blocking edge or gate?

        Used by the BLOCKED-recovery rule: a BLOCKED task is promoted when
        its projection clears **and** it had a graph reason to be blocked in
        the first place.  Failure-BLOCKED tasks (no edges, no gates) stay
        put, preserving today's behavior (design §4.4).
        """
        if not task_ids:
            return set()
        ordered = sorted(set(task_ids))
        async with self._engine.begin() as conn:
            rows = (
                await conn.execute(
                    select(task_dependencies.c.task_id)
                    .where(
                        and_(
                            task_dependencies.c.task_id.in_(ordered),
                            task_dependencies.c.dep_type.in_(sorted(BLOCKING_DEP_TYPES)),
                        )
                    )
                    .distinct()
                )
            ).fetchall()
            found = {r[0] for r in rows}
            rows = (
                await conn.execute(
                    select(task_gates.c.task_id).where(task_gates.c.task_id.in_(ordered)).distinct()
                )
            ).fetchall()
            found |= {r[0] for r in rows}
        return found

    async def get_ready_frontier(
        self,
        project_id: str,
        *,
        labels: list[str] | None = None,
        any_label: list[str] | None = None,
    ) -> list[Task]:
        """Structurally ready tasks (design §9.2).

        Development callers evaluate dynamic delivery admission separately;
        this SQL query never treats delivery rows as git authority.

        ``status = READY ∧ is_blocked = 0 ∧ no hold:* label ∧ no ingestion
        batch hold``, ordered ``(priority, created_at)``.  ``labels`` is all-of, ``any_label`` is
        any-of; both are AND-ed with each other.
        """
        stmt = select(tasks).where(
            and_(
                tasks.c.project_id == project_id,
                tasks.c.status == TaskStatus.READY.value,
                tasks.c.is_blocked == 0,
                not_ingest_held(),
            )
        )
        stmt = apply_label_filters(stmt, labels=labels, any_label=any_label, exclude_hold=True)
        stmt = stmt.order_by(tasks.c.priority.asc(), tasks.c.created_at.asc())
        async with self._engine.begin() as conn:
            result = await conn.execute(stmt)
            return [self._row_to_task(r) for r in result.mappings().fetchall()]

    # -- conditional edge disposal (design §3.1) --------------------------

    async def find_dead_conditional_tasks(self) -> list[tuple[str, str]]:
        """Return ``(task_id, project_id)`` for tasks whose only remaining
        blocking reason is a ``conditional-blocks`` edge that can never fire.

        A conditional edge becomes permanently unsatisfiable once its
        dependency reaches COMPLETED: the contingency will never be needed.
        Such a task is *dead* when every other blocking edge and gate is
        already satisfied — otherwise it is still legitimately waiting.

        Only non-terminal, not-yet-started tasks qualify; anything ASSIGNED
        or later is left alone.
        """
        # DEFINED/READY only.  BLOCKED is terminal in the state machine and
        # reachable only via failure or admin action; auto-completing such a
        # task would erase a failure record, and an unsatisfiable conditional
        # edge keeps a contingency in DEFINED rather than moving it to
        # BLOCKED in the first place.
        eligible = (
            TaskStatus.DEFINED.value,
            TaskStatus.READY.value,
        )
        # Dead conditional edge: dep is COMPLETED.
        dd = task_dependencies.alias("wg_dd")
        dt = tasks.alias("wg_dt")
        has_dead_conditional = (
            select(literal(1))
            .select_from(dd.join(dt, dt.c.id == dd.c.depends_on_task_id))
            .where(
                and_(
                    dd.c.task_id == tasks.c.id,
                    dd.c.dep_type == DepType.CONDITIONAL_BLOCKS.value,
                    dt.c.status == TaskStatus.COMPLETED.value,
                )
            )
            .exists()
        )
        # Everything else must already be satisfied.  Reuse the shared
        # predicate with the conditional clause neutralised by asking for
        # "no *other* blocking reason": we express that as "the task would be
        # unblocked if its dead conditional edges were satisfied", which is
        # exactly `not blocked_predicate()` evaluated over a graph where the
        # conditional dep is COMPLETED — i.e. every non-conditional clause
        # false and every conditional dep COMPLETED.
        other_blockers = _blocked_ignoring_conditional()

        stmt = (
            select(tasks.c.id, tasks.c.project_id)
            .where(
                and_(
                    tasks.c.status.in_(eligible),
                    has_dead_conditional,
                    not_(other_blockers),
                )
            )
            .order_by(tasks.c.created_at.asc())
        )
        async with self._engine.begin() as conn:
            rows = (await conn.execute(stmt)).fetchall()
        return [(r[0], r[1]) for r in rows]


def _blocked_ignoring_conditional():
    """The blocked predicate with *all* ``conditional-blocks`` edges treated
    as satisfied — "is anything other than a conditional edge holding this
    task back?".

    Used only by :meth:`BlockedStateMixin.find_dead_conditional_tasks`.  It is
    :func:`blocked_predicate` minus one clause, built from the same factories,
    so the two cannot drift.  ``TestConditionalDisposal`` guards both
    directions: the two expressions select identical rows on graphs with no
    ``conditional-blocks`` edges, and this one is a subset of the full
    predicate on graphs that carry them.
    """
    return or_(
        _blocks_unsat(),
        _parent_child_unsat(),
        _waits_for_unsat(),
        _delivery_of_unsat(),
        _gate_open(),
    )


def apply_label_filters(stmt, *, labels=None, any_label=None, exclude_hold=False):
    """Add ``task_labels`` filters to a ``SELECT ... FROM tasks`` statement.

    ``labels`` is all-of (one correlated EXISTS per label so the semantics
    survive multi-row joins), ``any_label`` is any-of, and ``exclude_hold``
    drops rows carrying a ``hold:*`` label (design §6).
    """
    if labels:
        for idx, label in enumerate(labels):
            tl = task_labels.alias(f"lbl_all_{idx}")
            stmt = stmt.where(
                select(literal(1))
                .select_from(tl)
                .where(and_(tl.c.task_id == tasks.c.id, tl.c.label == label))
                .exists()
            )
    if any_label:
        tl = task_labels.alias("lbl_any")
        stmt = stmt.where(
            select(literal(1))
            .select_from(tl)
            .where(and_(tl.c.task_id == tasks.c.id, tl.c.label.in_(sorted(set(any_label)))))
            .exists()
        )
    if exclude_hold:
        th = task_labels.alias("lbl_hold")
        stmt = stmt.where(
            not_(
                select(literal(1))
                .select_from(th)
                .where(
                    and_(
                        th.c.task_id == tasks.c.id,
                        th.c.label.like(f"{HOLD_LABEL_PREFIX}%"),
                    )
                )
                .exists()
            )
        )
    return stmt
