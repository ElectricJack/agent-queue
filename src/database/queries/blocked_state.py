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

from sqlalchemy import and_, case, literal, not_, or_, select, update

from src.database.tables import (
    gates,
    task_dependencies,
    task_gates,
    task_labels,
    tasks,
)
from src.models import BLOCKING_DEP_TYPES, DepType, HOLD_LABEL_PREFIX, Task, TaskStatus

logger = logging.getLogger(__name__)

__all__ = [
    "BLOCKING_DEP_TYPES",
    "BlockedStateMixin",
    "apply_label_filters",
    "blocked_predicate",
]


# Statuses of a ``parent-child`` container that keep its children withheld.
# Anything else counts as "released" (design §3.1).
_WITHHOLDING_PARENT_STATUSES = (
    TaskStatus.DEFINED.value,
    TaskStatus.AWAITING_PLAN_APPROVAL.value,
)

# Safety valve for the fixpoint driver (implementation spec §3.2).
_MAX_WAVES = 10_000


def blocked_predicate():
    """Return the SQL boolean expression for "this ``tasks`` row is blocked".

    Correlates against the ``tasks`` table itself, so it can be dropped into
    a ``CASE`` inside an ``UPDATE tasks`` or into a ``SELECT ... WHERE``.
    Built from ``EXISTS`` subqueries only — identical SQL on SQLite and
    PostgreSQL.

    One clause per blocking rule, in the order of design §3.1.
    """
    completed = TaskStatus.COMPLETED.value

    # 1. `blocks` — satisfied when the dependency is COMPLETED.
    bd = task_dependencies.alias("wg_bd")
    bt = tasks.alias("wg_bt")
    blocks_unsat = (
        select(literal(1))
        .select_from(bd.join(bt, bt.c.id == bd.c.depends_on_task_id))
        .where(
            and_(
                bd.c.task_id == tasks.c.id,
                bd.c.dep_type == DepType.BLOCKS.value,
                bt.c.status != completed,
            )
        )
        .exists()
    )

    # 2. `parent-child` — satisfied once the container has been released.
    pd = task_dependencies.alias("wg_pd")
    pt = tasks.alias("wg_pt")
    parent_unsat = (
        select(literal(1))
        .select_from(pd.join(pt, pt.c.id == pd.c.depends_on_task_id))
        .where(
            and_(
                pd.c.task_id == tasks.c.id,
                pd.c.dep_type == DepType.PARENT_CHILD.value,
                pt.c.status.in_(_WITHHOLDING_PARENT_STATUSES),
            )
        )
        .exists()
    )

    # 3. `waits-for` — dynamic fan-in over the container's children.
    #    Unsatisfied while any `parent-child` child of the container is not
    #    COMPLETED.  Vacuously satisfied when the container has no children.
    wd = task_dependencies.alias("wg_wd")
    pc = task_dependencies.alias("wg_pc")
    ch = tasks.alias("wg_ch")
    open_child = (
        select(literal(1))
        .select_from(pc.join(ch, ch.c.id == pc.c.task_id))
        .where(
            and_(
                pc.c.dep_type == DepType.PARENT_CHILD.value,
                pc.c.depends_on_task_id == wd.c.depends_on_task_id,
                ch.c.status != completed,
            )
        )
        .exists()
    )
    waits_unsat = (
        select(literal(1))
        .select_from(wd)
        .where(
            and_(
                wd.c.task_id == tasks.c.id,
                wd.c.dep_type == DepType.WAITS_FOR.value,
                open_child,
            )
        )
        .exists()
    )

    # 4. `conditional-blocks` — satisfied only on *terminal* failure of the
    #    dependency.  A transiently FAILED task about to be retried does not
    #    satisfy it.
    cd = task_dependencies.alias("wg_cd")
    ct = tasks.alias("wg_ct")
    terminal_failure = or_(
        ct.c.status == TaskStatus.BLOCKED.value,
        and_(
            ct.c.status == TaskStatus.FAILED.value,
            ct.c.retry_count >= ct.c.max_retries,
        ),
    )
    conditional_unsat = (
        select(literal(1))
        .select_from(cd.join(ct, ct.c.id == cd.c.depends_on_task_id))
        .where(
            and_(
                cd.c.task_id == tasks.c.id,
                cd.c.dep_type == DepType.CONDITIONAL_BLOCKS.value,
                not_(terminal_failure),
            )
        )
        .exists()
    )

    # 5. Gates — an attached gate blocks until it is `resolved`.  `expired`
    #    keeps blocking on purpose: a timed-out approval must never
    #    silently self-approve (design §5.4).
    tg = task_gates.alias("wg_tg")
    gt = gates.alias("wg_gt")
    gate_open = (
        select(literal(1))
        .select_from(tg.join(gt, gt.c.id == tg.c.gate_id))
        .where(and_(tg.c.task_id == tasks.c.id, gt.c.status != "resolved"))
        .exists()
    )

    return or_(blocks_unsat, parent_unsat, waits_unsat, conditional_unsat, gate_open)


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
        cascade — drive the fixpoint by re-seeding with the tasks whose
        status changed; :meth:`recompute_blocked_fixpoint` does that.
        """
        if not seed_task_ids:
            return set()

        affected = await self._collect_affected(seed_task_ids, conn)
        if not affected:
            return set()

        # Canonical (sorted) id order so concurrent PostgreSQL transactions
        # acquire row locks in the same sequence — same deadlock-freedom
        # discipline as workspace acquisition.
        ordered = sorted(affected)

        before = {
            r[0]: r[1]
            for r in (
                await conn.execute(
                    select(tasks.c.id, tasks.c.is_blocked).where(tasks.c.id.in_(ordered))
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

    async def recompute_blocked_fixpoint(
        self, seed_task_ids: set[str], *, conn, extra_waves: list[set[str]] | None = None
    ) -> set[str]:
        """Loop :meth:`recompute_blocked` until no value changes.

        Used by callers that mutate several statuses inside one transaction.
        ``extra_waves`` seeds additional rounds (e.g. tasks auto-closed by
        the conditional cascade).  Bounded by ``_MAX_WAVES`` as a safety
        valve — the loop can only shrink the frontier in practice.
        """
        pending = set(seed_task_ids)
        queued = list(extra_waves or [])
        flipped_all: set[str] = set()
        waves = 0
        while pending:
            waves += 1
            if waves > _MAX_WAVES:  # pragma: no cover — safety valve
                raise RuntimeError(
                    f"recompute_blocked_fixpoint exceeded {_MAX_WAVES} waves; "
                    "the affected set is not converging"
                )
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
        """
        if not flipped:
            return
        try:
            async with self._engine.begin() as conn:
                rows = (
                    await conn.execute(
                        select(tasks.c.id, tasks.c.project_id, tasks.c.is_blocked).where(
                            tasks.c.id.in_(sorted(flipped))
                        )
                    )
                ).fetchall()
        except Exception:  # pragma: no cover — defensive
            logger.debug("log_blocked_flips: could not read flipped rows", exc_info=True)
            return

        for task_id, project_id, is_blocked in rows:
            try:
                await self.log_event(
                    "task.blocked" if is_blocked else "task.unblocked",
                    project_id=project_id,
                    task_id=task_id,
                    payload="graph",
                )
            except Exception:  # pragma: no cover — defensive
                logger.debug("log_blocked_flips: log_event failed for %s", task_id, exc_info=True)

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
                for r in (await conn.execute(select(tasks.c.id, tasks.c.is_blocked))).fetchall()
            }
            await conn.execute(
                update(tasks).values(is_blocked=case((blocked_predicate(), 1), else_=0))
            )
            after = {
                r[0]: r[1]
                for r in (await conn.execute(select(tasks.c.id, tasks.c.is_blocked))).fetchall()
            }
        flipped = {tid for tid, old in before.items() if after.get(tid) != old}
        await self.log_blocked_flips(flipped)
        return flipped

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
        """Tasks that would be picked next (design §9.2).

        ``status = READY ∧ is_blocked = 0 ∧ no hold:* label``, ordered
        ``(priority, created_at)``.  ``labels`` is all-of, ``any_label`` is
        any-of; both are AND-ed with each other.
        """
        stmt = select(tasks).where(
            and_(
                tasks.c.project_id == project_id,
                tasks.c.status == TaskStatus.READY.value,
                tasks.c.is_blocked == 0,
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

    Used only by :meth:`BlockedStateMixin.find_dead_conditional_tasks`.  It
    restates the other four clauses of :func:`blocked_predicate` (correlated
    ``EXISTS`` subqueries cannot be composed out of a shared ``or_`` without
    dropping one term).  ``TestConditionalDisposal.
    test_the_two_predicates_agree_without_conditional_edges`` is the drift
    guard: on random graphs carrying no ``conditional-blocks`` edges the two
    expressions must select exactly the same rows.
    """
    completed = TaskStatus.COMPLETED.value

    bd = task_dependencies.alias("wg2_bd")
    bt = tasks.alias("wg2_bt")
    blocks_unsat = (
        select(literal(1))
        .select_from(bd.join(bt, bt.c.id == bd.c.depends_on_task_id))
        .where(
            and_(
                bd.c.task_id == tasks.c.id,
                bd.c.dep_type == DepType.BLOCKS.value,
                bt.c.status != completed,
            )
        )
        .exists()
    )

    pd = task_dependencies.alias("wg2_pd")
    pt = tasks.alias("wg2_pt")
    parent_unsat = (
        select(literal(1))
        .select_from(pd.join(pt, pt.c.id == pd.c.depends_on_task_id))
        .where(
            and_(
                pd.c.task_id == tasks.c.id,
                pd.c.dep_type == DepType.PARENT_CHILD.value,
                pt.c.status.in_(_WITHHOLDING_PARENT_STATUSES),
            )
        )
        .exists()
    )

    wd = task_dependencies.alias("wg2_wd")
    pc = task_dependencies.alias("wg2_pc")
    ch = tasks.alias("wg2_ch")
    open_child = (
        select(literal(1))
        .select_from(pc.join(ch, ch.c.id == pc.c.task_id))
        .where(
            and_(
                pc.c.dep_type == DepType.PARENT_CHILD.value,
                pc.c.depends_on_task_id == wd.c.depends_on_task_id,
                ch.c.status != completed,
            )
        )
        .exists()
    )
    waits_unsat = (
        select(literal(1))
        .select_from(wd)
        .where(
            and_(
                wd.c.task_id == tasks.c.id,
                wd.c.dep_type == DepType.WAITS_FOR.value,
                open_child,
            )
        )
        .exists()
    )

    tg = task_gates.alias("wg2_tg")
    gt = gates.alias("wg2_gt")
    gate_open = (
        select(literal(1))
        .select_from(tg.join(gt, gt.c.id == tg.c.gate_id))
        .where(and_(tg.c.task_id == tasks.c.id, gt.c.status != "resolved"))
        .exists()
    )

    return or_(blocks_unsat, parent_unsat, waits_unsat, gate_open)


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
