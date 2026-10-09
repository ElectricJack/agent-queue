"""Hierarchy — the single writer for parent/child membership (spec Part I).

Truth is the ``parent-child`` edge; ``tasks.parent_task_id`` is a derived
cache that only :meth:`HierarchyQueryMixin.set_parent` writes, in the same
transaction as the edge, the blocked-state recompute and container
settlement.  Every mutation here takes ``conn`` and never opens its own
transaction.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import (
    Text,
    and_,
    any_,
    bindparam,
    case,
    delete,
    exists,
    false,
    func,
    insert,
    literal,
    literal_column,
    or_,
    select,
    true,
    update,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.database.queries.task_queries import (
    INTEGRATION_REWORK_AT_KEY,
    STALE_OPEN_ATTENTION,
    TransitionResult,
)
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
    task_context,
    task_delivery_receipts,
    task_dependencies,
    task_integration_checkpoints,
    task_metadata,
    tasks,
    workspaces,
)
from src.models import AgentState, DepType, Task, TaskStatus
from src.task_names import MAX_STRUCTURAL_DEPTH, child_task_id

logger = logging.getLogger(__name__)

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
#: ``task_metadata`` key a task created as a container carries
#: (``create_task(container=true)``, ``aq task create --container``).  Written
#: with the container flag in the creation transaction by
#: :meth:`HierarchyQueryMixin.declare_container`, so an epic filed before its
#: children is never claimable and never settles empty (bold-flare-35).
DECLARED_CONTAINER_KEY = "declared_container"
#: Every ``task_metadata`` key that holds a childless container open.  A
#: container carrying any of these is created *before* the work it will hold,
#: so it must survive the window in which it has no children at all.
HELD_OPEN_CONTAINER_KEYS = (PHASE_KEY, STANDING_PARENT_KEY, DECLARED_CONTAINER_KEY)
#: Two-key PostgreSQL advisory-lock namespace "AQHI" (AQ hierarchy).
HIERARCHY_LOCK_NAMESPACE = 0x41514849
#: Bounds the recursive walk on an already-cyclic graph; far above any real
#: blocking chain. Blocking edges (waits-for / blocks / conditional-blocks)
#: have no structural depth bound, unlike parent-child nesting, so this must
#: not be derived from MAX_STRUCTURAL_DEPTH.
REACHABILITY_MAX_DEPTH = 10_000


#: Statuses a finished container can be stranded in.  Restarts, updates and
#: operator holds leave a container BLOCKED or PAUSED, and the ordinary §7 leg
#: only settles IN_PROGRESS, so nothing moved such an epic once its last child
#: was delivered (sharp-impact and agile-torrent, 2026-09-26).
STALE_CONTAINER_STATUSES = (TaskStatus.BLOCKED.value, TaskStatus.PAUSED.value)
#: Transition context (and audit event suffix) of the stale-status leg.
STALE_CONTAINER_CONTEXT = "stale_container_settled"
#: Hold metadata a stale PAUSED container keeps; settling it supersedes the
#: hold, exactly as ``_resume_locked`` clears it on a manual resume.
_STALE_CONTAINER_HOLD_KEYS = ("manual_pause", "manual_pause_withholds_children")


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


def never_leaseable_container():
    """``WHERE`` clause: the claim frontier will never offer this row to a worker.

    The conjunction of the two reasons ``_frontier_predicates``
    (``src/database/queries/claim_queries.py``) keeps off the frontier:
    ``container_settles_without_worker`` — the §7 container flag, because a
    container settles when its children finish and a worker holding one could
    never close it — and ``has_children``, which covers the parent row the flag
    never reached (the flag lands with a task's first child, so for any parent
    the two are the same rule).

    They stay two named frontier predicates because ``aq task explain`` and the
    doctor report each under its own code.  This is the composed question a
    writer *outside* the frontier needs to ask: is this row settle-only work no
    pool worker can ever be given?  A writer that has to give a task's
    lifecycle back must not draw the line narrower than the one that took it
    away, or a parent whose flag never landed is stranded by the difference
    between the two spellings.
    """
    child = tasks.alias("never_leaseable_child")
    return or_(
        container_flag_exists(),
        exists(select(literal(1)).where(child.c.parent_task_id == tasks.c.id)),
    )


def childless_held_open_container():
    """``WHERE`` clause: the row is a held-open container with no children.

    A *held-open* container — a phase (A1), a keyed standing parent (A2) or a
    declared container (``create_task(container=true)``, bold-flare-35) — is
    created *before* the work that belongs to it, so it spends a window
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


