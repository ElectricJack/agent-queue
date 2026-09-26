"""Hierarchy — the single writer for parent/child membership (spec Part I).

Truth is the ``parent-child`` edge; ``tasks.parent_task_id`` is a derived
cache that only :meth:`HierarchyQueryMixin.set_parent` writes, in the same
transaction as the edge, the blocked-state recompute and container
settlement.  Every mutation here takes ``conn`` and never opens its own
transaction.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import (
    and_,
    case,
    delete,
    exists,
    false,
    func,
    insert,
    literal,
    or_,
    select,
    true,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.database.queries.task_queries import INTEGRATION_REWORK_AT_KEY, TransitionResult
from src.database.tables import (
    agents,
    integration_batch_members,
    integration_batches,
    integration_branch_owners,
    integration_repair_operations,
    integration_repair_stages,
    projects,
    sessions,
    task_branch_origins,
    task_delivery_receipts,
    task_dependencies,
    task_integration_checkpoints,
    task_metadata,
    tasks,
    workspaces,
)
from src.models import AgentState, DepType, Task, TaskStatus
from src.task_names import MAX_STRUCTURAL_DEPTH, child_task_id

# Container statuses that withhold their children (work-graph §3.1) are
# enforced by BlockedStateMixin's satisfaction table
# (``_WITHHOLDING_PARENT_STATUSES`` in blocked_state.py) — this module only
# needs the terminal ``COMPLETED`` check for ``container_closed``.

CONTAINER_KEY = "container"
CONTAINER_VALUE = "true"  # json.dumps(True); matches set_task_meta's encoding
#: ``task_metadata`` key a phase container carries (graph-visibility A1).
#: Its presence, not its value, is what :func:`childless_held_open_container`
#: keys off.
PHASE_KEY = "phase"
#: A phase header exposes at most this many failed children.  The full task
#: tree remains available through the ordinary hierarchy reads; a single
#: phase-list/tiles response must not grow without bound with an unhealthy
#: phase.
PHASE_HOLD_CHILD_LIMIT = 20
#: These metadata rows distinguish a terminal failure in ``BLOCKED`` from an
#: ordinary dependency-blocked child.  ``FAILED`` is terminal by status alone.
_PHASE_FAILURE_META_KEYS = ("blocked_terminal", "needs_attention")
#: ``task_metadata`` key a keyed standing parent carries (graph-visibility A2).
#: Written by ``_resolve_standing_parent`` in the same transaction as the
#: container flag; its presence is the second half of
#: :func:`childless_held_open_container`.
STANDING_PARENT_KEY = "standing_parent"
#: Every ``task_metadata`` key that holds a childless container open.  A
#: container carrying any of these is created *before* the work it will hold,
#: so it must survive the window in which it has no children at all.
HELD_OPEN_CONTAINER_KEYS = (PHASE_KEY, STANDING_PARENT_KEY)
#: Two-key PostgreSQL advisory-lock namespace "AQHI" (AQ hierarchy).
HIERARCHY_LOCK_NAMESPACE = 0x41514849
#: Bounds the recursive walk on an already-cyclic graph; far above any real
#: blocking chain. Blocking edges (waits-for / blocks / conditional-blocks)
#: have no structural depth bound, unlike parent-child nesting, so this must
#: not be derived from MAX_STRUCTURAL_DEPTH.
REACHABILITY_MAX_DEPTH = 10_000


def container_flag_exists():
    """``EXISTS`` clause: the correlated ``tasks`` row carries the container flag.

    The §7 flag is the *only* thing that says a task is settle-only work
    (``creator.PARENT_STATUS``, recovery and the claim frontier all key off
    it, never off "has children"), so the predicate lives here once.
    """
    return exists(
        select(literal(1)).where(
            and_(
                task_metadata.c.task_id == tasks.c.id,
                task_metadata.c.key == CONTAINER_KEY,
                task_metadata.c.value == CONTAINER_VALUE,
            )
        )
    )


def childless_held_open_container():
    """``WHERE`` clause: the row is a held-open container with no children.

    A *held-open* container — a phase (A1) or a keyed standing parent (A2) —
    is created *before* the work that belongs to it, so it spends a window
    with no children at all.  The §7 settlement predicate below asks "no
    child is un-COMPLETED", which is vacuously true of zero children, and
    would therefore complete such a container the instant the promotion
    cascade released it (``_check_defined_tasks`` promotes an unblocked
    DEFINED task, ``_release_ready_containers`` flips a flagged one straight
    to IN_PROGRESS, and settlement seeds off that) — after which
    ``container_closed`` refuses the very work the container was created to
    hold.  The window is real and unavoidable: the container and its first
    child commit on separate connections, so a crash, a refused child
    creation, or an ordinary 5-second cascade tick can land between them.

    The exclusion is deliberately narrowed to containers that *say* they are
    held open, rather than to every childless container: an ordinary
    container emptied by reparenting its last child away must still settle,
    or an epic whose work moved elsewhere would hang IN_PROGRESS forever
    (``test_emptied_container_settles_on_reparent``).  A phase or a standing
    parent emptied the same way stops settling, which is the same rule read
    the other way round and is asserted explicitly in ``tests/test_phases.py``.

    The two kinds differ in what an abandoned empty one costs.

    **An empty phase must be deleted, not left in place.**  Because it never
    settles, it holds its ``blocks`` edge shut and every later phase with it,
    indefinitely — there is no timeout and no sweep that will clear it.  The
    escape hatch is ``task_delete`` (``aq task delete <phase-id>``): deleting
    the phase removes the edge with it and releases the next phase on the
    following cascade (``test_deleting_an_abandoned_empty_phase_releases_the_next``).

    **An empty standing parent needs no escape hatch.**  It gates nothing, and
    because it stays non-terminal the next ``parent_key`` call resolves it and
    files work into it — an orphan is *reused*, not leaked.  A standing parent
    that did hold children and saw them all complete settles normally, and a
    settled one is then never reused: the next call creates a fresh container
    beside it.

    Shared by :meth:`HierarchyQueryMixin.settle_containers` and
    :meth:`HierarchyQueryMixin.settle_candidates` so the event path and the
    backstop sweep can never disagree about what settles.
    """
    child = tasks.alias()
    return and_(
        exists(
            select(literal(1)).where(
                and_(
                    task_metadata.c.task_id == tasks.c.id,
                    task_metadata.c.key.in_(HELD_OPEN_CONTAINER_KEYS),
                )
            )
        ),
        ~exists(select(literal(1)).where(child.c.parent_task_id == tasks.c.id)),
    )


#: Project ``hierarchical_integration_mode`` values that gate the two claim
#: predicates below.  ``disabled`` (and anything unrecognised) does not.
HIERARCHY_MODES = ("hierarchy", "train")

#: Refusal code both phase doors return in a :data:`HIERARCHY_MODES` project —
#: ``phase_create`` and a graph document declaring ``phases:``.
PHASES_UNSUPPORTED_MODE_CODE = "hierarchy.phases_unsupported_mode"


@dataclass(frozen=True)
class ProjectIntegrationMode:
    """The two per-project constants the hierarchy claim predicates need.

    Both predicates below ask one question of the ``projects`` row —
    "is this project in hierarchy/train mode, and against which
    repository?" — and both used to ask it as a *correlated* subquery keyed
    by ``tasks.project_id``.  In a single-project frontier scan that is one
    ``projects_pkey`` lookup **per candidate row** (2,499 index searches and
    ~5,000 buffer hits at the §15.2 scale) for an answer that is constant
    across the whole scan and that the caller already holds in a Python
    object.  A caller that has read the project row passes this in and the
    subqueries collapse to a constant.

    Callers whose statement spans more than one project (see
    :meth:`HierarchyQueryMixin.hierarchy_runnable_task_ids`) pass ``None``
    and keep the correlated form, which is why both predicates still build
    it.
    """

    hierarchical: bool
    integration_repository_id: str | None

    @classmethod
    def of(cls, project) -> ProjectIntegrationMode | None:
        """Read the mode off a project row; ``None`` when there is no row.

        ``None`` means "ask the database per row" — the safe fallback, not
        "not hierarchical".
        """
        if project is None:
            return None
        return cls(
            hierarchical=getattr(project, "hierarchical_integration_mode", None)
            in HIERARCHY_MODES,
            integration_repository_id=getattr(project, "integration_repository_id", None),
        )


def materialized_origin_when_hierarchical(mode: ProjectIntegrationMode | None = None):
    """Require an exact origin or an active delegate reservation in enabled projects.

    A repair delegate writes its operation's existing branch and a parent
    verifier checks the parent's; neither ever gets an origin of its own, so
    each is admitted only on its exact reserved fence.

    With *mode* supplied the ``projects`` lookup is folded away at compile
    time: a non-hierarchical project admits every task, and a hierarchical
    one with no ``integration_repository_id`` admits none (no origin row can
    equal a NULL repository, so the correlated form rejected them too).
    """
    if mode is not None:
        if not mode.hierarchical:
            return true()
        if mode.integration_repository_id is None:
            return false()
        # The origin arm admits nearly every frontier row, and PostgreSQL
        # evaluates ``OR`` arms in order, so it goes first: the delegate
        # sub-plans run only for the rare row that has no origin.
        return or_(
            exists(select(literal(1)).where(
                task_branch_origins.c.task_id == tasks.c.id,
                task_branch_origins.c.repository_id == mode.integration_repository_id,
                task_branch_origins.c.retired_at.is_(None),
                task_branch_origins.c.materialized.is_(True),
            )),
            _reserved_repair_branch(mode.integration_repository_id),
            _reserved_verifier_branch(mode.integration_repository_id),
        )
    return or_(~exists(
        select(literal(1))
        .select_from(projects)
        .where(
            projects.c.id == tasks.c.project_id,
            projects.c.hierarchical_integration_mode.in_(HIERARCHY_MODES),
            ~exists(
                select(literal(1))
                # SQLAlchemy auto-correlates only against the *immediately*
                # enclosing SELECT, whose FROM is ``projects`` alone.  Without
                # this, ``tasks`` joins this subquery's own FROM and the
                # predicate silently asks "does *any* task have a materialized
                # origin", which admits an unmaterialized child as soon as one
                # sibling materialises.  Correlate both levels explicitly.
                .correlate(tasks, projects)
                .where(
                    task_branch_origins.c.task_id == tasks.c.id,
                    task_branch_origins.c.repository_id == projects.c.integration_repository_id,
                    task_branch_origins.c.retired_at.is_(None),
                    task_branch_origins.c.materialized.is_(True),
                )
            ),
        )
    ), _reserved_repair_branch(), _reserved_verifier_branch())


def _reserved_repair_branch(repository_id: str | None = None):
    """A repair writes its operation's existing branch, not a new task origin."""
    operation = integration_repair_operations
    stage = integration_repair_stages
    owner = integration_branch_owners
    source = stage.join(operation, operation.c.id == stage.c.operation_id).join(
        owner, owner.c.owner_id == stage.c.repair_task_id,
    )
    if repository_id is None:
        source = source.join(projects, projects.c.id == tasks.c.project_id)
    return exists(
        select(literal(1))
        .select_from(source)
        .correlate(tasks)
        .where(
            tasks.c.created_by_kind == "integration_repair",
            tasks.c.created_by_id == operation.c.id,
            stage.c.repair_task_id == tasks.c.id,
            stage.c.writer_kind == "repair_delegate",
            stage.c.ordinal == operation.c.active_stage,
            stage.c.state.in_(("active", "awaiting_completion")),
            operation.c.state.in_(("active", "escalated")),
            owner.c.owner_role == "repair",
            owner.c.handoff_state == "reserved",
            owner.c.session_id.is_(None),
            owner.c.workspace_id.is_(None),
            owner.c.repository_id == tasks.c.repo_id,
            or_(
                owner.c.ref == tasks.c.branch_name,
                owner.c.ref == ("refs/heads/" + tasks.c.branch_name),
            ),
            owner.c.repository_id == (
                repository_id if repository_id is not None else projects.c.integration_repository_id
            ),
        )
    )


