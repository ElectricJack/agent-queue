"""Claims — who holds what (swarm-work-model §10, §11.2).

Every write that records a holder goes through this mixin on a caller-owned
connection opened by ``immediate()``: the session slot is taken with a
conditional UPDATE first (row lock on Postgres, writer lock on SQLite), then
the task, then agent/workspace/metadata.  ``activate_claim`` and
``release_claim`` are the two ways a claim leaves ``preparing`` and they
race by design — the conditional UPDATEs make exactly one of them win.
"""

from __future__ import annotations

import json
import time
from collections.abc import Collection

from sqlalchemy import (
    Float,
    and_,
    case,
    cast,
    delete,
    exists,
    false,
    func,
    literal,
    or_,
    select,
    update,
)

from src.database.queries.blocked_state import apply_label_filters, blocked_predicate
from src.database.queries.hierarchy_queries import (
    HIERARCHY_MODES,
    ProjectIntegrationMode,
    container_flag_exists,
    cross_parent_prerequisites_on_default,
    delivered_prerequisites_for_projects,
    delivered_same_parent_prerequisites_when_hierarchical,
    epic_refresh_ready,
    materialized_origin_when_hierarchical,
    never_leaseable_container,
    open_epic_refresh_batches,
)
from src.database.queries.session_queries import _row_to_session
from src.database.queries.task_queries import (
    ManualPauseActive,
    TransitionResult,
    _not_manually_paused,
    supports_returning,
)
from src.database.tables import (
    agents,
    integration_batches,
    integration_branch_owners,
    integration_repair_stages,
    projects,
    sessions,
    task_branch_origins,
    task_completion_records,
    task_delivery_receipts,
    task_integration_checkpoints,
    task_metadata,
    task_session_attempts,
    task_workspace_requirements,
    tasks,
    workspaces,
)
from src.models import AgentState, SessionRecord, Task, TaskEvent, TaskStatus, Workspace
from src.routing.sources import CLAIMABLE_SOURCES, LEGACY, claimable_sources


_frontier_child = tasks.alias("frontier_child")

#: Task statuses a pool claim can end in that no further close changes.
SETTLED_POOL_CLAIM_STATUSES = (TaskStatus.COMPLETED.value, TaskStatus.FAILED.value)

#: ``task_metadata`` key of the identity ``{completion_id, session_id,
#: claim_epoch}`` that the terminal transition accepting an ``aq task close``
#: records in its own transaction (smart-cascade).  Unlike
#: ``close_session_id``, which the close writes before it is accepted and a
#: refused close leaves behind, it exists only once that close committed.
ACCEPTED_CLOSE_KEY = "accepted_close"


def _frontier_predicates(hierarchy_mode: ProjectIntegrationMode | None = None):
    """Named acceptance predicates shared by claiming and diagnostics."""
    return {
        # Legacy control-plane profiles stay visible for rerouting but never
        # win a pool worker's claim.
        "supervisor_profile": tasks.c.profile_id.is_(None) | (tasks.c.profile_id != "supervisor"),
        "dependency_blocked": tasks.c.is_blocked == 0,
        "already_assigned": tasks.c.assigned_agent_id.is_(None),
        "plan_subtask": tasks.c.is_plan_subtask == 0,
        "origin_not_materialized": materialized_origin_when_hierarchical(hierarchy_mode),
        "sibling_prerequisite_not_delivered": (
            delivered_same_parent_prerequisites_when_hierarchical(hierarchy_mode)
        ),
        "prerequisite_not_on_default_branch": cross_parent_prerequisites_on_default(hierarchy_mode),
        "epic_refresh_pending": epic_refresh_ready(hierarchy_mode),
        # Containers settle when their children finish. A worker holding one
        # could never close it and would block the settlement it waits for.
        "container_settles_without_worker": ~container_flag_exists(),
        # The flag lands with a task's first child, so this is the same rule
        # for any parent row the flag never reached (bold-flare-35).
        "has_children": ~exists(
            select(literal(1)).where(_frontier_child.c.parent_task_id == tasks.c.id)
        ),
        # Stale READY delegates must not be claimed while cleanup catches up
        # with their expired stage.
        "retired_repair_delegate": ~exists(
            select(literal(1)).where(
                integration_repair_stages.c.repair_task_id == tasks.c.id,
                integration_repair_stages.c.writer_kind == "repair_delegate",
                integration_repair_stages.c.state.notin_(("active", "awaiting_completion")),
            )
        ),
    }