def _settlement_clauses() -> list:
    """The §7 conditions both settlement legs share, correlated to ``tasks``.

    Container flag ∧ no live session holds it ∧ no non-COMPLETED child ∧ not
    a childless held-open container ∧ no hierarchy/train collection episode
    owns its completion.  The caller joins ``projects`` and adds the status.
    """
    child = tasks.alias("child")
    return [
        container_flag_exists(),
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
                    # In a train project the git-first train never runs the
                    # legacy collector, and orphan settlement cancels its
                    # operation; a cancelled episode owns nothing, so the
                    # container settles here (grand-lantern-78, 2026-10-07).
                    or_(
                        projects.c.hierarchical_integration_mode != "train",
                        exists(
                            select(literal(1)).where(
                                integration_repair_operations.c.parent_task_id == tasks.c.id,
                                integration_repair_operations.c.episode_id
                                == task_integration_checkpoints.c.episode_id,
                                integration_repair_operations.c.state != "cancelled",
                            )
                        ),
                    ),
                )
            ),
        ),
    ]


def stale_container_clauses() -> list:
    """The stale-status leg's graph half: a BLOCKED/PAUSED container that may settle.

    On top of :func:`_settlement_clauses` it needs at least one child (a
    childless container's stale status is not "left over from its children
    finishing").  Whether every child's work is *delivered* is not SQL's
    answer: :meth:`HierarchyQueryMixin.settle_containers` also needs git to
    prove each child :func:`stale_delivery_children` names, through a verified
    :class:`~src.integration.delivery_observer.DeliveryView`.
    """
    child = tasks.alias("stale_child")
    return [
        tasks.c.status.in_(STALE_CONTAINER_STATUSES),
        *_settlement_clauses(),
        exists(select(literal(1)).where(child.c.parent_task_id == tasks.c.id)),
    ]