def _reserved_verifier_branch(repository_id: str | None = None):
    """A parent verifier checks the parent's branch, not a new task origin.

    ``ParentCompletion`` files the delegate on the parent's checkpoint branch
    and binds it as ``verifier_task_id``; the handoff then reserves that
    branch's fence to it.  Admit exactly that: the bound delegate of a live
    parent operation, holding the unattached ``verifier`` fence on the
    operation's own parent branch.
    """
    operation = integration_repair_operations
    owner = integration_branch_owners
    parent = tasks.alias("verifier_parent")
    source = operation.join(owner, owner.c.owner_id == operation.c.verifier_task_id).join(
        parent, parent.c.id == operation.c.parent_task_id,
    )
    if repository_id is None:
        source = source.join(projects, projects.c.id == tasks.c.project_id)
    return exists(
        select(literal(1))
        .select_from(source)
        .correlate(tasks)
        .where(
            operation.c.verifier_task_id == tasks.c.id,
            operation.c.target_kind == "parent",
            operation.c.state.in_(("active", "escalated")),
            parent.c.project_id == tasks.c.project_id,
            owner.c.owner_role == "verifier",
            owner.c.handoff_state == "reserved",
            owner.c.session_id.is_(None),
            owner.c.workspace_id.is_(None),
            owner.c.repository_id == tasks.c.repo_id,
            owner.c.ref == tasks.c.branch_name,
            owner.c.repository_id == parent.c.repo_id,
            owner.c.ref == parent.c.branch_name,
            owner.c.repository_id == (
                repository_id if repository_id is not None else projects.c.integration_repository_id
            ),
        )
    )


def integration_rework_cutoff(task_id):
    """The only task timestamp that invalidates an earlier delivery receipt."""
    # Claim queries import the hierarchy predicates, so defer this import
    # until the predicate is built rather than creating a module cycle.
    from src.database.queries.claim_queries import numeric_meta_value

    return (
        select(numeric_meta_value(task_metadata.c.value))
        .where(
            task_metadata.c.task_id == task_id,
            task_metadata.c.key == INTEGRATION_REWORK_AT_KEY,
        )
        .scalar_subquery()
    )


def delivered_same_parent_prerequisites_when_hierarchical(
    mode: ProjectIntegrationMode | None = None,
):
    """Require a direct sibling prerequisite to reach the shared parent first.

    Graph blockedness intentionally releases a ``blocks`` dependent when its
    predecessor completes.  In a hierarchy project that is too early: the
    predecessor's reviewed head still belongs to its feature branch until a
    receipt proves it was incorporated into their common parent branch.

    *mode*, when supplied, folds the two ``projects`` lookups away exactly as
    in :func:`materialized_origin_when_hierarchical`.
    """
    if mode is not None and not mode.hierarchical:
        return true()
    dependency = task_dependencies.alias("hierarchy_prerequisite")
    prerequisite = tasks.alias("hierarchy_prerequisite_task")
    parent = tasks.alias("hierarchy_prerequisite_parent")
    checkpoint = task_integration_checkpoints.alias("hierarchy_prerequisite_checkpoint")
    receipt = task_delivery_receipts.alias("hierarchy_prerequisite_receipt")
    origin = task_branch_origins.alias("hierarchy_prerequisite_origin")

    delivered = exists(
        select(literal(1))
        # As above: ``tasks`` is two levels out, so name every correlated
        # FROM explicitly (``correlate`` replaces auto-correlation).
        .correlate(tasks, prerequisite, parent, checkpoint)
        .where(
            receipt.c.source_task_id == prerequisite.c.id,
            receipt.c.target_task_id == tasks.c.parent_task_id,
            receipt.c.repository_id == checkpoint.c.repository_id,
            receipt.c.target_branch == parent.c.branch_name,
            receipt.c.disposition == "code",
            receipt.c.reviewed_head_sha == checkpoint.c.checkpoint_sha,
            # Ordinary task bookkeeping and the close record may be written
            # after delivery.  Only entry into a new work incarnation makes
            # an old receipt stale.
            receipt.c.created_at >= func.coalesce(
                integration_rework_cutoff(prerequisite.c.id), prerequisite.c.created_at
            ),
        )
    )
    prerequisite_is_undelivered = exists(
        select(literal(1))
        .select_from(
            dependency.join(prerequisite, prerequisite.c.id == dependency.c.depends_on_task_id)
            .join(parent, parent.c.id == tasks.c.parent_task_id)
            .outerjoin(checkpoint, checkpoint.c.task_id == prerequisite.c.id)
        )
        .where(
            dependency.c.task_id == tasks.c.id,
            dependency.c.dep_type == DepType.BLOCKS.value,
            tasks.c.parent_task_id.is_not(None),
            prerequisite.c.parent_task_id == tasks.c.parent_task_id,
            prerequisite.c.status == TaskStatus.COMPLETED.value,
            ~delivered,
        )
    )
    preserved_parent_origin = exists(
        select(literal(1))
        .correlate(tasks, parent)
        .where(
            origin.c.task_id == tasks.c.id,
            origin.c.retired_at.is_(None),
            origin.c.parent_task_id == tasks.c.parent_task_id,
            origin.c.parent_ref == parent.c.branch_name,
        )
    )
    if mode is not None:
        # ``mode.hierarchical`` is true here (the other branch returned
        # above), so the project row is a known constant: both ``exists``
        # clauses reduce to their non-``projects`` remainder.
        return ~exists(
            select(literal(1))
            .select_from(parent)
            .where(
                parent.c.id == tasks.c.parent_task_id,
                tasks.c.parent_task_id.is_not(None),
                ~preserved_parent_origin,
            )
        ) & ~prerequisite_is_undelivered
    return ~exists(
        select(literal(1))
        .select_from(projects.join(parent, parent.c.id == tasks.c.parent_task_id))
        .where(
            projects.c.id == tasks.c.project_id,
            projects.c.hierarchical_integration_mode.in_(HIERARCHY_MODES),
            tasks.c.parent_task_id.is_not(None),
            ~preserved_parent_origin,
        )
    ) & (~exists(select(literal(1)).where(
        projects.c.id == tasks.c.project_id,
        projects.c.hierarchical_integration_mode.in_(HIERARCHY_MODES),
    )) | ~prerequisite_is_undelivered)