def route_claimable(router_ready: bool):
    """Spec I3 for one project: a profile, and a source the project accepts.

    ``router``, ``override`` and ``role`` routes are claimable; a ``legacy``
    one only while the project's router is not ready (mandatory routing
    §9.1); an ``unrouted`` task never is.
    """
    return and_(
        tasks.c.profile_id.is_not(None),
        tasks.c.route_source.in_(claimable_sources(router_ready)),
    )


def route_claimable_in(ready_project_ids: Collection[str]):
    """Spec I3 across projects: :func:`route_claimable` per row's project."""
    legacy = tasks.c.route_source == LEGACY
    if ready_project_ids:
        legacy = and_(legacy, tasks.c.project_id.notin_(sorted(ready_project_ids)))
    return and_(
        tasks.c.profile_id.is_not(None),
        or_(tasks.c.route_source.in_(CLAIMABLE_SOURCES), legacy),
    )


def _frontier_where(
    project_id: str,
    hierarchy_mode: ProjectIntegrationMode | None = None,
    *,
    router_ready: bool | None = None,
):
    """The frontier predicate, for one project.

    *hierarchy_mode* is this project's already-read
    ``hierarchical_integration_mode`` / ``integration_repository_id`` pair.
    Supplying it folds the two hierarchy predicates' ``projects`` lookups
    into constants instead of re-asking the same one-row question once per
    candidate row; ``None`` keeps the self-contained correlated form.

    *router_ready* adds :func:`route_claimable` for a project whose router
    is (not) ready; ``None`` leaves routing out, for the structural reads
    (development admission's candidates, diagnostics) that are not a claim.
    """
    predicates = [
        tasks.c.project_id == project_id,
        tasks.c.status == TaskStatus.READY.value,
        *_frontier_predicates(hierarchy_mode).values(),
    ]
    if router_ready is not None:
        predicates.append(route_claimable(router_ready))
    return and_(*predicates)


#: Transition context, session end reason and audit suffix of a claim taken
#: back because its task turned out to be a container (bold-flare-35).
CONTAINER_CLAIM_RELEASED = "container_claim_released"

#: Transition context, session end reason and audit suffix of a stopped pool
#: worker's claim taken back from a container it can no longer close
#: (sharp-ridge-57).
STALE_CONTAINER_CLAIM_RELEASED = "stale_container_claim_released"

#: ``task_metadata`` key naming the session a task's claim belongs to, written
#: by :meth:`ClaimQueryMixin.record_holder` on the claim path and nowhere else.
CLAIMED_BY_SESSION_KEY = "claimed_by_session"

#: ``sessions.claim_phase`` values that mean a claim is still being taken.  A
#: session stopped in one of them never activated the claim its record names,
#: so that record proves nothing about a claim anybody could still act on.
CLAIM_PHASE_IN_FLIGHT = ("claiming", "preparing")

#: The task's own claim record, the session it names, and every other session
#: row — one alias each, shared by :func:`stale_container_claim_statement`'s
#: ``FROM`` and :func:`_exact_stopped_pool_holder`'s predicate so the candidate
#: read and the repair it feeds cannot disagree about which row is the holder.
_STALE_CLAIM_RECORD = task_metadata.alias("stale_container_claim")
_STALE_CLAIM_HOLDER = sessions.alias("stale_container_holder")
_STALE_CLAIM_OTHER = sessions.alias("stale_container_other")