async def stale_delivery_children(conn, container_ids) -> dict[str, set[str]]:
    """The children whose delivery git must prove before their stale container settles.

    A child in development delivery scope
    (:func:`~src.integration.delivery_observer.development_delivery_scope`,
    foreign repositories included, as the archive guard asks it).  Anything
    outside development mode and any branchless organizational child has
    nothing to deliver.  A child that is itself a container proved delivery
    through its own children when it settled, so it is not asked again.
    """
    from src.integration.delivery_observer import development_delivery_scope

    ids = sorted(set(container_ids))
    if not ids:
        return {}
    child = tasks.alias("stale_child")
    child_flag = task_metadata.alias("stale_child_flag")
    rows = await conn.execute(
        select(child.c.parent_task_id, child.c.id).where(
            child.c.parent_task_id.in_(ids),
            ~exists(
                select(literal(1)).where(
                    child_flag.c.task_id == child.c.id,
                    child_flag.c.key == CONTAINER_KEY,
                    child_flag.c.value == CONTAINER_VALUE,
                )
            ),
            development_delivery_scope(child),
        )
    )
    required: dict[str, set[str]] = {}
    for parent_id, child_id in rows.all():
        required.setdefault(parent_id, set()).add(child_id)
    return required


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

    Multi-project readers use request-scoped modes where available and keep
    the correlated form for other projects.
    """

    hierarchical: bool
    integration_repository_id: str | None
    # None preserves legacy admission; a supplied set is one revalidated Git view.
    delivered_prerequisite_ids: frozenset[str] | None = None
    cross_epic_prerequisites: str = "default_branch"
    default_prerequisite_ids: frozenset[str] = frozenset()
    #: Dependents whose epic branch already contains every cross-epic source.
    parent_contained_task_ids: frozenset[str] = frozenset()
    stacked: bool = False
    stackable_prerequisite_ids: frozenset[str] = frozenset()

    @classmethod
    def of(cls, project) -> ProjectIntegrationMode | None:
        """Read the mode off a project row; ``None`` when there is no row.

        ``None`` means "ask the database per row" — the safe fallback, not
        "not hierarchical".
        """
        if project is None:
            return None
        from src.integration.stacked_branches import stacked_policy

        return cls(
            stacked=stacked_policy(project),
            hierarchical=getattr(project, "hierarchical_integration_mode", None)
            in HIERARCHY_MODES,
            integration_repository_id=getattr(project, "integration_repository_id", None),
            cross_epic_prerequisites=(
                getattr(project, "hierarchical_integration_policy", None) or {}
            ).get("cross_epic_prerequisites", "default_branch"),
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
            _ordinary_repair_branch(mode.integration_repository_id),
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
    ), _reserved_repair_branch(), _ordinary_repair_branch(), _reserved_verifier_branch())


def _ordinary_repair_branch(repository_id: str | None = None):
    """Ordinary repairs use the leased batch ref, without a legacy origin/stage."""
    owner = integration_branch_owners
    source = task_context.join(owner, owner.c.holder == task_context.c.task_id).join(
        integration_batches, integration_batches.c.id == tasks.c.created_by_id,
    )
    if repository_id is None:
        source = source.join(projects, projects.c.id == tasks.c.project_id)
    return exists(select(literal(1)).select_from(source).correlate(tasks).where(
        tasks.c.created_by_kind == "system",
        tasks.c.dedup_key == (
            "repair:" + integration_batches.c.id + ":"
            + func.cast(integration_batches.c.repair_attempt_count, Text)
        ),
        task_context.c.id == tasks.c.id,
        task_context.c.task_id == tasks.c.id,
        task_context.c.label == "Repair input",
        owner.c.fence.is_not(None),
        owner.c.repository_id == tasks.c.repo_id,
        or_(owner.c.ref == tasks.c.branch_name,
            owner.c.ref == ("refs/heads/" + tasks.c.branch_name)),
        owner.c.repository_id == (
            repository_id if repository_id is not None else projects.c.integration_repository_id
        ),
    ))


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

    ``ParentEpisodeRecords`` files the delegate on the parent's checkpoint branch
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
        # The prerequisite can live several SELECT levels above this scalar
        # subquery. Auto-correlation only checks the immediate parent and
        # otherwise adds a new prerequisite FROM, reading every task's marker.
        .correlate_except(task_metadata)
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
    predecessor's source still belongs to its feature branch until delivery
    is proven on their common parent branch. A revalidated Git view replaces
    the legacy receipt predicate when supplied in *mode*.

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
    if mode is not None and mode.delivered_prerequisite_ids is not None:
        delivered = prerequisite.c.id.in_(mode.delivered_prerequisite_ids)
    if mode is not None and mode.stacked:
        delivered = or_(delivered, prerequisite.c.id.in_(mode.stackable_prerequisite_ids))
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


def delivered_prerequisites_for_projects(modes=None, *, cross_parent=True):
    """Use each project's Git view, retaining legacy admission for other projects."""
    def predicate(mode=None):
        siblings = delivered_same_parent_prerequisites_when_hierarchical(mode)
        if not cross_parent:
            return siblings
        return siblings & cross_parent_prerequisites_on_default(mode) & epic_refresh_ready(mode)

    legacy = predicate()
    if not modes:
        return legacy
    return case(
        {pid: predicate(mode) for pid, mode in modes.items()},
        value=tasks.c.project_id,
        else_=legacy,
    )