#: Session states that still hold their task — a container in one of these
#: cannot be settled out from under a live worker (spec §7).
LIVE_SESSION_STATES = ("starting", "running", "draining")


class HierarchyError(Exception):
    """A rejected hierarchy mutation.  ``code`` is the stable machine string."""

    def __init__(self, code: str, detail: str = "", context: dict | None = None):
        self.code = code
        self.detail = detail
        #: Machine-readable specifics a surface can render (e.g. the branches a
        #: ``branch_discard_required`` refusal is asking the operator about).
        self.context = context or {}
        super().__init__(f"{code}: {detail}" if detail else code)


class HierarchyQueryMixin:
    """Expects ``self._engine`` plus BlockedStateMixin and TaskQueryMixin."""

    # -- container flag -------------------------------------------------

    async def guard_hierarchy_bulk_write(self, project_id: str, *, conn) -> None:
        """Reject legacy bulk hierarchy writers for hierarchy-enabled projects."""
        mode = (
            await conn.execute(
                select(projects.c.hierarchical_integration_mode).where(projects.c.id == project_id)
            )
        ).scalar_one_or_none()
        if mode in {"hierarchy", "train"}:
            raise HierarchyError(
                "integration_required",
                "bulk parent writes must use atomic hierarchy filing",
            )

    async def phase_mode_refusal(self, project_id: str, *, conn=None) -> dict | None:
        """The phase refusal for *project_id*, or ``None`` when phases are fine.

        Phases are refused in :data:`HIERARCHY_MODES` and nowhere else.  There
        a phase container owns a branch and its children deliver *to it*, so
        phase *N+1* can open on a base without phase *N*'s work and one FAILED
        child strands the whole stage — the hazard
        ``hierarchy.parent_key_unsupported_mode`` already bars for standing
        parents.  In ``disabled``/``observe``/``development`` a container is a
        plain task row with no branch and phases are safe.

        This is the *one* check behind both doors: ``phase_create`` calls it
        before filing anything, and ``write_plan`` calls it inside the graph's
        transaction so a caller that skipped the command layer cannot bypass
        it.  *conn* joins that transaction instead of opening a connection.
        """
        stmt = select(projects.c.hierarchical_integration_mode).where(
            projects.c.id == project_id
        )
        if conn is not None:
            mode = (await conn.execute(stmt)).scalar_one_or_none()
        else:
            async with self._engine.connect() as owned:
                mode = (await owned.execute(stmt)).scalar_one_or_none()
        if mode not in HIERARCHY_MODES:
            return None
        return {
            "success": False,
            "code": PHASES_UNSUPPORTED_MODE_CODE,
            "error": (
                f"project '{project_id}' delivers hierarchically, where a phase container "
                "would own its children's delivery branch and hold a whole stage's work "
                "off the default branch; order the work with 'aq task add-dependency' "
                "instead"
            ),
        }

    async def hierarchy_runnable_task_ids(self, task_ids: list[str]) -> set[str]:
        """Return tasks whose project mode/origin permits writer assignment."""
        if not task_ids:
            return set()
        async with self._engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        select(tasks.c.id).where(
                            tasks.c.id.in_(task_ids), materialized_origin_when_hierarchical(),
                            delivered_same_parent_prerequisites_when_hierarchical(),
                        )
                    )
                )
                .scalars()
                .all()
            )
        return set(rows)

    async def is_hierarchy_task_runnable(self, task_id: str) -> bool:
        return task_id in await self.hierarchy_runnable_task_ids([task_id])

    async def hierarchy_prerequisite_delivery_head(self, task_id: str) -> str | None:
        """Return the latest exact parent head a sibling-dependent child needs.

        The child origin remains immutable at its filing base.  This is only a
        workspace-start overlay: a delivered parent head is a descendant of
        that base and lets the child's normal branch preparation fast-forward
        from the preserved origin without copying files or merging siblings.
        ``None`` means either no sibling prerequisite exists or the task is
        not currently receipt-eligible (the caller has already fail-closed via
        :meth:`is_hierarchy_task_runnable`).
        """
        dependency = task_dependencies.alias("delivery_head_dependency")
        prerequisite = tasks.alias("delivery_head_prerequisite")
        checkpoint = task_integration_checkpoints.alias("delivery_head_checkpoint")
        receipt = task_delivery_receipts.alias("delivery_head_receipt")
        async with self._engine.connect() as conn:
            task = (
                await conn.execute(select(tasks).where(tasks.c.id == task_id))
            ).mappings().one_or_none()
            if task is None or task["parent_task_id"] is None:
                return None
            parent_branch = (
                await conn.execute(
                    select(tasks.c.branch_name).where(tasks.c.id == task["parent_task_id"])
                )
            ).scalar_one_or_none()
            if not parent_branch:
                return None
            rows = (
                await conn.execute(
                    select(receipt.c.after_sha, receipt.c.created_at)
                    .select_from(
                        dependency.join(
                            prerequisite,
                            prerequisite.c.id == dependency.c.depends_on_task_id,
                        )
                        .join(checkpoint, checkpoint.c.task_id == prerequisite.c.id)
                        .join(
                            receipt,
                            and_(
                                receipt.c.source_task_id == prerequisite.c.id,
                                receipt.c.target_task_id == task["parent_task_id"],
                                receipt.c.repository_id == checkpoint.c.repository_id,
                                receipt.c.target_branch == parent_branch,
                                receipt.c.disposition == "code",
                                receipt.c.reviewed_head_sha == checkpoint.c.checkpoint_sha,
                                receipt.c.created_at >= func.coalesce(
                                    integration_rework_cutoff(prerequisite.c.id),
                                    prerequisite.c.created_at,
                                ),
                            ),
                        )
                    )
                    .where(
                        dependency.c.task_id == task_id,
                        dependency.c.dep_type == DepType.BLOCKS.value,
                        prerequisite.c.parent_task_id == task["parent_task_id"],
                    )
                    .order_by(receipt.c.created_at.desc(), receipt.c.id.desc())
                )
            ).all()
        for head, _created_at in rows:
            if isinstance(head, str) and len(head) == 40:
                return head
        return None

    async def mark_container(self, task_id: str, *, conn) -> None:
        """Set ``task_metadata.container = true`` (idempotent).  Never cleared."""
        ins = pg_insert
        await conn.execute(
            ins(task_metadata)
            .values(task_id=task_id, key=CONTAINER_KEY, value=CONTAINER_VALUE)
            .on_conflict_do_nothing()
        )

    async def is_container(self, task_id: str, *, conn) -> bool:
        row = (
            await conn.execute(
                select(literal(1)).where(
                    and_(
                        task_metadata.c.task_id == task_id,
                        task_metadata.c.key == CONTAINER_KEY,
                        task_metadata.c.value == CONTAINER_VALUE,
                    )
                )
            )
        ).fetchone()
        return row is not None

    # -- structure reads (CTE) -------------------------------------------

    def _ancestor_cte(self, task_id: str):
        base = (
            select(tasks.c.id, tasks.c.parent_task_id, literal(1).label("depth"))
            .where(tasks.c.id == task_id)
            .cte("ancestors", recursive=True)
        )
        parent = tasks.alias("parent")
        rec = select(parent.c.id, parent.c.parent_task_id, (base.c.depth + 1).label("depth")).where(
            parent.c.id == base.c.parent_task_id
        )
        return base.union_all(rec)

    def _descendant_cte(self, root_id: str):
        base = (
            select(tasks.c.id, tasks.c.parent_task_id, literal(1).label("depth"))
            .where(tasks.c.id == root_id)
            .cte("descendants", recursive=True)
        )
        child = tasks.alias("child")
        rec = select(child.c.id, child.c.parent_task_id, (base.c.depth + 1).label("depth")).where(
            child.c.parent_task_id == base.c.id
        )
        return base.union_all(rec)

    async def structural_depth(self, task_id: str, *, conn) -> int:
        """Live parent-child chain length from *task_id* to its root (root = 1)."""
        cte = self._ancestor_cte(task_id)
        row = (
            await conn.execute(select(cte.c.depth).order_by(cte.c.depth.desc()).limit(1))
        ).fetchone()
        return int(row[0]) if row else 0

    async def subtree_height(self, task_id: str, *, conn) -> int:
        """Height of the subtree rooted at *task_id* (leaf = 1)."""
        cte = self._descendant_cte(task_id)
        row = (
            await conn.execute(select(cte.c.depth).order_by(cte.c.depth.desc()).limit(1))
        ).fetchone()
        return int(row[0]) if row else 0

    async def subtree_ids(self, root_id: str, *, conn) -> list[str]:
        """Every id in the subtree, root first, shallow before deep."""
        cte = self._descendant_cte(root_id)
        rows = (await conn.execute(select(cte.c.id).order_by(cte.c.depth, cte.c.id))).fetchall()
        return [r[0] for r in rows]

    async def get_children(
        self,
        parent_id: str,
        *,
        recursive: bool = False,
        status: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Task]:
        """Direct (or recursive) children, ordered by depth then id."""
        if recursive:
            cte = self._descendant_cte(parent_id)
            stmt = (
                select(tasks, cte.c.depth)
                .join(cte, cte.c.id == tasks.c.id)
                .where(cte.c.id != parent_id)
                .order_by(cte.c.depth, tasks.c.id)
            )
        else:
            stmt = select(tasks).where(tasks.c.parent_task_id == parent_id).order_by(tasks.c.id)
        if status:
            stmt = stmt.where(tasks.c.status == status)
        if limit is not None:
            stmt = stmt.limit(limit)
        if offset:
            stmt = stmt.offset(offset)
        async with self._engine.begin() as conn:
            rows = (await conn.execute(stmt)).mappings().fetchall()
        return [self._row_to_task(r) for r in rows]

    async def get_children_summary(self, task_id: str) -> dict | None:
        """One aggregate over the direct children; ``None`` when there are none."""
        s = tasks.c.status
        stmt = select(
            func.count().label("total"),
            func.sum(case((s == TaskStatus.COMPLETED.value, 1), else_=0)).label("done"),
            func.sum(
                case((and_(s == TaskStatus.READY.value, tasks.c.is_blocked == 0), 1), else_=0)
            ).label("ready"),
            func.sum(case((tasks.c.is_blocked == 1, 1), else_=0)).label("blocked"),
            func.sum(
                case(
                    (s.in_((TaskStatus.ASSIGNED.value, TaskStatus.IN_PROGRESS.value)), 1),
                    else_=0,
                )
            ).label("in_progress"),
        ).where(tasks.c.parent_task_id == task_id)
        async with self._engine.begin() as conn:
            row = (await conn.execute(stmt)).mappings().fetchone()
        if not row or not row["total"]:
            return None
        return {k: int(row[k] or 0) for k in ("total", "done", "ready", "blocked", "in_progress")}

    async def get_phase_hold_details(self, phase_ids: list[str]) -> dict[str, dict]:
        """Return additive, bounded strict-hold detail for declared phases.

        Settlement remains the source of truth: a phase releases only once
        every child reaches ``COMPLETED``.  This read makes that strict rule
        legible without persisting a second counter or mistaking an ordinary
        dependency block for a failed worker attempt.  The caller supplies
        known phase ids (from the authoritative ``phase`` metadata read), so
        this helper deliberately does not infer phase membership from a title
        or container flag.
        """
        ids = list(dict.fromkeys(task_id for task_id in phase_ids if task_id))
        if not ids:
            return {}

        child = tasks.alias("phase_hold_child")
        async with self._engine.connect() as conn:
            children = (
                await conn.execute(
                    select(
                        child.c.id,
                        child.c.parent_task_id,
                        child.c.status,
                        child.c.resume_after,
                    )
                    .where(child.c.parent_task_id.in_(ids))
                    .order_by(child.c.parent_task_id, child.c.id)
                )
            ).mappings().all()
            # ``FAILED`` is terminal by status; only a ``BLOCKED`` child
            # needs metadata to distinguish a terminal worker failure from a
            # normal dependency block. Do not add a metadata read to healthy
            # phase-list/tiles responses.
            child_ids = [
                row["id"] for row in children if row["status"] == TaskStatus.BLOCKED.value
            ]
            failure_meta_rows = []
            if child_ids:
                failure_meta_rows = (
                    await conn.execute(
                        select(task_metadata.c.task_id, task_metadata.c.key).where(
                            task_metadata.c.task_id.in_(child_ids),
                            task_metadata.c.key.in_(_PHASE_FAILURE_META_KEYS),
                        )
                    )
                ).mappings().all()

            # One recursive walk counts every not-yet-completed descendant of
            # each phase.  A direct child is a descendant too, which makes the
            # number an honest measure of work still preventing settlement.
            seed = select(
                child.c.id.label("id"),
                child.c.parent_task_id.label("phase_id"),
                child.c.status.label("status"),
            ).where(child.c.parent_task_id.in_(ids))
            descendants = seed.cte("phase_hold_descendants", recursive=True)
            descendant = tasks.alias("phase_hold_descendant")
            descendants = descendants.union_all(
                select(
                    descendant.c.id,
                    descendants.c.phase_id,
                    descendant.c.status,
                ).where(descendant.c.parent_task_id == descendants.c.id)
            )
            counts = (
                await conn.execute(
                    select(
                        descendants.c.phase_id,
                        func.count().label("count"),
                    )
                    .where(descendants.c.status != TaskStatus.COMPLETED.value)
                    .group_by(descendants.c.phase_id)
                )
            ).mappings().all()

        failure_meta = {row["task_id"] for row in failure_meta_rows}
        descendant_counts = {row["phase_id"]: int(row["count"] or 0) for row in counts}
        by_phase: dict[str, list] = {phase_id: [] for phase_id in ids}
        for row in children:
            by_phase[row["parent_task_id"]].append(row)

        details: dict[str, dict] = {}
        for phase_id, phase_children in by_phase.items():
            failed = [
                row
                for row in phase_children
                if row["status"] == TaskStatus.FAILED.value
                or (
                    row["status"] == TaskStatus.BLOCKED.value
                    and row["id"] in failure_meta
                )
            ]
            if failed:
                details[phase_id] = {
                    "phase_id": phase_id,
                    "failed_children": [
                        {"id": row["id"], "status": row["status"]}
                        for row in failed[:PHASE_HOLD_CHILD_LIMIT]
                    ],
                    "failed_children_total": len(failed),
                    "descendant_blocker_count": descendant_counts.get(phase_id, 0),
                    "remedies": [
                        {
                            "code": "retry_or_reopen",
                            "detail": (
                                "A supervisor may retry or reopen work with concrete feedback "
                                "when the current recovery, claim, hold, and retry-budget guards allow it."
                            ),
                        },
                        {
                            "code": "delete",
                            "detail": (
                                "An operator may explicitly delete work only when hierarchy, integration, "
                                "delivery-history, and ownership guards allow it."
                            ),
                        },
                    ],
                    "reason_code": "phase_failed_work",
                }
                continue

            if not phase_children:
                details[phase_id] = {"phase_id": phase_id, "reason_code": "phase_empty"}
            elif any(
                row["status"] == TaskStatus.PAUSED.value and row["resume_after"] is None
                for row in phase_children
            ):
                details[phase_id] = {"phase_id": phase_id, "reason_code": "phase_manual_pause"}
            elif any(row["status"] == TaskStatus.BLOCKED.value for row in phase_children):
                details[phase_id] = {"phase_id": phase_id, "reason_code": "phase_child_blocked"}
            else:
                details[phase_id] = {"phase_id": phase_id, "reason_code": "phase_active_children"}
        return details

    async def get_task_tree(self, root_task_id: str, *, max_depth: int = 4) -> dict | None:
        """Nested ``{"task", "children"}`` from one recursive CTE (spec §8)."""
        cte = self._descendant_cte(root_task_id)
        stmt = (
            select(tasks, cte.c.depth)
            .join(cte, cte.c.id == tasks.c.id)
            .where(cte.c.depth <= max_depth + 1)
            .order_by(cte.c.depth, tasks.c.id)
        )
        async with self._engine.begin() as conn:
            rows = (await conn.execute(stmt)).mappings().fetchall()
        if not rows:
            return None
        nodes: dict[str, dict] = {}
        root: dict | None = None
        for r in rows:
            task = self._row_to_task(r)
            node = {"task": task, "children": []}
            nodes[task.id] = node
            if task.id == root_task_id:
                root = node
            elif task.parent_task_id in nodes:
                nodes[task.parent_task_id]["children"].append(node)
        return root

    # -- the single writer ----------------------------------------------

    async def lock_hierarchy_project(self, conn, project_id: str) -> None:
        """Serialize hierarchy moves and worker-filing scope reads per project.

        PostgreSQL takes a transaction-scoped advisory lock in the dedicated
        AQ hierarchy namespace, keyed by the project's stable id. It does not
        contend with unrelated writes to the project row. SQLite filing
        transactions already use ``BEGIN IMMEDIATE``, so its database-wide
        writer lock provides the same exclusion.
        """
        await conn.execute(
            select(
                func.pg_advisory_xact_lock(
                    HIERARCHY_LOCK_NAMESPACE,
                    func.hashtext(project_id),
                )
            )
        )

    async def guard_integration_mutation(
        self,
        task_id: str,
        mutation: str,
        *,
        conn,
        retire_pending: bool = False,
        branch_policy: str | None = None,
        abandon_undelivered: bool = False,
    ) -> bool:
        """Fence canonical hierarchy/lifecycle writers for enabled projects.

        Returns whether hierarchical delivery applies.  When
        ``retire_pending`` is true, a removal retires every live origin in the
        subtree and advances each surviving affected parent's generation in
        this transaction.  Delivered and batch-sealed identity is never
        discarded.

        ``branch_policy`` says what to do about origins whose branch reached
        the remote, and is only consulted when ``retire_pending`` is set:

        ``"keep"``
            Retire the origins; leave the refs alone.  ``archive`` always
            passes this — archiving moves a task out of the active view, it
            does not destroy work.
        ``"discard"``
            Retire the origins and mark each materialized one ``pending`` for
            :class:`~src.integration.branch_discard.BranchDiscardService`,
            which removes the ref asynchronously.
        ``None``
            Refuse with ``branch_discard_required`` when the subtree holds a
            materialized origin, naming the branches in the error context so a
            surface can ask.  A caller that says nothing can never destroy a
            branch by omission.
        """
        if branch_policy not in (None, "keep", "discard"):
            raise ValueError(f"unknown branch_policy: {branch_policy!r}")
        task_row = (
            await conn.execute(
                select(tasks.c.project_id, tasks.c.parent_task_id).where(tasks.c.id == task_id)
            )
        ).one_or_none()
        if task_row is None:
            return False
        mode = (
            await conn.execute(
                select(projects.c.hierarchical_integration_mode).where(
                    projects.c.id == task_row.project_id
                )
            )
        ).scalar_one_or_none()
        removal_ids = None
        if mutation in {"delete", "archive"}:
            # The removal half is mode-independent: audit rows and live repair
            # authority survive a project's switch to development/disabled.
            # The advisory lock is re-entrant when hierarchy work continues
            # below, and preserves the existing project → sessions → tasks
            # ordering for the new reads.
            from src.integration.removal_guard import assert_integration_permits_removal

            await self.lock_hierarchy_project(conn, task_row.project_id)
            removal_ids = await self.subtree_ids(task_id, conn=conn)
            if not removal_ids:
                return mode in {"hierarchy", "train"}
            await assert_integration_permits_removal(
                self,
                conn,
                root_id=task_id,
                ids=removal_ids,
                mutation=mutation,
                project_id=task_row.project_id,
                mode=mode,
                abandon_undelivered=abandon_undelivered,
            )
        if mode not in {"hierarchy", "train"}:
            return False
        await self.lock_hierarchy_project(conn, task_row.project_id)
        ids = removal_ids if removal_ids is not None else await self.subtree_ids(task_id, conn=conn)
        if not ids:
            return True
        active_batch_states = (
            "sealing",
            "sealed",
            "building",
            "testing",
            "repairing",
            "human_blocked",
            "promoting",
            "cleanup_pending",
        )
        # A mutation below a sealed root changes that member's frozen
        # aggregate just as surely as mutating the member row itself.  Walk
        # upward as well as downward so a descendant cannot evade sealing.
        ancestor_seed = select(tasks.c.id, tasks.c.parent_task_id, literal(0).label("depth")).where(
            tasks.c.id == task_id
        )
        ancestors = ancestor_seed.cte("integration_mutation_ancestors", recursive=True)
        parent = tasks.alias("integration_mutation_parent")
        ancestors = ancestors.union_all(
            select(parent.c.id, parent.c.parent_task_id, ancestors.c.depth + 1)
            .join(ancestors, parent.c.id == ancestors.c.parent_task_id)
            .where(ancestors.c.depth < MAX_STRUCTURAL_DEPTH)
        )
        ancestor_ids = [row[0] for row in (await conn.execute(select(ancestors.c.id))).all()]
        sealed_scope = sorted(set(ids) | set(ancestor_ids))
        sealed = (
            await conn.execute(
                select(integration_batch_members.c.task_id)
                .select_from(
                    integration_batch_members.join(
                        integration_batches,
                        integration_batches.c.id == integration_batch_members.c.batch_id,
                    )
                )
                .where(integration_batch_members.c.task_id.in_(sealed_scope))
                .where(integration_batches.c.lifecycle.in_(active_batch_states))
                .limit(1)
            )
        ).first()
        if sealed:
            raise HierarchyError("sealed", f"{mutation} would change a sealed subtree")
        rollover_operation = None
        if mutation == "reopen":
            checkpoint_episode = (
                await conn.execute(
                    select(task_integration_checkpoints.c.episode_id).where(
                        task_integration_checkpoints.c.task_id == task_id
                    )
                )
            ).scalar_one_or_none()
            if checkpoint_episode is not None:
                rollover_operation = (
                    await conn.execute(
                        select(integration_repair_operations.c.id).where(
                            integration_repair_operations.c.parent_task_id == task_id,
                            integration_repair_operations.c.episode_id == checkpoint_episode,
                            integration_repair_operations.c.state == "completed",
                        )
                    )
                ).scalar_one_or_none()
        receipt_source = tasks.alias("integration_receipt_source")
        delivered_stmt = (
            select(task_delivery_receipts.c.source_task_id)
            .select_from(
                task_delivery_receipts.join(
                    receipt_source,
                    receipt_source.c.id == task_delivery_receipts.c.source_task_id,
                )
            )
            .where(task_delivery_receipts.c.source_task_id.in_(ids))
        )
        if rollover_operation is not None:
            delivered_stmt = delivered_stmt.where(
                or_(
                    task_delivery_receipts.c.target_task_id.is_(None),
                    task_delivery_receipts.c.target_task_id != task_id,
                    task_delivery_receipts.c.source_task_id == task_id,
                    receipt_source.c.parent_task_id != task_id,
                )
            )
        delivered = (await conn.execute(delivered_stmt.limit(1))).first()
        if delivered:
            raise HierarchyError(
                "delivery_target_fixed", f"{mutation} would change delivered branch identity"
            )
        origins = (
            (
                await conn.execute(
                    select(task_branch_origins)
                    .where(task_branch_origins.c.task_id.in_(ids))
                    .where(task_branch_origins.c.retired_at.is_(None))
                    .with_for_update()
                )
            )
            .mappings()
            .all()
        )
        materialized = [row for row in origins if row["materialized"]]
        if retire_pending and materialized and branch_policy is None:
            # Not a refusal on the merits — the caller simply has not said what
            # should happen to the branches.  Name them so the surface can ask.
            branches = [
                {
                    "task_id": row["task_id"],
                    "branch": row["branch_name"],
                    "base_sha": row["base_sha"],
                }
                for row in sorted(materialized, key=lambda r: r["task_id"])
            ]
            raise HierarchyError(
                "branch_discard_required",
                f"{len(branches)} task(s) in this subtree have a branch on the remote; "
                f"{mutation} must say whether to keep or delete it",
                {"branches": branches},
            )
        if retire_pending and origins:
            now = time.time()
            await conn.execute(
                update(task_branch_origins)
                .where(task_branch_origins.c.id.in_([row["id"] for row in origins]))
                .values(retired_at=now)
            )
            if branch_policy == "discard" and materialized:
                # The row outlives the task it describes, so the drain can find
                # this after the subtree is gone.
                await conn.execute(
                    update(task_branch_origins)
                    .where(
                        task_branch_origins.c.id.in_([row["id"] for row in materialized])
                    )
                    .values(
                        discard_state="pending",
                        discard_requested_at=now,
                        discard_attempts=0,
                        discard_next_attempt_at=now,
                        discard_last_error=None,
                    )
                )
            owner_ids = [row["task_id"] for row in origins]
            await conn.execute(
                update(integration_branch_owners)
                .where(integration_branch_owners.c.owner_id.in_(owner_ids))
                .where(integration_branch_owners.c.owner_role == "worker")
                .where(integration_branch_owners.c.handoff_state == "reserved")
                .values(handoff_state="released", updated_at=now)
            )
            surviving_parents = {
                row["parent_task_id"]
                for row in origins
                if row["parent_task_id"] and row["parent_task_id"] not in ids
            }
            for parent_id in sorted(surviving_parents):
                await conn.execute(
                    update(task_integration_checkpoints)
                    .where(task_integration_checkpoints.c.task_id == parent_id)
                    .values(
                        generation=task_integration_checkpoints.c.generation + 1,
                        verified_sha=None,
                        verified_generation=None,
                        version=task_integration_checkpoints.c.version + 1,
                        updated_at=now,
                    )
                )
        if rollover_operation is not None:
            await conn.execute(
                update(task_integration_checkpoints)
                .where(task_integration_checkpoints.c.task_id == task_id)
                .values(
                    generation=task_integration_checkpoints.c.generation + 1,
                    episode_id=None,
                    verified_sha=None,
                    verified_generation=None,
                    current_verification_id=None,
                    state="working",
                    version=task_integration_checkpoints.c.version + 1,
                    updated_at=time.time(),
                )
            )
        elif mutation in {"reopen", "disposition"} and task_row.parent_task_id:
            # Reopening or changing the terminal disposition makes the
            # parent's previously observed child aggregate stale even when
            # no delivery receipt exists yet.  The transition and this
            # invalidation share the caller's transaction.
            await conn.execute(
                update(task_integration_checkpoints)
                .where(task_integration_checkpoints.c.task_id == task_row.parent_task_id)
                .values(
                    generation=task_integration_checkpoints.c.generation + 1,
                    verified_sha=None,
                    verified_generation=None,
                    version=task_integration_checkpoints.c.version + 1,
                    updated_at=time.time(),
                )
            )
        return True

    async def set_parent(
        self,
        task_id: str,
        parent_id: str | None,
        *,
        conn,
        description: str | None = None,
        integration_authorized: bool = False,
        completed_parent_for_repair: bool = False,
    ) -> TransitionResult:
        """Move *task_id* under *parent_id* (``None`` = root).  Spec §5.

        Same transaction: delete any existing parent-child edge, insert the
        new one, write ``tasks.parent_task_id``, recompute ``is_blocked``
        over the affected set, mark the new parent a container, settle both
        the old and the new container, and record any ``task.ready``
        frontier entries the reparent produced (spec §9) — the settlement
        recursion already recorded its own entries in-transaction, so this
        only notes ids in ``flipped`` that settlement did not already
        cover.  Returns a ``TransitionResult`` (``flipped``, ``settled``,
        ``ready``).
        """
        task_row = (
            await conn.execute(
                select(tasks.c.id, tasks.c.project_id, tasks.c.parent_task_id).where(
                    tasks.c.id == task_id
                )
            )
        ).fetchone()
        if task_row is None:
            raise HierarchyError("not_found", task_id)
        await self.lock_hierarchy_project(conn, task_row.project_id)
        # The hierarchy lock may have waited behind another hierarchy move;
        # re-read the row so all validation below sees that committed move.
        task_row = (
            await conn.execute(
                select(tasks.c.id, tasks.c.project_id, tasks.c.parent_task_id).where(
                    tasks.c.id == task_id
                )
            )
        ).fetchone()
        if task_row is None:
            raise HierarchyError("not_found", task_id)
        old_parent = task_row.parent_task_id

        if not integration_authorized and old_parent != parent_id:
            mode = (
                await conn.execute(
                    select(projects.c.hierarchical_integration_mode).where(
                        projects.c.id == task_row.project_id
                    )
                )
            ).scalar_one_or_none()
            if mode in {"hierarchy", "train"}:
                raise HierarchyError(
                    "delivery_target_fixed",
                    "hierarchical tasks must be reparented through integration_mutate_hierarchy",
                )

        if parent_id is not None:
            if parent_id == task_id:
                raise HierarchyError("self_parent", task_id)
            parent_row = (
                await conn.execute(
                    select(tasks.c.id, tasks.c.project_id, tasks.c.status).where(
                        tasks.c.id == parent_id
                    )
                )
            ).fetchone()
            if parent_row is None:
                raise HierarchyError("not_found", parent_id)
            if parent_row.project_id != task_row.project_id:
                raise HierarchyError(
                    "cross_project",
                    f"{task_id} is in {task_row.project_id}, "
                    f"{parent_id} in {parent_row.project_id}",
                )
            # Development conflict repair is filed only after its source has
            # checkpointed and completed.  The integration service may retain
            # that completed source as the repair's structural origin, but no
            # ordinary caller may add work beneath a closed container.
            if parent_row.status == TaskStatus.COMPLETED.value and not (
                integration_authorized and completed_parent_for_repair
            ):
                raise HierarchyError("container_closed", parent_id)
            # Cycle: the new parent must not be inside task_id's subtree.
            if parent_id in await self.subtree_ids(task_id, conn=conn):
                raise HierarchyError("cycle", f"{parent_id} is a descendant of {task_id}")
            # Blocking-edge DAG check (waits-for / blocks edges could loop
            # through the new parent-child edge).  Runs before the depth
            # check so a cyclic request reports ``cycle``, not ``depth``
            # (spec order: self_parent, not_found, cross_project,
            # container_closed, cycle, depth).
            # The new edge is task_id -> parent_id.  It closes a cycle iff
            # parent_id already reaches task_id over blocking edges.
            if await self._reaches_over_blocking_edges(conn, parent_id, task_id):
                raise HierarchyError(
                    "cycle", f"{parent_id} already depends on {task_id} through blocking edges"
                )
            depth = await self.structural_depth(parent_id, conn=conn)
            height = await self.subtree_height(task_id, conn=conn)
            if depth + height > MAX_STRUCTURAL_DEPTH:
                raise HierarchyError(
                    "depth",
                    f"parent depth {depth} + subtree height {height} > {MAX_STRUCTURAL_DEPTH}",
                )

        affected = await self._collect_affected({task_id}, conn)
        if old_parent:
            affected.add(old_parent)
        if parent_id:
            affected.add(parent_id)

        # Marked after every validation has passed (a raising set_parent
        # writes nothing) and before the edge writes, while the previous
        # parent is still the one recorded on the row.
        await self.mark_layout_dirty(
            task_row.project_id,
            [task_id],
            f"parent.changed:{old_parent or '-'}",
            conn=conn,
        )

        await conn.execute(
            delete(task_dependencies).where(
                and_(
                    task_dependencies.c.task_id == task_id,
                    task_dependencies.c.dep_type == DepType.PARENT_CHILD.value,
                )
            )
        )
        if parent_id is not None:
            await conn.execute(
                insert(task_dependencies).values(
                    task_id=task_id,
                    depends_on_task_id=parent_id,
                    dep_type=DepType.PARENT_CHILD.value,
                    description=description,
                )
            )
            await self.mark_container(parent_id, conn=conn)
        await conn.execute(
            update(tasks)
            .where(tasks.c.id == task_id)
            .values(parent_task_id=parent_id, updated_at=time.time())
        )
        affected |= await self._collect_affected({task_id}, conn)
        flipped = await self.recompute_blocked(affected, conn=conn)
        settle_result = await self.settle_containers(
            {p for p in (old_parent, parent_id) if p}, conn=conn
        )
        flipped |= settle_result.flipped

        already_noted = {tid for tid, _ in settle_result.ready}
        own_ready_ids = await self._note_frontier_entry(
            conn, flipped - already_noted, reason="unblocked"
        )
        ready = list(settle_result.ready) + [(tid, "unblocked") for tid in own_ready_ids]

        return TransitionResult(flipped=flipped, settled=settle_result.settled, ready=ready)

    async def set_parent_bulk(
        self, child_ids: list[str], parent_id: str, *, conn
    ) -> tuple[set[str], list[str]]:
        """Link many freshly inserted leaves under one parent (spec §5, §15.2).

        The bulk twin of :meth:`set_parent` for the graph-creation path: the
        parent is validated **once**, the edges are written in two statements
        and the projection is recomputed and settled once, so a 200-node
        graph costs a constant handful of statements instead of ~23 per node.

        The shortcut is only sound for *freshly inserted leaves* — a childless
        node with no blocking out-edges cannot close a cycle and cannot make
        the subtree taller than one level.  Both preconditions are asserted
        here (one statement each) and raise ``cycle_check_skipped`` rather
        than silently skipping the DAG walk.  Use :meth:`set_parent` for
        anything that already sits in the graph.
        """
        ids = list(dict.fromkeys(child_ids))
        if not ids:
            return set(), []
        if parent_id in ids:
            raise HierarchyError("self_parent", parent_id)

        parent_row = (
            await conn.execute(
                select(tasks.c.id, tasks.c.project_id, tasks.c.status).where(
                    tasks.c.id == parent_id
                )
            )
        ).fetchone()
        if parent_row is None:
            raise HierarchyError("not_found", parent_id)
        await self.guard_hierarchy_bulk_write(parent_row.project_id, conn=conn)
        if parent_row.status == TaskStatus.COMPLETED.value:
            raise HierarchyError("container_closed", parent_id)

        child_rows = (
            await conn.execute(
                select(tasks.c.id, tasks.c.project_id, tasks.c.parent_task_id).where(
                    tasks.c.id.in_(sorted(ids))
                )
            )
        ).fetchall()
        found = {r.id for r in child_rows}
        missing = [i for i in ids if i not in found]
        if missing:
            raise HierarchyError("not_found", missing[0])
        wrong = [r.id for r in child_rows if r.project_id != parent_row.project_id]
        if wrong:
            raise HierarchyError(
                "cross_project",
                f"{wrong[0]} is in another project than {parent_id}",
            )

        # Leaf preconditions — these are what make the skipped DAG walk honest.
        from src.models import BLOCKING_DEP_TYPES

        has_edge = (
            await conn.execute(
                select(task_dependencies.c.task_id)
                .where(
                    and_(
                        task_dependencies.c.task_id.in_(sorted(ids)),
                        task_dependencies.c.dep_type.in_(sorted(BLOCKING_DEP_TYPES)),
                    )
                )
                .limit(1)
            )
        ).fetchone()
        if has_edge is not None:
            raise HierarchyError(
                "cycle_check_skipped",
                f"{has_edge[0]} already has blocking edges; use set_parent",
            )
        has_child = (
            await conn.execute(
                select(tasks.c.id).where(tasks.c.parent_task_id.in_(sorted(ids))).limit(1)
            )
        ).fetchone()
        if has_child is not None:
            raise HierarchyError(
                "cycle_check_skipped",
                f"{has_child[0]}'s parent is one of the children; use set_parent",
            )

        # Subtree height is 1 for every child (asserted above), so one depth
        # read covers the whole batch.
        depth = await self.structural_depth(parent_id, conn=conn)
        if depth + 1 > MAX_STRUCTURAL_DEPTH:
            raise HierarchyError(
                "depth",
                f"parent depth {depth} + subtree height 1 > {MAX_STRUCTURAL_DEPTH}",
            )

        old_parents = {r.parent_task_id for r in child_rows if r.parent_task_id}
        # One mark per child, carrying that child's own previous parent —
        # same contract as set_parent, written before the edges move.
        for row in child_rows:
            await self.mark_layout_dirty(
                row.project_id,
                [row.id],
                f"parent.changed:{row.parent_task_id or '-'}",
                conn=conn,
            )
        now = time.time()
        await conn.execute(
            delete(task_dependencies).where(
                and_(
                    task_dependencies.c.task_id.in_(sorted(ids)),
                    task_dependencies.c.dep_type == DepType.PARENT_CHILD.value,
                )
            )
        )
        await conn.execute(
            insert(task_dependencies),
            [
                {
                    "task_id": cid,
                    "depends_on_task_id": parent_id,
                    "dep_type": DepType.PARENT_CHILD.value,
                }
                for cid in ids
            ],
        )
        await self.mark_container(parent_id, conn=conn)
        await conn.execute(
            update(tasks)
            .where(tasks.c.id.in_(sorted(ids)))
            .values(parent_task_id=parent_id, updated_at=now)
        )

        affected = await self._collect_affected(set(ids), conn)
        affected.add(parent_id)
        affected |= old_parents
        flipped = await self.recompute_blocked(affected, conn=conn)
        settle_result = await self.settle_containers({parent_id} | old_parents, conn=conn)
        flipped |= settle_result.flipped
        return flipped, settle_result.settled

    async def _reaches_over_blocking_edges(self, conn, start: str, target: str) -> bool:
        """Is *target* reachable from *start* along blocking edges?

        Follows ``task_id -> depends_on_task_id`` (the "X blocks-on Y"
        direction) over ``BLOCKING_DEP_TYPES`` only, as one recursive CTE
        bounded by the reachable set.  Replaces loading every blocking edge
        in the database and running ``validate_dag_with_new_edge`` in
        Python, which cost ~6 ms of SQL plus the graph build per call and
        grew with the whole database rather than the subtree (93 ms per
        ``set_parent`` at 14k edges).

        Unlike the whole-graph check it replaced, this only detects a cycle
        the new edge would close; a graph that is already cyclic from drift
        is not rejected here.  ``REACHABILITY_MAX_DEPTH`` is a safety net for
        that case, not a performance bound: the ``UNION`` keys on
        ``(id, depth)``, so a drifted cycle yields up to ``depth × |cycle|``
        rows before the guard stops it.
        """
        from src.models import BLOCKING_DEP_TYPES

        blocking = sorted(BLOCKING_DEP_TYPES)
        base = (
            select(task_dependencies.c.depends_on_task_id.label("id"), literal(1).label("depth"))
            .where(
                task_dependencies.c.task_id == start,
                task_dependencies.c.dep_type.in_(blocking),
            )
            .cte("reach", recursive=True)
        )
        step = select(
            task_dependencies.c.depends_on_task_id, (base.c.depth + 1).label("depth")
        ).where(
            task_dependencies.c.task_id == base.c.id,
            task_dependencies.c.dep_type.in_(blocking),
            base.c.depth < REACHABILITY_MAX_DEPTH,
        )
        # UNION collapses equal-depth re-convergence; the depth guard below
        # is what bounds an already-cyclic graph.
        reach = base.union(step)
        row = (
            await conn.execute(select(reach.c.id).where(reach.c.id == target).limit(1))
        ).fetchone()
        return row is not None

    # -- settlement -------------------------------------------------------

    async def settle_containers(self, seeds: set[str], *, conn, depth: int = 0) -> TransitionResult:
        """Complete every seeded container whose children are all done (spec §7).

        Predicate: container flag ∧ status = IN_PROGRESS ∧ no live session holds
        it ∧ no non-COMPLETED child (vacuously true when empty) ∧ not a
        childless *held-open* container (see
        :func:`childless_held_open_container`).  Each hit goes
        through ``_apply_transition``, which — via its ``_settle_depth``
        keyword — seeds its own parent back into this method one level
        deeper; the climb is bounded by ``MAX_STRUCTURAL_DEPTH`` levels of
        recursion, enforced by the ``depth`` guard below.  ``depth`` counts
        the settlement hop about to run (the first hop, off the task that
        actually transitioned, is ``depth=1``), so the guard is ``depth >
        MAX_STRUCTURAL_DEPTH`` — not ``>=`` — or a 3-level cap would only
        ever let 2 ancestors settle before blocking (see
        ``test_settles_exactly_max_structural_depth_levels``).
        """
        result = TransitionResult()
        pending = {s for s in seeds if s}
        if not pending or depth > MAX_STRUCTURAL_DEPTH:
            return result

        from src.integration.parent_completion import ParentCompletion

        for parent_id in sorted(pending):
            has_episode = (
                await conn.execute(
                    select(task_integration_checkpoints.c.task_id)
                    .select_from(
                        task_integration_checkpoints.join(
                            tasks, tasks.c.id == task_integration_checkpoints.c.task_id
                        ).join(projects, projects.c.id == tasks.c.project_id)
                    )
                    .where(
                        task_integration_checkpoints.c.task_id == parent_id,
                        task_integration_checkpoints.c.episode_id.is_not(None),
                        projects.c.hierarchical_integration_mode.in_(("hierarchy", "train")),
                    )
                )
            ).first()
            if has_episode:
                await ParentCompletion(self).mark_ready_on(conn, parent_id)

        child = tasks.alias("child")
        stmt = (
            select(tasks.c.id)
            .select_from(tasks.join(projects, projects.c.id == tasks.c.project_id))
            .where(
                and_(
                    tasks.c.id.in_(sorted(pending)),
                    tasks.c.status == TaskStatus.IN_PROGRESS.value,
                    exists(
                        select(literal(1)).where(
                            and_(
                                task_metadata.c.task_id == tasks.c.id,
                                task_metadata.c.key == CONTAINER_KEY,
                                task_metadata.c.value == CONTAINER_VALUE,
                            )
                        )
                    ),
                    ~exists(
                        select(literal(1)).where(
                            and_(
                                sessions.c.task_id == tasks.c.id,
                                sessions.c.state.in_(LIVE_SESSION_STATES),
                            )
                        )
                    ),
                    ~exists(
                        select(literal(1)).where(
                            and_(
                                child.c.parent_task_id == tasks.c.id,
                                child.c.status != TaskStatus.COMPLETED.value,
                            )
                        )
                    ),
                    ~childless_held_open_container(),
                    or_(
                        ~projects.c.hierarchical_integration_mode.in_(("hierarchy", "train")),
                        ~exists(
                            select(literal(1)).where(
                                task_integration_checkpoints.c.task_id == tasks.c.id,
                                task_integration_checkpoints.c.episode_id.is_not(None),
                            )
                        ),
                    ),
                )
            )
        )
        hits = [r[0] for r in (await conn.execute(stmt)).fetchall()]
        for cid in hits:
            # _apply_transition seeds the container's own parent back into
            # this method at depth + 1, so grandparents are handled by
            # recursion; merge everything it settled and flipped.
            res = await self._apply_transition(
                conn,
                cid,
                TaskStatus.COMPLETED,
                context="subtasks_completed",
                _settle_depth=depth,
            )
            # Report the container as settled only if the write actually
            # landed: ``_apply_transition`` can decline (a missing row, an
            # enforced invalid transition).  Announcing a completion that did
            # not happen would emit ``task.completed`` for a task still
            # IN_PROGRESS.  Recursion may also have settled it already, so
            # the id is only appended once.
            landed = (await conn.execute(select(tasks.c.status).where(tasks.c.id == cid))).scalar()
            if landed == TaskStatus.COMPLETED.value and cid not in result.settled:
                result.settled.append(cid)
            for sid in res.settled:
                if sid not in result.settled:
                    result.settled.append(sid)
            result.flipped |= res.flipped
            result.ready.extend(res.ready)
        return result

    async def settle_candidates(self) -> list[str]:
        """Every container the §7 predicate would settle right now (backstop).

        Shares :func:`childless_held_open_container` with
        :meth:`settle_containers`, so the backstop sweep cannot complete a
        brand-new phase or standing parent the event path deliberately left
        alone.
        """
        child = tasks.alias("child")
        stmt = (
            select(tasks.c.id)
            .select_from(tasks.join(projects, projects.c.id == tasks.c.project_id))
            .where(
                and_(
                    tasks.c.status == TaskStatus.IN_PROGRESS.value,
                    exists(
                        select(literal(1)).where(
                            and_(
                                task_metadata.c.task_id == tasks.c.id,
                                task_metadata.c.key == CONTAINER_KEY,
                                task_metadata.c.value == CONTAINER_VALUE,
                            )
                        )
                    ),
                    ~exists(
                        select(literal(1)).where(
                            and_(
                                sessions.c.task_id == tasks.c.id,
                                sessions.c.state.in_(LIVE_SESSION_STATES),
                            )
                        )
                    ),
                    ~exists(
                        select(literal(1)).where(
                            and_(
                                child.c.parent_task_id == tasks.c.id,
                                child.c.status != TaskStatus.COMPLETED.value,
                            )
                        )
                    ),
                    ~childless_held_open_container(),
                    or_(
                        ~projects.c.hierarchical_integration_mode.in_(("hierarchy", "train")),
                        ~exists(
                            select(literal(1)).where(
                                task_integration_checkpoints.c.task_id == tasks.c.id,
                                task_integration_checkpoints.c.episode_id.is_not(None),
                            )
                        ),
                    ),
                )
            )
        )
        async with self._engine.begin() as conn:
            return [r[0] for r in (await conn.execute(stmt)).fetchall()]

    # -- creation -------------------------------------------------------

    async def create_task_under(
        self,
        task: Task,
        parent_id: str,
        *,
        routing_policy: Callable[[Task], bool] | None = None,
        description: str | None = None,
    ) -> tuple[str, bool]:
        """Insert *task* as a child of *parent_id* in one transaction (spec §6).

        Reserves the dotted id, inserts the row, links it via
        :meth:`set_parent` — or, at the naming cap, gives it a root id and a
        ``discovered-from`` edge.  Returns ``(task_id, capped)``.
        """
        async with self._engine.begin() as conn:
            # Lock order is project hierarchy advisory lock, then task rows.
            # Worker filing takes the same order before reserving an ordinal.
            await self.lock_hierarchy_project(conn, task.project_id)
            task_id, capped = await child_task_id(conn, parent_id)
            task.id = task_id
            task.parent_task_id = None  # set_parent owns the pointer
            await self._insert_task_row(task, conn=conn)
            if capped:
                await conn.execute(
                    insert(task_dependencies).values(
                        task_id=task_id,
                        depends_on_task_id=parent_id,
                        dep_type=DepType.DISCOVERED_FROM.value,
                        description=description,
                    )
                )
            else:
                await self.set_parent(task_id, parent_id, conn=conn, description=description)
            task.parent_task_id = None if capped else parent_id
            gated = routing_policy is not None and routing_policy(task)
            if gated:
                await self.create_gate(
                    task.project_id,
                    "routing",
                    "Route task",
                    question="Assign profile + intelligence class (+ workspace if profile needs one).",
                    waiter_task_ids=[task_id],
                    conn=conn,
                )
                task.is_blocked = True
        if gated:
            await self.log_blocked_flips({task_id})
        return task_id, capped

    # -- container-close semantics ---------------------------------------

    async def open_children(self, task_id: str, *, conn=None) -> list[str]:
        """Direct children not yet terminal (spec §7 close rule)."""
        terminal = (TaskStatus.COMPLETED.value, TaskStatus.FAILED.value)
        stmt = (
            select(tasks.c.id)
            .where(and_(tasks.c.parent_task_id == task_id, tasks.c.status.notin_(terminal)))
            .order_by(tasks.c.id)
        )
        if conn is not None:
            return [r[0] for r in (await conn.execute(stmt)).fetchall()]
        async with self._engine.begin() as c:
            return [r[0] for r in (await c.execute(stmt)).fetchall()]

    async def live_descendant_sessions(
        self, task_id: str, *, conn, exclude_root: bool = False
    ) -> list[tuple[str, str]]:
        """Live sessions holding any task in *task_id*'s subtree.

        Lock order is sessions-before-tasks to match the claim path (spec §7):
        on Postgres the rows are taken ``FOR UPDATE`` so a session cannot
        start holding a descendant between this check and the abandonment.

        With ``exclude_root=True`` the root task's own sessions are left out,
        so the answer is about *descendants* only.  The ``task_close
        --abandon-children`` path needs that: the closing worker (or the
        container-root session driving the close) is itself live by
        definition, and counting it made every abandon refuse.
        """
        ids = await self.subtree_ids(task_id, conn=conn)
        if exclude_root:
            ids = [i for i in ids if i != task_id]
        if not ids:
            return []
        stmt = select(sessions.c.id, sessions.c.task_id).where(
            and_(sessions.c.task_id.in_(ids), sessions.c.state.in_(LIVE_SESSION_STATES))
        )
        stmt = stmt.with_for_update()
        rows = [(r[0], r[1]) for r in (await conn.execute(stmt)).fetchall()]
        # Deduplicate: a task may carry more than one session row, and the
        # caller reports one entry per (session, task) pair.
        return sorted(set(rows))

    async def manually_paused_descendants(self, task_id: str, *, conn) -> list[str]:
        """Descendants a human has paused by hand (``PAUSED``, no ``resume_after``).

        ``abandon_subtree`` cannot move these — the manual-pause guard in
        ``_apply_transition`` raises ``ManualPauseActive`` — so the close
        path checks them up front and refuses with the ids instead of
        letting the exception escape mid-transaction.
        """
        ids = await self.subtree_ids(task_id, conn=conn)
        ids = [i for i in ids if i != task_id]
        if not ids:
            return []
        stmt = select(tasks.c.id).where(
            and_(
                tasks.c.id.in_(ids),
                tasks.c.status == TaskStatus.PAUSED.value,
                tasks.c.resume_after.is_(None),
            )
        )
        return sorted(r[0] for r in (await conn.execute(stmt)).fetchall())

    async def abandon_subtree(self, task_id: str, *, conn) -> TransitionResult:
        """Close every non-terminal descendant as ``abandoned`` (spec §7).

        Administrative close: ``force=True`` on every transition, since a
        descendant may be sitting in a state (``PAUSED``, ``ASSIGNED``,
        ``WAITING_INPUT``, ...) with no ordinary edge to ``COMPLETED``.
        Accumulates ``.flipped`` / ``.settled`` across every descendant so
        the caller can run one post-commit ``log_blocked_flips`` /
        ``_notify_settled`` pass instead of dropping them.
        """
        ids = await self.subtree_ids(task_id, conn=conn)
        ids = [i for i in ids if i != task_id]
        if not ids:
            return TransitionResult()
        stmt = select(tasks.c.id, tasks.c.status).where(tasks.c.id.in_(ids))
        stmt = stmt.with_for_update()
        rows = (await conn.execute(stmt)).fetchall()
        terminal = (TaskStatus.COMPLETED.value, TaskStatus.FAILED.value)
        # Deepest first so each container settles naturally after its children.
        depth = {tid: i for i, tid in enumerate(ids)}
        open_ids = sorted((r[0] for r in rows if r[1] not in terminal), key=lambda t: -depth[t])
        result = TransitionResult()
        for tid in open_ids:
            await self._upsert_meta(tid, "work_outcome", "abandoned", conn=conn)
            res = await self._apply_transition(
                conn,
                tid,
                TaskStatus.COMPLETED,
                context="abandoned_by_container",
                assigned_agent_id=None,
                force=True,
            )
            # An abandoned task holds nothing.  Same transaction as the
            # status write, mirroring ``_delete_one``'s release: a lock or an
            # agent pointer left behind would strand a workspace and keep an
            # agent BUSY on work nobody is doing.
            await conn.execute(
                update(workspaces)
                .where(workspaces.c.locked_by_task_id == tid)
                .values(
                    locked_by_task_id=None,
                    locked_by_agent_id=None,
                    locked_at=None,
                    lock_mode=None,
                )
            )
            await conn.execute(
                update(agents)
                .where(agents.c.current_task_id == tid)
                .values(current_task_id=None, state=AgentState.IDLE.value)
            )
            result.settled.extend(res.settled)
            result.flipped |= res.flipped
            # Abandoning a blocker unblocks its dependents; carry the
            # frontier entries up so the caller's ``_notify_ready`` wakes a
            # waiting ``task_claim`` long-poll (I2).
            result.ready.extend(res.ready)
            result.abandoned.append(tid)
        return result

    async def _upsert_meta(self, task_id: str, key: str, value, *, conn) -> None:
        """Set ``task_metadata[key] = value`` (JSON-encoded), insert-or-update."""
        encoded = json.dumps(value)
        ins = pg_insert
        stmt = ins(task_metadata).values(task_id=task_id, key=key, value=encoded)
        stmt = stmt.on_conflict_do_update(
            index_elements=["task_id", "key"], set_={"value": encoded}
        )
        await conn.execute(stmt)

    async def _upsert_meta_many(self, task_id: str, items: dict, *, conn) -> None:
        """``_upsert_meta`` for several keys of one task in a single statement.

        The claim path writes ``claimed_by_session`` and ``work_dir``
        together; one multi-row upsert keeps that inside the spec §15
        transaction budget.  ``set_`` reads from ``excluded`` so each row
        updates with its own value.
        """
        if not items:
            return
        ins = pg_insert
        stmt = ins(task_metadata).values(
            [
                {"task_id": task_id, "key": key, "value": json.dumps(value)}
                for key, value in items.items()
            ]
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["task_id", "key"], set_={"value": stmt.excluded.value}
        )
        await conn.execute(stmt)