def _claim_record_names_session():
    """Join condition: the task's claim record names the session it is joined to.

    ``task_metadata.value`` is :func:`json.dumps` output, so a session id is
    stored quoted and the comparison has to reproduce that quoting in SQL
    rather than decode the value once per candidate row.  A row written by
    anything else is simply not a match, which fails closed.
    """
    return and_(
        _STALE_CLAIM_RECORD.c.task_id == tasks.c.id,
        _STALE_CLAIM_RECORD.c.key == CLAIMED_BY_SESSION_KEY,
        _STALE_CLAIM_RECORD.c.value
        == literal('"').concat(_STALE_CLAIM_HOLDER.c.id).concat(literal('"')),
    )


def _exact_stopped_pool_holder():
    """Clause: the task's own claim record names one session that is dead for good.

    The exact-holder proof :func:`src.integration.noop_repair.stale_parent_claim_on`
    introduced (sharp-glacier-30), as a predicate over the holder the *task*
    names rather than over the sessions pointing back at it.

    Joining ``sessions.task_id == tasks.id`` was the bug this replaces
    (fleet-delta-97): the real stop path does not leave that pointer behind.
    ``release_container_claim`` — the monitoring leg the 14:05Z log shows
    firing on every restart — runs the ordinary release, which writes the task
    back ``IN_PROGRESS`` with no agent and clears ``sessions.task_id`` and
    ``sessions.claim_phase``, and the drain that follows leaves the row
    ``stopped``/``stopped``.  ``task_metadata.claimed_by_session`` is the one
    record of the claim that survives that, so it is what identifies the
    holder now (bold-crest-75, calm-quest-88).

    What the named session must then prove:

    * ``lifecycle`` ``pool``, ``state`` *and* ``desired_state`` both
      ``stopped``, so no drain or restart can resurrect it;
    * not mid-claim — ``claim_phase`` is what a stopped holder actually
      keeps, either the activated claim it never gave back or ``NULL`` once it
      did, and never ``claiming``/``preparing``, which is a claim nobody ever
      held;
    * a concrete tmux ``instance_token``, and a last claim epoch that is still
      the task's current one — a requeued or re-leased claim carries a newer
      epoch, which is what makes releasing safe for a slot that may already
      have been reused;
    * that it is not holding some *other* task, so clearing its row cannot
      disturb a claim that has nothing to do with this one;
    * and that no other live session still claims this task, because which of
      two holders owns the claim would then be a guess.  "Live" is the same
      test the holder itself must pass: a session a restart is about to revive
      (``stopped`` now, ``running`` wanted) counts, so this can be no looser
      than the one clause above.
    """
    return and_(
        _STALE_CLAIM_HOLDER.c.lifecycle == "pool",
        _STALE_CLAIM_HOLDER.c.state == "stopped",
        _STALE_CLAIM_HOLDER.c.desired_state == "stopped",
        or_(
            _STALE_CLAIM_HOLDER.c.claim_phase.is_(None),
            _STALE_CLAIM_HOLDER.c.claim_phase.notin_(CLAIM_PHASE_IN_FLIGHT),
        ),
        _STALE_CLAIM_HOLDER.c.instance_token.is_not(None),
        _STALE_CLAIM_HOLDER.c.last_claim_epoch == tasks.c.claim_epoch,
        or_(
            _STALE_CLAIM_HOLDER.c.task_id.is_(None),
            _STALE_CLAIM_HOLDER.c.task_id == tasks.c.id,
        ),
        ~exists(
            select(literal(1)).where(
                _STALE_CLAIM_OTHER.c.task_id == tasks.c.id,
                or_(
                    _STALE_CLAIM_OTHER.c.state != "stopped",
                    _STALE_CLAIM_OTHER.c.desired_state != "stopped",
                ),
            )
        ),
    )