def _cross_parent_prerequisites():
    """Completed cross-parent blocks edges, correlated to the dependent task."""
    edge = task_dependencies.alias("cross_prerequisite_edge")
    source = tasks.alias("cross_prerequisite_source")
    return source, select(literal_column("1")).select_from(
        edge.join(source, source.c.id == edge.c.depends_on_task_id),
    ).correlate(tasks).where(
        edge.c.task_id == tasks.c.id, edge.c.dep_type == literal_column("'blocks'"),
        source.c.parent_task_id.is_distinct_from(tasks.c.parent_task_id)
        | source.c.parent_task_id.is_(None),
        source.c.status == literal_column("'COMPLETED'"),
    )


def _cross_parent_delivery_enabled(mode):
    if mode is None:
        return exists(select(literal_column("1")).where(
            projects.c.id == tasks.c.project_id,
            projects.c.hierarchical_integration_mode.in_(HIERARCHY_MODES),
            func.coalesce(projects.c.hierarchical_integration_policy[
                "cross_epic_prerequisites"].as_string(), literal_column("'default_branch'"))
            != literal_column("'completed'"),
        ))
    return true() if mode.hierarchical and mode.cross_epic_prerequisites != "completed" else false()


def cross_parent_prerequisites_on_default(mode: ProjectIntegrationMode | None = None):
    """Require exact Git delivery of cross-parent sources on the default branch.

    Epic containment and refresh batches have their own frontier predicate.
    With no observation, a completed cross-parent source remains unproven.
    """
    source, cross_edge = _cross_parent_prerequisites()
    ids = mode.default_prerequisite_ids if mode else ()
    unproven = ~(source.c.id == any_(literal(list(ids), type_=ARRAY(Text)))) if ids else true()
    return ~(_cross_parent_delivery_enabled(mode) & exists(cross_edge.where(unproven)))


def open_epic_refresh_batches():
    """Open refresh batch ids for this task's epic; ordinary collections are unrelated."""
    parent = tasks.alias("refresh_pending_parent")
    return select(integration_batches.c.id).select_from(
        integration_batches.join(parent, parent.c.repo_id == integration_batches.c.repository_id),
    ).correlate(tasks).where(
        parent.c.id == tasks.c.parent_task_id,
        integration_batches.c.project_id == tasks.c.project_id,
        integration_batches.c.intent != literal_column("'aborted'"),
        integration_batches.c.lifecycle != literal_column("'promoted'"),
        integration_batches.c.id.like("train-epic-refresh-%"),
        integration_batches.c.target_ref == (literal_column("'refs/heads/'", type_=Text) + func.replace(
            parent.c.branch_name, literal_column("'refs/heads/'"), literal_column("''"))),
    )