def stale_container_claim_clauses() -> list:
    """Containers a stopped pool worker still holds and nothing else can finish.

    A container is never leased, so the frontier cannot offer it to anybody,
    and :meth:`ClaimQueryMixin.release_container_claim` only ever takes a
    claim back from a holder that is still *running*.  A container whose worker
    has since stopped therefore keeps a claim that nothing may act on: the
    worker can never close it, the frontier will not offer it, and in a
    hierarchy/train project §7 settlement refuses it outright because its
    collection episode owns the completion.  The parent is stranded
    IN_PROGRESS forever (bold-crest-75, sharp-ridge-57).

    The clause set is deliberately narrow — this is a reconciler, not a
    scheduler, and it may only move work that is already finished with
    everything except the claim itself:

    * ``never_leaseable_container()`` — the composed frontier rule, so this
      predicate cannot be narrower than the one that made the row unleasable;
    * ``IN_PROGRESS`` with no agent, which is the shape a container is created
      in and the only one §7 ever completes;
    * the exact stopped pool holder above;
    * a checkpoint carrying both a collection *episode* and a head of its own,
      so there is an aggregate for the parent runtime to verify and the
      ordinary (episode-free) §7 leg — which already tolerates a stopped
      holder — is not what is being repaired here;
    * no child outside ``COMPLETED``: a container with work still in flight has
      a live collection to make progress, and this reconciler is not the thing
      that should decide otherwise.
    """
    child = tasks.alias("stale_container_claim_child")
    return [
        tasks.c.status == TaskStatus.IN_PROGRESS.value,
        tasks.c.assigned_agent_id.is_(None),
        never_leaseable_container(),
        _not_manually_paused(),
        _exact_stopped_pool_holder(),
        exists(
            select(literal(1)).where(
                task_integration_checkpoints.c.task_id == tasks.c.id,
                task_integration_checkpoints.c.episode_id.is_not(None),
                task_integration_checkpoints.c.checkpoint_sha.is_not(None),
            )
        ),
        ~exists(
            select(literal(1)).where(
                child.c.parent_task_id == tasks.c.id,
                child.c.status != TaskStatus.COMPLETED.value,
            )
        ),
    ]


def stale_container_claim_statement(*, task_ids: Collection[str] | None = None):
    """The candidate read for :meth:`ClaimQueryMixin.release_stale_container_claim`.

    The holder arrives through the task's own claim record
    (:func:`_claim_record_names_session`), which is what
    :func:`_exact_stopped_pool_holder` then proves, so the two joins share one
    pair of aliases and the scan and the repair always read the same row.
    """
    stmt = (
        select(
            tasks.c.id,
            tasks.c.project_id,
            _STALE_CLAIM_HOLDER.c.id.label("session_id"),
            _STALE_CLAIM_HOLDER.c.last_claim_epoch,
        )
        .select_from(
            tasks.join(
                _STALE_CLAIM_RECORD,
                and_(
                    _STALE_CLAIM_RECORD.c.task_id == tasks.c.id,
                    _STALE_CLAIM_RECORD.c.key == CLAIMED_BY_SESSION_KEY,
                ),
            )
            .join(_STALE_CLAIM_HOLDER, _claim_record_names_session())
            .join(projects, projects.c.id == tasks.c.project_id)
        )
        .where(*stale_container_claim_clauses())
        .where(projects.c.hierarchical_integration_mode.in_(HIERARCHY_MODES))
        .order_by(tasks.c.id)
    )
    if task_ids is not None:
        stmt = stmt.where(tasks.c.id.in_(sorted(task_ids)))
    return stmt

# Kept in task metadata rather than a task column: this is operational
# claim-state, not lifecycle state, and therefore needs no schema migration.
PREPARE_BACKOFF_UNTIL_KEY = "claim_prepare_backoff_until"
PREPARE_BACKOFF_ATTEMPTS_KEY = "claim_prepare_backoff_attempts"
PREPARE_BACKOFF_INITIAL_SECONDS = 120.0
PREPARE_BACKOFF_MAX_SECONDS = 300.0
CLAIM_PREPARATION_METADATA_KEYS = (
    "manual_pause_checkpoint",
    # Operator-handoff checkpoint (task_recovery_queries): consumed by the one
    # preparation that applies it; left behind, every later prepare re-enters
    # the handoff path against a preserved ref that has since moved or gone.
    "supervisor_recovery_checkpoint",
    PREPARE_BACKOFF_UNTIL_KEY,
    PREPARE_BACKOFF_ATTEMPTS_KEY,
    "slot_reset_failure",
    # The last fence that refused a preparation (``BRANCH_FENCED``). Evidence
    # for the wait, so it goes stale at exactly the same boundary as the
    # ladder it throttles.
    "branch_fenced",
    "stack_prerequisites_conflict",
)

#: Matches exactly what PostgreSQL's ``double precision`` input accepts here:
#: an optional sign, digits, and an optional fractional part.  Deliberately
#: narrower than ``float8`` (no exponents, no ``NaN``/``Infinity``) because
#: every writer of a numeric metadata value writes a plain timestamp.
_NUMERIC_TEXT = r"^-?[0-9]+(\.[0-9]+)?$"


def numeric_meta_value(column, *, default: str = "0"):
    """``column`` cast to ``Float``, with non-numeric text read as *default*.

    ``task_metadata.value`` is free-form JSON text: ``json.dumps`` writes a
    bare ``0`` for the number and ``"0"`` -- quotes included -- for the
    string, and nothing in the schema stops a caller storing either.  A bare
    ``cast(value, Float)`` therefore raises
    ``invalid input syntax for type double precision`` on the *whole
    statement* the moment one malformed row is scanned.

    That is not a hypothetical.  On 2026-09-07 two rows holding ``"0"`` made
    every ``select_ready_for_profile`` call in one project raise for ~18
    hours: no pool worker could claim anything, and because the failure was
    a database error rather than an empty result, the queue looked idle
    rather than broken.

    The ``CASE`` is what makes this safe rather than merely likely to work:
    PostgreSQL does not guarantee evaluation order between a regex guard and
    a cast sitting in the same ``AND``, so the guard has to be *inside* the
    expression being cast.  A malformed row then reads as *default* -- for a
    backoff deadline, "expired", which fails open to claimable rather than
    silently withholding work.
    """
    return cast(
        case((column.op("~")(_NUMERIC_TEXT), column), else_=literal(default)),
        Float,
    )


def _claim_preparation_predicates():
    origin = task_branch_origins.alias("claim_stack_origin")
    repair = tasks.alias("claim_stack_repair")
    completion = task_completion_records.alias("claim_stack_completion")
    latest_outcome = (
        select(completion.c.outcome).where(completion.c.task_id == repair.c.id)
        .order_by(completion.c.completed_at.desc()).limit(1).correlate(repair).scalar_subquery()
    )
    repair_passed = exists(select(literal(1)).where(
        repair.c.id == origin.c.stack_snapshot["preparation_conflict"]["repair_task_id"].astext,
        repair.c.status == TaskStatus.COMPLETED.value,
        latest_outcome == "pass",
    ).correlate(origin))
    return {
        "stack_prerequisites_conflict": ~exists(select(literal(1)).where(
            origin.c.task_id == tasks.c.id,
            origin.c.retired_at.is_(None),
            origin.c.stack_snapshot.op("?")("preparation_conflict"),
            ~repair_passed,
        )),
        "workspace_requirement": ~exists(select(literal(1)).where(
            task_workspace_requirements.c.task_id == tasks.c.id,
            task_workspace_requirements.c.kind_id.notin_(("project-repo", "vault")),
        )),
        "claim_prepare_backoff": ~exists(select(literal(1)).where(
            task_metadata.c.task_id == tasks.c.id,
            task_metadata.c.key == PREPARE_BACKOFF_UNTIL_KEY,
            numeric_meta_value(task_metadata.c.value) > time.time(),
        )),
    }