def epic_refresh_ready(mode: ProjectIntegrationMode | None = None):
    """A cross-epic child needs contained sources and no open epic refresh.

    An ordinary sibling collection, including a failed or held one, cannot
    withhold a child whose epic already contains every proven source.
    """
    _, cross_edge = _cross_parent_prerequisites()
    parent = tasks.alias("containment_parent")
    has_epic = exists(select(literal_column("1")).where(
        parent.c.id == tasks.c.parent_task_id, parent.c.branch_name.is_not(None),
    ))
    ids = mode.parent_contained_task_ids if mode else ()
    contained = tasks.c.id == any_(literal(sorted(ids), type_=ARRAY(Text))) if ids else false()
    pending = exists(open_epic_refresh_batches()) | (has_epic & ~contained)
    return ~(_cross_parent_delivery_enabled(mode) & exists(cross_edge) & pending)


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

    async def hierarchy_runnable_task_ids(
        self, task_ids: list[str], *, hierarchy_modes=None
    ) -> set[str]:
        """Return tasks whose project mode/origin permits writer assignment."""
        if not task_ids:
            return set()
        if hierarchy_modes is None:
            from src.integration.delivery_observer import hierarchy_frontier_modes

            hierarchy_modes = await hierarchy_frontier_modes(self)
        async with self._engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        select(tasks.c.id).where(
                            # One array bind avoids expanding the whole READY frontier
                            # into thousands of parameters on each scheduler tick.
                            tasks.c.id == any_(bindparam("task_ids", task_ids, type_=ARRAY(Text))),
                            materialized_origin_when_hierarchical(),
                            delivered_prerequisites_for_projects(hierarchy_modes),
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
        In Git-first mode this uses the same current Git proof as admission,
        including its exact parent tip, rather than historical receipt rows.
        Active mode refuses stale or unknown proof. ``None`` means no sibling
        prerequisite needs a head; shadow mode retains the receipt overlay
        after the caller's :meth:`is_hierarchy_task_runnable` guard.
        """
        dependency = task_dependencies.alias("delivery_head_dependency")
        prerequisite = tasks.alias("delivery_head_prerequisite")
        checkpoint = task_integration_checkpoints.alias("delivery_head_checkpoint")
        receipt = task_delivery_receipts.alias("delivery_head_receipt")
        from src.integration.delivery_observer import prerequisite_observer

        observer = prerequisite_observer(self)
        if observer is not None:
            task = await self.get_task(task_id)
            if task is None or task.parent_task_id is None:
                return None
            view = await observer.prerequisite_view(task.project_id, task_id=task_id)
            if not await view.fresh():
                raise ValueError("hierarchy prerequisite target moved during preparation")
            async with self._engine.connect() as conn:
                current = (await conn.execute(select(tasks).where(
                    tasks.c.id == task_id,
                ))).mappings().one_or_none()
                if current is None or current["parent_task_id"] != task.parent_task_id:
                    raise ValueError("hierarchy prerequisite parent changed during preparation")
                ids = set((await conn.execute(select(prerequisite.c.id).select_from(
                    dependency.join(prerequisite,
                                    prerequisite.c.id == dependency.c.depends_on_task_id),
                ).where(
                    dependency.c.task_id == task_id,
                    dependency.c.dep_type == DepType.BLOCKS.value,
                    prerequisite.c.parent_task_id == current["parent_task_id"],
                ))).scalars())
                if not ids:
                    return None
                verified = await view.verified_on(conn, ids)
                origin = (await conn.execute(select(task_branch_origins).where(
                    task_branch_origins.c.task_id == task_id,
                    task_branch_origins.c.reserved.is_(True),
                    task_branch_origins.c.retired_at.is_(None),
                ))).mappings().one_or_none()
                parent_branch = (await conn.execute(select(tasks.c.branch_name).where(
                    tasks.c.id == current["parent_task_id"],
                ))).scalar_one_or_none()
                if (origin is None or not parent_branch or not origin["parent_ref"]
                    or origin["parent_task_id"] != current["parent_task_id"]
                    or origin["parent_repository_id"] != current["repo_id"]
                    or origin["parent_ref"].removeprefix("refs/heads/")
                    != parent_branch.removeprefix("refs/heads/")):
                    raise ValueError("hierarchy prerequisite origin changed during preparation")
                for tid in ids:
                    proof = verified.get(tid)
                    target = view.targets.get(tid)
                    if (proof is None or not proof.satisfied or target is None
                        or target.repository_id != current["repo_id"]
                        or target.target_ref != "refs/heads/" + parent_branch.removeprefix(
                            "refs/heads/")):
                        raise ValueError("hierarchy prerequisite delivery is not current")
                heads = {proof.target_oid for proof in verified.values()}
            if not await view.fresh() or len(heads) != 1:
                raise ValueError("hierarchy prerequisite target moved during preparation")
            return heads.pop()
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

    async def declare_container(self, task_id: str, *, conn) -> None:
        """Flag *task_id* a container that is held open until work arrives.

        Both writes in the caller's transaction, for the reason
        ``_mark_standing_parent`` gives: the flag alone is a childless
        container the §7 sweep completes, the held-open key alone a claimable
        task.  Idempotent.
        """
        await self.mark_container(task_id, conn=conn)
        await self._upsert_meta(task_id, DECLARED_CONTAINER_KEY, True, conn=conn)

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
                            # A stale-open flag is advisory: the child is
                            # still an ordinary dependency block.
                            task_metadata.c.value != json.dumps(STALE_OPEN_ATTENTION),
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
        delivery=None,
        obsolete_integration_delegate: bool = False,
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

        ``delivery`` is the git view a removal took before this transaction
        (see :mod:`src.integration.removal_guard`); development work it does
        not verify as delivered refuses an archive.
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
                delivery=delivery,
                obsolete_integration_delegate=obsolete_integration_delegate,
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
        delivered_stmt = select(task_delivery_receipts.c.source_task_id).where(
            task_delivery_receipts.c.source_task_id.in_(ids)
        )
        if rollover_operation is not None:
            # A completed aggregate can reverify without changing any
            # delivery inside its subtree, including nested collections.
            # Only receipts that leave the subtree fix an external identity.
            delivered_stmt = delivered_stmt.where(
                or_(
                    task_delivery_receipts.c.target_task_id.is_(None),
                    task_delivery_receipts.c.target_task_id.not_in(ids),
                )
            )
        delivered = (await conn.execute(delivered_stmt.limit(1))).first()
        if delivered and mutation != "archive":
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
        reject_live_parent: bool = False,
        defer_projection: bool = False,
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

        ``reject_live_parent`` protects a reparent destination from acquiring
        children while another worker holds it. Creation paths leave it off:
        workers deliberately creating subtasks must retain their ownership.

        ``defer_projection`` is for a task change set: the caller must
        recompute the final graph and settle both parents before committing.
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
            parent_stmt = select(
                tasks.c.id, tasks.c.project_id, tasks.c.status, tasks.c.assigned_agent_id
            ).where(tasks.c.id == parent_id)
            if reject_live_parent:
                # Claims lock the task before recording their holder. Wait
                # for that transaction, then inspect its committed ownership.
                # Do not lock sessions here: claims lock session before task.
                parent_stmt = parent_stmt.with_for_update()
            parent_row = (await conn.execute(parent_stmt)).fetchone()
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
            if reject_live_parent and old_parent != parent_id:
                live_holder = (
                    await conn.execute(
                        select(sessions.c.id).where(
                            sessions.c.task_id == parent_id,
                            sessions.c.state.in_(LIVE_SESSION_STATES),
                        ).limit(1)
                    )
                ).scalar_one_or_none()
                if parent_row.assigned_agent_id is not None or live_holder is not None:
                    raise HierarchyError(
                        "live_parent",
                        f"cannot reparent under '{parent_id}' while it has a live holder; "
                        "leave the task in its current placement or use an unclaimed container",
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
        if defer_projection:
            return TransitionResult()
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

    async def settle_containers(
        self, seeds: set[str], *, conn, depth: int = 0, delivery=None
    ) -> TransitionResult:
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

        A second, stricter leg settles a seeded container stranded BLOCKED or
        PAUSED (:func:`stale_container_clauses`): it needs every child's work
        delivered, not merely COMPLETED, and it supersedes the stale status and
        any operator hold (:meth:`_settle_stale_container`).  Delivery is git's
        answer, prepared before this transaction: *delivery* is a
        :class:`~src.integration.delivery_observer.DeliveryView`, and each
        child :func:`stale_delivery_children` names must still carry the
        identity it evaluated and be satisfied.  Without a view (the event
        path, inside some other write) only a container with no such child
        settles here; the backstop and startup sweeps bring the view for the
        rest.
        """
        result = TransitionResult()
        pending = {s for s in seeds if s}
        if not pending or depth > MAX_STRUCTURAL_DEPTH:
            return result

        from src.integration.records import ParentEpisodeRecords

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
                await ParentEpisodeRecords(self).mark_ready_on(conn, parent_id)

        stmt = (
            select(tasks.c.id)
            .select_from(tasks.join(projects, projects.c.id == tasks.c.project_id))
            .where(
                tasks.c.id.in_(sorted(pending)),
                tasks.c.status == TaskStatus.IN_PROGRESS.value,
                *_settlement_clauses(),
            )
        )
        hits = [r[0] for r in (await conn.execute(stmt)).fetchall()]
        for cid in hits:
            settleable, completion_token = await self._episode_completion_token(conn, cid)
            if not settleable:
                continue
            # _apply_transition seeds the container's own parent back into
            # this method at depth + 1, so grandparents are handled by
            # recursion; merge everything it settled and flipped. A refusal
            # skips this container rather than failing the caller's write.
            try:
                async with conn.begin_nested():
                    res = await self._apply_transition(
                        conn,
                        cid,
                        TaskStatus.COMPLETED,
                        context="subtasks_completed",
                        _settle_depth=depth,
                        _integration_completion_token=completion_token,
                    )
            except HierarchyError as exc:
                logger.warning("Container %s not settled: %s", cid, exc)
                continue
            await self._merge_settlement(conn, cid, res, result)
        stale = (
            select(tasks.c.id)
            .select_from(tasks.join(projects, projects.c.id == tasks.c.project_id))
            .where(tasks.c.id.in_(sorted(pending)), *stale_container_clauses())
        )
        stale_ids = [r[0] for r in (await conn.execute(stale)).fetchall()]
        required = await stale_delivery_children(conn, stale_ids)
        wanted = set().union(*required.values()) if required else set()
        verified = (
            await delivery.verified_on(conn, wanted) if delivery is not None and wanted else {}
        )
        for cid in stale_ids:
            if any(
                child_id not in verified or not verified[child_id].satisfied
                for child_id in required.get(cid, ())
            ):
                continue
            # One refused container must not stall the sweep for every other
            # container: settle each inside its own savepoint.
            try:
                async with conn.begin_nested():
                    res = await self._settle_stale_container(conn, cid, depth=depth)
            except HierarchyError as exc:
                logger.warning("Stale container %s not settled: %s", cid, exc)
                continue
            if res is not None:
                await self._merge_settlement(conn, cid, res, result)
        return result

    async def _merge_settlement(self, conn, cid: str, res: TransitionResult, result) -> None:
        """Fold one container's transition into the running settlement result.

        Report the container as settled only if the write actually landed:
        ``_apply_transition`` can decline (a missing row, an enforced invalid
        transition).  Announcing a completion that did not happen would emit
        ``task.completed`` for a task still open.  Recursion may also have
        settled it already, so the id is only appended once.
        """
        landed = (await conn.execute(select(tasks.c.status).where(tasks.c.id == cid))).scalar()
        if landed == TaskStatus.COMPLETED.value and cid not in result.settled:
            result.settled.append(cid)
        for sid in res.settled:
            if sid not in result.settled:
                result.settled.append(sid)
        result.flipped |= res.flipped
        result.ready.extend(res.ready)

    async def _episode_completion_token(self, conn, cid: str):
        """Whether *cid* may settle, and the completion token it needs.

        A managed parent completes only through verified integration. The one
        exception settlement admits is a train project whose legacy collection
        episode was cancelled: that episode owns nothing, and the git-first
        train never runs its collector. Re-checked on the caller's (locked)
        connection; anything else with an episode is refused.
        """
        episode = (
            await conn.execute(
                select(task_integration_checkpoints.c.episode_id).where(
                    task_integration_checkpoints.c.task_id == cid,
                    task_integration_checkpoints.c.episode_id.is_not(None),
                )
            )
        ).scalar_one_or_none()
        if episode is None:
            return True, None
        mode = (
            await conn.execute(
                select(projects.c.hierarchical_integration_mode)
                .select_from(tasks.join(projects, projects.c.id == tasks.c.project_id))
                .where(tasks.c.id == cid)
            )
        ).scalar_one_or_none()
        if mode != "train":
            # Hierarchy keeps its own collector; only it completes the parent.
            return True, None
        live = (
            await conn.execute(
                select(integration_repair_operations.c.id)
                .where(
                    integration_repair_operations.c.parent_task_id == cid,
                    integration_repair_operations.c.episode_id == episode,
                    integration_repair_operations.c.state != "cancelled",
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if live is not None:
            return False, None
        from src.database.queries.task_queries import _INTEGRATION_COMPLETION_TOKEN

        return True, _INTEGRATION_COMPLETION_TOKEN

    async def _settle_stale_container(
        self, conn, cid: str, *, depth: int
    ) -> TransitionResult | None:
        """Complete one BLOCKED/PAUSED container the stale leg selected.

        Re-checked under the row lock, so a resume, a pause or a claim that
        landed after the candidate read is left alone.  A pause whose session
        cleanup is still pending is not settled: the hold's own retry owns
        that session.  The hold metadata goes with the status, in the same
        transaction, and the audit event names the status it left.
        """
        status = (
            await conn.execute(select(tasks.c.status).where(tasks.c.id == cid).with_for_update())
        ).scalar_one_or_none()
        if status not in STALE_CONTAINER_STATUSES:
            return None
        pause = (
            await conn.execute(
                select(task_metadata.c.value).where(
                    task_metadata.c.task_id == cid, task_metadata.c.key == "manual_pause"
                )
            )
        ).scalar_one_or_none()
        if pause is not None:
            try:
                if (json.loads(pause) or {}).get("cleanup_pending"):
                    return None
            except (TypeError, ValueError, AttributeError):
                pass
        settleable, completion_token = await self._episode_completion_token(conn, cid)
        if not settleable:
            return None
        await conn.execute(
            delete(task_metadata).where(
                task_metadata.c.task_id == cid,
                task_metadata.c.key.in_(_STALE_CONTAINER_HOLD_KEYS),
            )
        )
        res = await self._apply_transition(
            conn,
            cid,
            TaskStatus.COMPLETED,
            context=STALE_CONTAINER_CONTEXT,
            force=True,
            _integration_completion_token=completion_token,
            _manual_pause_control=True,
            _settle_depth=depth,
            resume_after=None,
            assigned_agent_id=None,
        )
        project_id = (
            await conn.execute(select(tasks.c.project_id).where(tasks.c.id == cid))
        ).scalar_one_or_none()
        await self.log_event(
            f"task.{STALE_CONTAINER_CONTEXT}",
            project_id=project_id,
            task_id=cid,
            payload=json.dumps({"from_status": status, "manual_hold": pause is not None}),
            conn=conn,
        )
        return res

    async def settle_candidates(self) -> list[str]:
        """Every container the §7 predicate would settle right now (backstop).

        Shares :func:`_settlement_clauses` (and so
        :func:`childless_held_open_container`) with :meth:`settle_containers`,
        so the backstop sweep cannot complete a brand-new phase or standing
        parent the event path deliberately left alone.
        """
        stmt = (
            select(tasks.c.id)
            .select_from(tasks.join(projects, projects.c.id == tasks.c.project_id))
            .where(tasks.c.status == TaskStatus.IN_PROGRESS.value, *_settlement_clauses())
        )
        async with self._engine.begin() as conn:
            return [r[0] for r in (await conn.execute(stmt)).fetchall()]

    async def stale_container_candidates(self) -> list[str]:
        """BLOCKED/PAUSED containers whose children are all COMPLETED.

        The stale-status leg's backstop and startup read (see
        :func:`stale_container_clauses`).  The event path settles these when a
        child's completion seeds the container; a delivery that lands later
        has no such event, so the container sweep asks this every interval,
        then proves delivery of :meth:`stale_container_delivery_children` in
        git before it settles any of them.
        """
        stmt = (
            select(tasks.c.id)
            .select_from(tasks.join(projects, projects.c.id == tasks.c.project_id))
            .where(*stale_container_clauses())
            .order_by(tasks.c.id)
        )
        async with self._engine.begin() as conn:
            return [r[0] for r in (await conn.execute(stmt)).fetchall()]

    async def stale_container_delivery_children(
        self, container_ids: list[str]
    ) -> dict[str, set[str]]:
        """Per stale candidate, the children git must prove delivered (no locks)."""
        async with self._engine.connect() as conn:
            return await stale_delivery_children(conn, container_ids)

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
            from src.integration.branch_retirement import request_task_retirement_on

            await request_task_retirement_on(
                conn, tid, request_id=f"abandon:{task_id}:{tid}:{time.time()}",
                reason=f"abandoned by container {task_id}",
            )
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