def claim_frontier_predicates(hierarchy_modes=None):
    """All profile-independent acceptance filters, including candidate preparation."""
    predicates = {
        **_frontier_predicates(),
        **_claim_preparation_predicates(),
        "hold_label": apply_label_filters(select(tasks.c.id), exclude_hold=True).whereclause,
    }
    predicates["sibling_prerequisite_not_delivered"] = delivered_prerequisites_for_projects(
        hierarchy_modes, cross_parent=False
    )
    for name, predicate in (
        ("prerequisite_not_on_default_branch", cross_parent_prerequisites_on_default),
        ("epic_refresh_pending", epic_refresh_ready),
    ):
        predicates[name] = case(
            {pid: predicate(mode) for pid, mode in hierarchy_modes.items()},
            value=tasks.c.project_id, else_=predicate(),
        ) if hierarchy_modes else predicate()
    return predicates


FRONTIER_PREDICATE_DETAILS = {
    "prerequisite_not_on_default_branch": (
        "a cross-epic prerequisite needs exact source containment on the default branch"
    ),
    "epic_refresh_pending": (
        "the epic must contain every proven cross-epic source and its open epic-refresh "
        "batch must finish before its children start"
    ),
    "supervisor_profile": "profile_id must not be supervisor",
    "dependency_blocked": "is_blocked must be false",
    "already_assigned": "assigned_agent_id must be empty",
    "plan_subtask": "is_plan_subtask must be false",
    "origin_not_materialized": (
        "materialized_origin_when_hierarchical(): requires a materialized origin in the "
        "designated repository or an exact active delegate reservation"
    ),
    "sibling_prerequisite_not_delivered": (
        "delivered_same_parent_prerequisites_when_hierarchical(): requires the preserved "
        "parent origin and each completed blocks sibling delivered to the shared parent or "
        "proven on its own task branch under the project stacked policy; "
        "Git-first uses current Git proof, shadow uses matching code receipts after rework"
    ),
    "container_settles_without_worker": "container_flag_exists() must be false",
    "has_children": "the task must have no children (a parent is a container)",
    "retired_repair_delegate": "repair delegate stage must be active or awaiting_completion",
    "workspace_requirement": "workspace requirements must be project-repo or vault",
    "claim_prepare_backoff": "claim_prepare_backoff_until must not be in the future",
    "stack_prerequisites_conflict": "the prerequisite stack repair needs a passing completion",
    "hold_label": "no hold:* label may be present",
    "route_not_claimable": (
        "route_source must be router, override or role with a profile (legacy too while "
        "the project's router is not ready); the project's router routes it"
    ),
}


class ClaimQueryMixin:
    """Expects ``self._engine`` plus Task/Session/Workspace/Hierarchy mixins.

    ``_row_to_workspace`` (WorkspaceQueryMixin), ``_apply_transition`` /
    ``_row_to_task`` (TaskQueryMixin) and ``_upsert_meta[_many]``
    (HierarchyQueryMixin) all come from the composed adapter.
    """

    async def claim_frontier_exclusions(
        self, task_id: str, *, router_ready: bool | None = None, cached_only: bool = False,
    ) -> list[dict]:
        """Evaluate the real claim filters for one READY task, without scheduler guesses.

        *router_ready* adds the route filter (:func:`route_claimable`) for a
        project whose router is (not) ready.
        """
        from src.integration.delivery_observer import hierarchy_frontier_modes

        unavailable = {}
        modes = await hierarchy_frontier_modes(
            self, task_id=task_id, cached_only=cached_only,
            **({"display_unavailable": unavailable} if cached_only else {}),
        )
        predicates = claim_frontier_predicates(modes)
        if router_ready is not None:
            predicates["route_not_claimable"] = route_claimable(router_ready)
        display_predicates = {}
        if unavailable:
            from dataclasses import replace

            # These counterfactual predicates classify diagnostic uncertainty
            # only. The actual modes and all decision queries remain fail closed.
            display_modes = {
                pid: replace(
                    mode,
                    delivered_prerequisite_ids=(mode.delivered_prerequisite_ids or frozenset())
                    | unavailable.get(pid, {}).get("siblings", frozenset()),
                    default_prerequisite_ids=mode.default_prerequisite_ids
                    | unavailable.get(pid, {}).get("default", frozenset()),
                ) for pid, mode in modes.items()
            }
            display_predicates = {
                name: predicate for name, predicate in claim_frontier_predicates(display_modes).items()
                if name in {
                    "sibling_prerequisite_not_delivered", "prerequisite_not_on_default_branch",
                }
            }
        async with self._engine.connect() as conn:
            row = (await conn.execute(
                select(
                    *(predicate.label(name) for name, predicate in predicates.items()),
                    *(predicate.label("display_" + name)
                      for name, predicate in display_predicates.items()),
                )
                .where(tasks.c.id == task_id, tasks.c.status == TaskStatus.READY.value)
            )).mappings().one_or_none()
        if row is None:
            return []
        exclusions = [
            {
                "code": ("delivery_evidence_unavailable"
                         if name in display_predicates and row["display_" + name]
                         else f"frontier_{name}"),
                "detail": (
                    "Delivery evidence has not loaded yet; claim frontier eligibility "
                    f"for {name} is unknown (snapshot_unavailable)"
                    if name in display_predicates and row["display_" + name]
                    else "Pool claim frontier excludes this READY task: "
                    + FRONTIER_PREDICATE_DETAILS[name]
                ),
                "ref": task_id,
            }
            for name in predicates if not row[name]
        ]
        if not row["epic_refresh_pending"]:
            async with self._engine.connect() as conn:
                batches = (await conn.execute(
                    open_epic_refresh_batches().where(tasks.c.id == task_id)
                    .order_by(integration_batches.c.id)
                )).scalars().all()
            for exclusion in exclusions:
                if exclusion["code"] == "frontier_epic_refresh_pending" and batches:
                    exclusion["batch_id"] = batches[0]
                    exclusion["batch_ids"] = batches
                    exclusion["detail"] += "; blocking epic-refresh batch: " + ", ".join(batches)
        if not row["prerequisite_not_on_default_branch"]:
            from src.database.tables import task_dependencies

            source = tasks.alias("explain_cross_source")
            dependent = tasks.alias("explain_cross_dependent")
            mode = next(iter(modes.values()), None)
            async with self._engine.connect() as conn:
                sources = (await conn.execute(select(source.c.id, source.c.parent_task_id)
                    .select_from(task_dependencies.join(source,
                        source.c.id == task_dependencies.c.depends_on_task_id)
                        .join(dependent, dependent.c.id == task_dependencies.c.task_id))
                    .where(dependent.c.id == task_id, task_dependencies.c.dep_type == "blocks",
                           source.c.status == "COMPLETED",
                           source.c.parent_task_id.is_distinct_from(dependent.c.parent_task_id)
                           | source.c.parent_task_id.is_(None)))).all()
            missing = set().union(*(entry["default"] for entry in unavailable.values()))
            exclusions.extend({
                "code": ("delivery_evidence_unavailable" if tid in missing
                         else "prerequisite_not_on_default_branch"),
                "ref": tid, "epic_id": parent,
                "detail": (f"Prerequisite {tid}: delivery evidence has not loaded yet; "
                           "default-branch delivery is unknown (snapshot_unavailable)"
                           if tid in missing else f"Prerequisite {tid} (epic {parent or tid}) "
                           "is not proven on the default branch"),
                }
                for tid, parent in sources if mode is None or tid not in mode.default_prerequisite_ids)
        if cached_only:
            for exclusion in exclusions:
                if exclusion["code"] in {
                    "frontier_sibling_prerequisite_not_delivered",
                    "frontier_prerequisite_not_on_default_branch",
                    "frontier_epic_refresh_pending",
                    "prerequisite_not_on_default_branch",
                }:
                    exclusion["detail"] += (
                        "; delivery read is cache-only; missing or stale evidence withholds work"
                    )
