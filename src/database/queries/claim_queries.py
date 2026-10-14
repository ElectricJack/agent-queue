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
from collections.abc import Collection, Mapping

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
from sqlalchemy.dialects.postgresql import JSONB

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
    integration_repair_operations,
    integration_repair_stages,
    job_workspace_pins,
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
from src.models import AgentProfile, AgentState, SessionRecord, Task, TaskEvent, TaskStatus, Workspace
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


def active_train_repair_bindings():
    """Current active batch-repair delegate identities, independent of titles."""
    operation = integration_repair_operations.alias("urgent_operation")
    stage = integration_repair_stages.alias("urgent_stage")
    return select(
        stage.c.repair_task_id, operation.c.id.label("operation_id"), operation.c.batch_id,
    ).select_from(stage.join(
        operation, operation.c.id == stage.c.operation_id,
    )).where(
        stage.c.ordinal == operation.c.active_stage,
        stage.c.writer_kind == "repair_delegate",
        stage.c.state.in_(("active", "awaiting_completion")),
        operation.c.target_kind == "batch",
        operation.c.state.in_(("active", "escalated")),
    )


def active_train_repair_tasks():
    bindings = active_train_repair_bindings()
    return bindings.with_only_columns(bindings.selected_columns.repair_task_id)


def active_train_repair(task_id=tasks.c.id):
    return task_id.in_(active_train_repair_tasks())


def _frontier_predicates(hierarchy_mode: ProjectIntegrationMode | None = None):
    """Named acceptance predicates shared by claiming and diagnostics."""
    recovery = task_metadata.alias("frontier_recovery")
    # Metadata is JSON-encoded text. Keep the cast inside a key-guarded
    # CASE so unrelated legacy text values cannot break frontier reads.
    incident = cast(case(
        (recovery.c.key == "supervisor_recovery_incident", recovery.c.value),
        else_=literal("{}"),
    ), JSONB)
    return {
        # Legacy control-plane profiles stay visible for rerouting but never
        # win a pool worker's claim.
        "supervisor_profile": tasks.c.profile_id.is_(None) | (tasks.c.profile_id != "supervisor"),
        "dependency_blocked": tasks.c.is_blocked == 0,
        "already_assigned": tasks.c.assigned_agent_id.is_(None),
        "plan_subtask": tasks.c.is_plan_subtask == 0,
        # Recovery decisions outlive status projection and daemon restarts.
        # retry_allowed is false for an accepted retry too; only hold blocks.
        "recovery_hold": ~exists(select(literal(1)).where(
            recovery.c.task_id == tasks.c.id,
            recovery.c.key == "supervisor_recovery_incident",
            incident["decision"].astext == "hold",
        )),
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


def _pool_role_predicate(operator: bool):
    operator_task = func.coalesce(tasks.c.task_type, "") == "operator"
    return operator_task if operator else ~operator_task


def _pool_routing_predicates(*, operator=None, enforce_routing=False, intelligence_class=None):
    """Match the task's authored class and role to a fixed pool identity.

    An explicit task class never inherits a provider pin. Missing launch
    identity cannot prove compatibility when routing is enforced.
    """
    predicates = []
    if enforce_routing:
        explicit_class = func.nullif(func.trim(tasks.c.intelligence_class), "")
        predicates.append(explicit_class == intelligence_class if intelligence_class else false())
    if operator is not None:
        predicates.append(_pool_role_predicate(operator))
    return predicates


def _pool_profiles_predicate(pool_profiles: Mapping[str, AgentProfile]):
    """Demand compatible with any pool's authored class and role; empty means none."""
    from src.commands.operator_authority import is_operator_profile

    compatible = []
    for profile_id, profile in pool_profiles.items():
        fixed_class = (profile.default_class or "").strip()
        compatible.append(and_(
            tasks.c.profile_id == profile_id,
            *_pool_routing_predicates(
                operator=is_operator_profile(profile), enforce_routing=bool(fixed_class),
                intelligence_class=fixed_class,
            ),
        ))
    return or_(false(), *compatible)


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
    """The frontier for one project, with optional pre-read hierarchy constants.

    A supplied *hierarchy_mode* folds project lookups away; ``None`` keeps
    correlated lookups. *router_ready* adds :func:`route_claimable`, while
    ``None`` leaves routing out for structural reads such as diagnostics.
    """
    predicates = [
        tasks.c.project_id == project_id,
        tasks.c.status == TaskStatus.READY.value,
        *claim_frontier_predicates(hierarchy_mode=hierarchy_mode).values(),
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
    # A stopped writer's saved-WIP resume point (``stranded_work``): consumed
    # by the preparation that continues from it, like the handoff above.
    "failover_resume_checkpoint",
    PREPARE_BACKOFF_UNTIL_KEY,
    PREPARE_BACKOFF_ATTEMPTS_KEY,
    "slot_reset_failure",
    "claim_preparation_failure",
    # The last fence that refused a preparation (``BRANCH_FENCED``). Evidence
    # for the wait, so it goes stale at exactly the same boundary as the
    # ladder it throttles.
    "branch_fenced",
    # The last permanent preparation failure's description and identity
    # (``PREPARE_FAILURE``): diagnosis of the ladder above, never its budget.
    "prepare_failure",
    "stack_prerequisites_conflict",
    "stack_preparation_changed",
)

#: Matches exactly what PostgreSQL's ``double precision`` input accepts here:
#: an optional sign, digits, and an optional fractional part.  Deliberately
#: narrower than ``float8`` (no exponents, no ``NaN``/``Infinity``) because
#: every writer of a numeric metadata value writes a plain timestamp.
_NUMERIC_TEXT = r"^-?[0-9]+(\.[0-9]+)?$"


def numeric_meta_value(column, *, default: str = "0"):
    """Cast numeric metadata to ``Float``; malformed JSON text uses *default*.

    Metadata stores JSON, so a number and a quoted numeric string differ.
    Guard the cast *inside* CASE: PostgreSQL may evaluate AND terms in any
    order, making a separate regex predicate unsafe. Non-numeric backoff
    deadlines read as zero (expired), rather than breaking the entire query.
    See the numeric-metadata regression tests for the quoted-zero outage.
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


def claim_frontier_predicates(hierarchy_modes=None, *, hierarchy_mode=None):
    """All profile-independent acceptance filters, including candidate preparation."""
    predicates = {
        **_frontier_predicates(hierarchy_mode),
        **_claim_preparation_predicates(),
        "hold_label": apply_label_filters(select(tasks.c.id), exclude_hold=True).whereclause,
    }
    # Single-project callers already supplied every hierarchy constant. Keep
    # those predicates; a multi-project CASE would reintroduce correlated fallbacks.
    if hierarchy_mode is not None:
        return predicates
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
    "recovery_hold": "the supervisor recovery hold needs an explicit fenced release",
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
        hierarchy_modes=None,
    ) -> list[dict]:
        """Evaluate the real claim filters for one READY task, without scheduler guesses.

        *router_ready* adds the route filter (:func:`route_claimable`) for a
        project whose router is (not) ready.
        """
        from src.integration.delivery_observer import hierarchy_frontier_modes

        unavailable = {}
        modes = hierarchy_modes if hierarchy_modes is not None else await hierarchy_frontier_modes(
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
        return exclusions

    async def take_claim_slot(self, conn, session_id: str, *, now: float, cap: int | None):
        """CAS the session into ``claiming``; ``(kind, session_or_None)``.

        The happy path is **one** statement: ``UPDATE … RETURNING`` hands
        back the row it just took, so the re-read below only runs when the
        CAS lost (or the dialect predates RETURNING — SQLite < 3.35).
        """
        cond = [
            sessions.c.id == session_id,
            sessions.c.task_id.is_(None),
            sessions.c.claim_phase.is_(None),
            sessions.c.desired_state == "running",
            func.coalesce(sessions.c.claim_refusal["retry_at"].as_float(), 0) <= now,
        ]
        if cap is not None:
            cond.append(sessions.c.claims < cap)
        stmt = (
            update(sessions).where(and_(*cond)).values(claim_phase="claiming", claim_phase_at=now)
        )
        row = None
        if supports_returning(conn):
            row = (await conn.execute(stmt.returning(*sessions.c))).mappings().fetchone()
            took = row is not None
        else:
            took = (await conn.execute(stmt)).rowcount == 1
        if row is None:
            row = (
                (await conn.execute(select(sessions).where(sessions.c.id == session_id)))
                .mappings()
                .fetchone()
            )
        if row is None:
            return "not_found", None
        record = _row_to_session(row)
        if took:
            return "slot", record
        if (record.claim_phase != "active" and record.claim_refusal
                and record.claim_refusal["retry_at"] > now):
            return "claim_refusal", record
        if record.claim_phase in ("active", "preparing", "claiming"):
            return record.claim_phase, record
        if record.desired_state != "running":
            return "drain_requested", record
        if cap is not None and record.claims >= cap:
            return "session_exhausted", record
        if record.task_id:
            return "active", record
        return "not_found", record

    async def claim_preparation_state(
        self, session_id: str, task_id: str, claim_epoch: int, *, conn=None
    ):
        """Return the preparation fence and its current project in one read.

        Slot reset and claim-file creation happen outside the claim
        transaction, so this read must prove both rows are still current.
        The project must be fresh after a long poll. Joining it here avoids
        another projects SELECT without reusing the admission loop's old row.
        An absent project is distinct from a stale preparation fence.
        """
        stmt = (
            select(projects)
            .select_from(tasks.join(sessions, sessions.c.task_id == tasks.c.id)
                         .outerjoin(projects, projects.c.id == tasks.c.project_id))
            .where(
                tasks.c.id == task_id,
                tasks.c.status == TaskStatus.IN_PROGRESS.value,
                tasks.c.claim_epoch == claim_epoch,
                sessions.c.id == session_id,
                sessions.c.claim_phase == "preparing",
                sessions.c.desired_state == "running",
            )
        )
        if conn is not None:
            row = (await conn.execute(stmt)).mappings().fetchone()
        else:
            async with self._engine.connect() as owned:
                row = (await owned.execute(stmt)).mappings().fetchone()
        return (row is not None, self._row_to_project(row) if row and row["id"] else None)

    async def release_claim_slot(self, conn, session_id: str) -> None:
        await conn.execute(
            update(sessions)
            .where(and_(sessions.c.id == session_id, sessions.c.claim_phase == "claiming"))
            .values(claim_phase=None, claim_phase_at=None)
        )

    async def select_ready_for_profile(
        self,
        conn,
        *,
        project_id,
        profile_id,
        agent_id,
        router_ready=False,
        task_id=None,
        enforce_routing=False,
        intelligence_class=None,
        llm_provider=None,
        options_hash=None,
        operator: bool | None = None,
        hierarchy_mode: ProjectIntegrationMode | None = None,
        allowed_task_ids: set[str] | None = None,
        excluded_task_ids: set[str] | None = None,
    ) -> str | None:
        """Select the §10 frontier with FOR UPDATE SKIP LOCKED.

        Allow/exclude sets are request-scoped Git admission decisions, re-proven
        on each attempt. A pre-read *hierarchy_mode* only filters candidates:
        the task is re-fenced under lock and preparation re-reads the project.

        Prefer active train repairs, then this agent's affinity rows, then all admissible rows.
        scans order by priority/created_at so an index can stop at LIMIT 1.
        A CASE affinity sort instead evaluated every candidate's frontier
        subqueries (~10,100 shared buffers versus 10–14 at the §15.2 scale).
        The second scan must include unpinned work even when all pinned rows
        are locked; a restricting InitPlan would incorrectly report no work.
        Targeted claims skip the affinity probe because only one row can match.

        Routing requires the task's own profile and an accepted route source
        (router/override/role, plus legacy before router readiness); no project
        default admits unrouted work. See :func:`route_claimable`.
        """
        profile_ok = tasks.c.profile_id == profile_id

        def candidate(*, pinned: bool, repair_only: bool = False):
            stmt = (
                select(tasks.c.id)
                .where(
                    _frontier_where(project_id, hierarchy_mode, router_ready=bool(router_ready)),
                    profile_ok,
                )
                .order_by(tasks.c.priority.asc(), tasks.c.created_at.asc())
                .limit(1)
            )
            if allowed_task_ids is not None:
                stmt = stmt.where(tasks.c.id.in_(allowed_task_ids))
            if excluded_task_ids:
                stmt = stmt.where(tasks.c.id.not_in(excluded_task_ids))
            if pinned:
                stmt = stmt.where(tasks.c.affinity_agent_id == agent_id)
            if repair_only:
                # The uncorrelated existence gate avoids scanning ordinary
                # tasks when there is no active repair anywhere in the fleet.
                stmt = stmt.where(active_train_repair_tasks().exists(), active_train_repair())
            stmt = stmt.where(*_pool_routing_predicates(
                operator=operator, enforce_routing=enforce_routing,
                intelligence_class=intelligence_class,
            ))
            if task_id is not None:
                stmt = stmt.where(tasks.c.id == task_id)
            return stmt.with_for_update(of=tasks, skip_locked=True)

        if task_id is None:
            # COALESCE probes repairs then affinity lazily in one round trip.
            row = (await conn.execute(select(func.coalesce(
                candidate(pinned=False, repair_only=True).scalar_subquery(),
                candidate(pinned=True).scalar_subquery(),
            )))).fetchone()
            if row and row[0] is not None:
                return row[0]
        row = (await conn.execute(candidate(pinned=False))).fetchone()
        return row[0] if row else None

    async def take_task(self, conn, task_id: str, *, agent_id: str, now: float) -> Task | None:
        """Fence + epoch bump + status write in **one** statement (spec §15).

        The fence (``READY``, unblocked, unassigned) rides the same UPDATE as
        the epoch bump and the ``IN_PROGRESS`` write, so exactly one racer
        can match it; a matched row proves the pre-state, which is what lets
        ``_apply_transition`` skip its pre-read (``assume_pre_state``).  The
        write still goes through ``_apply_transition`` — the single
        sanctioned status-write path — which validates
        ``READY --CLAIMED--> IN_PROGRESS`` on the state machine and skips the
        blocked-state recompute: no clause of ``blocked_predicate()`` can
        tell READY from IN_PROGRESS, so nothing's projection can move
        (``projection_stable``).  Returns ``None`` when the fence lost.
        """
        # Reserving the worker is also its soft-delete fence.  The old
        # SELECT ... FOR UPDATE followed by record_holder's later UPDATE
        # needed two round trips to lock and mark the same row.  This guarded
        # UPDATE does both, while retaining the lock until the task transition
        # commits; a competing soft delete can neither pass its idle predicate
        # nor alter this row underneath the claim.
        reserve = (
            update(agents)
            .where(
                agents.c.id == agent_id,
                agents.c.enabled.is_(True),
                agents.c.role == "worker",
                agents.c.deleted_at.is_(None),
                agents.c.state == AgentState.IDLE.value,
                agents.c.current_task_id.is_(None),
            )
            .values(state=AgentState.BUSY.value, current_task_id=task_id)
        )
        reserved = (
            await conn.execute(reserve.returning(agents.c.id))
        ).scalar_one_or_none() is not None
        if not reserved:
            return None
        out = await self._apply_transition(
            conn,
            task_id,
            TaskStatus.IN_PROGRESS,
            context="claim",
            event=TaskEvent.CLAIMED,
            assigned_agent_id=agent_id,
            projection_stable=True,
            assume_pre_state=(TaskStatus.READY, False),
            extra_where=and_(
                tasks.c.status == TaskStatus.READY.value,
                tasks.c.is_blocked == 0,
                tasks.c.assigned_agent_id.is_(None),
                ~container_flag_exists(),
                ~exists(
                    select(literal(1)).where(
                        integration_repair_stages.c.repair_task_id == tasks.c.id,
                        integration_repair_stages.c.writer_kind == "repair_delegate",
                        integration_repair_stages.c.state.notin_(
                            ("active", "awaiting_completion")
                        ),
                    )
                ),
            ),
            extra_values={"claim_epoch": tasks.c.claim_epoch + 1},
            returning=True,
        )
        if out.row is None:
            await conn.execute(
                update(agents)
                .where(agents.c.id == agent_id, agents.c.current_task_id == task_id)
                .values(state=AgentState.IDLE.value, current_task_id=None)
            )
            return None
        return self._row_to_task(out.row)

    async def bump_claim_epoch(self, task_id: str, *, conn=None) -> int:
        async def _run(c):
            changed = await c.execute(
                update(tasks)
                .where(tasks.c.id == task_id, _not_manually_paused())
                .values(claim_epoch=tasks.c.claim_epoch + 1)
            )
            if changed.rowcount == 0:
                raise ManualPauseActive(f"Task {task_id} is paused or missing; cannot launch.")
            return (
                await c.execute(select(tasks.c.claim_epoch).where(tasks.c.id == task_id))
            ).scalar() or 0

        if conn is not None:
            return await _run(conn)
        async with self.immediate() as conn:
            return await _run(conn)

    async def record_holder(
        self,
        conn,
        *,
        session_id,
        task_id,
        claim_epoch,
        agent_id,
        work_dir,
        now,
        agent_reserved=False,
    ) -> Workspace | None:
        """Write the holder rows; return the agent's workspace slot.

        The workspace UPDATE uses ``RETURNING`` so the caller
        (``_prepare_and_activate``) does not have to re-read the slot it
        just stamped, and both metadata keys go out in one multi-row upsert
        (spec §15).
        """
        holder = (
            update(sessions)
            .where(sessions.c.id == session_id)
            .values(
                task_id=task_id,
                claim_phase="preparing",
                claim_phase_at=now,
                last_claim_epoch=claim_epoch,
            )
        )
        if agent_id and agent_reserved:
            # take_claim_slot already locked this session, and take_task
            # reserved the agent. Feed the holder write into the workspace
            # update so PostgreSQL performs both in one round trip, in order.
            holder = holder.returning(sessions.c.project_id).cte("claim_holder")
            project_id = select(holder.c.project_id).scalar_subquery()
        else:
            await conn.execute(holder)
            project_id = select(sessions.c.project_id).where(
                sessions.c.id == session_id
            ).scalar_subquery()
        slot = None
        if agent_id:
            if not agent_reserved:
                await conn.execute(
                    update(agents)
                    .where(agents.c.id == agent_id)
                    .values(state=AgentState.BUSY.value, current_task_id=task_id)
                )
            # Pool sessions keep the agent lock for their lifetime; claiming
            # another task only changes the task hold within that same slot.
            stmt = (
                update(workspaces)
                .where(
                    workspaces.c.project_id
                    == project_id,
                    workspaces.c.workspace_path == work_dir,
                    workspaces.c.enabled.is_(True),
                    workspaces.c.locked_by_agent_id == agent_id,
                    or_(workspaces.c.locked_by_task_id.is_(None),
                        workspaces.c.locked_by_task_id == task_id),
                )
                .values(
                    locked_by_agent_id=agent_id,
                    locked_by_task_id=task_id,
                    locked_at=now,
                )
            )
            row = (await conn.execute(stmt.returning(*workspaces.c))).mappings().fetchone()
            if row is None:
                row = await self._repair_pool_workspace_lock(
                    conn, session_id=session_id, task_id=task_id,
                    agent_id=agent_id, work_dir=work_dir, now=now,
                )
            slot = self._row_to_workspace(row) if row is not None else None
        await self._upsert_meta_many(
            task_id, {CLAIMED_BY_SESSION_KEY: session_id, "work_dir": work_dir}, conn=conn
        )
        await self._start_task_session_attempt(
            conn,
            session_id,
            started_at=now,
            work_dir=work_dir,
        )
        return slot

    async def _repair_pool_workspace_lock(
        self, conn, *, session_id, task_id, agent_id, work_dir, now,
    ):
        """Move an idle misplaced agent lock to its session's actual checkout.

        Never adopt a foreign lock or erase retained ownership. The session
        row is already locked by claim; workspace rows are locked in id order.
        Branch/checkout reconciliation follows outside this transaction.
        """
        project_id = await conn.scalar(
            select(sessions.c.project_id).where(sessions.c.id == session_id)
        )
        rows = (await conn.execute(
            select(workspaces).where(
                or_(workspaces.c.workspace_path == work_dir,
                    and_(workspaces.c.project_id == project_id,
                         workspaces.c.locked_by_agent_id == agent_id)),
            ).order_by(workspaces.c.id).with_for_update()
        )).mappings().all()
        targets = [row for row in rows if row["workspace_path"] == work_dir
                   and row["project_id"] == project_id]
        if len(targets) != 1:
            return None
        target = targets[0]
        if not target["enabled"] or any(
            row["locked_by_agent_id"] is not None and (
                row["project_id"] != project_id or row["locked_by_agent_id"] != agent_id
            ) for row in rows
        ):
            return None
        if any(row["locked_by_task_id"] not in (None, task_id) for row in rows):
            return None
        from src.claim_file import read_claim_file

        if any(read_claim_file(row["workspace_path"]) is not None for row in rows):
            return None
        paths = [row["workspace_path"] for row in rows]
        ids = [row["id"] for row in rows]
        if await conn.scalar(select(sessions.c.id).where(
            sessions.c.id != session_id,
            sessions.c.work_dir.in_(paths),
            sessions.c.state.in_(("starting", "running", "draining")),
        ).limit(1)):
            return None
        if await conn.scalar(select(integration_branch_owners.c.owner_id).where(
            integration_branch_owners.c.workspace_id.in_(ids),
            integration_branch_owners.c.handoff_state.in_(("attached", "handoff_pending")),
        ).limit(1)):
            return None
        if await conn.scalar(select(job_workspace_pins.c.job_id).where(
            job_workspace_pins.c.workspace_id.in_(ids),
        ).limit(1)):
            return None
        old_rows = [row for row in rows if row["id"] != target["id"]
                    and row["project_id"] == project_id
                    and row["locked_by_agent_id"] == agent_id]
        modes = {row["lock_mode"] for row in old_rows if row["lock_mode"] is not None}
        if len(modes) > 1:
            return None
        lock_mode = next(iter(modes), "exclusive")
        old_ids = [row["id"] for row in old_rows]
        if old_ids:
            await conn.execute(update(workspaces).where(workspaces.c.id.in_(old_ids)).values(
                locked_by_agent_id=None, locked_by_task_id=None, locked_at=None, lock_mode=None,
            ))
        return (await conn.execute(update(workspaces).where(
            workspaces.c.id == target["id"],
        ).values(locked_by_agent_id=agent_id, locked_by_task_id=task_id, locked_at=now,
                 lock_mode=lock_mode)
          .returning(*workspaces.c))).mappings().one()

    async def lock_claim_for_activation(self, conn, session_id, task_id, *, epoch: int):
        """Lock session then task before a caller acquires managed ref exclusion.

        Task identity is immutable here, so FOR NO KEY UPDATE excludes status
        changes while permitting the slot reset's separate salvage transaction
        to take FK key-share locks when inserting task context or metadata.
        """
        holder = (await conn.execute(
            select(sessions.c.id).where(
                sessions.c.id == session_id,
                sessions.c.task_id == task_id,
                sessions.c.claim_phase == "preparing",
                sessions.c.desired_state == "running",
                sessions.c.state == "running",
            ).with_for_update()
        )).scalar_one_or_none()
        if holder is None:
            return None
        return (await conn.execute(
            select(tasks.c.id, tasks.c.branch_name).where(
                tasks.c.id == task_id,
                tasks.c.status == TaskStatus.IN_PROGRESS.value,
                tasks.c.claim_epoch == epoch,
            ).with_for_update(key_share=True)
        )).fetchone()

    async def activate_claim(
        self, session_id, task_id, *, epoch: int, now: float, conn=None,
        branch_name: str | None = None,
        clear_preparation_metadata: bool = False,
        admission=None,
    ) -> SessionRecord | None:
        """Flip ``preparing`` -> ``active``; the updated row, or ``None``.

        PostgreSQL's ``RETURNING`` supplies the response row without a re-read.
        All writes follow the session/task locks and admission guards; a
        rejected activation changes neither the branch nor diagnostics.

        *branch_name* records the slot reset's branch in ``tasks.branch_name``
        in the activation transaction. A failed preparation leaves no branch
        that a later close could mistake for a usable checkout.

        Successful activation resets both refusal streaks and clears stale
        ``claim_refusal`` and ``needs_attention`` metadata. With *clear_preparation_metadata*, it
        also deletes the pause/checkpoint and backoff keys in
        ``CLAIM_PREPARATION_METADATA_KEYS`` in that same transaction.
        """

        async def _run(c):
            # Claims and release acquire the session before the task. Keep
            # activation in that order as well to avoid a PostgreSQL deadlock.
            # Share the task row lock with pause. An EXISTS predicate alone
            # can observe a pre-pause PostgreSQL statement snapshot.
            claim = await self.lock_claim_for_activation(c, session_id, task_id, epoch=epoch)
            if claim is None:
                return None
            if admission is not None and (
                not await admission.matches(conn=c, lock=True, task_id=task_id)
                or (await c.execute(select(blocked_predicate()).where(
                    tasks.c.id == task_id
                ))).scalar_one()
            ):
                return None
            stmt = (
                update(sessions)
                .where(
                    and_(
                        sessions.c.id == session_id,
                        sessions.c.claim_phase == "preparing",
                        sessions.c.task_id == task_id,
                        sessions.c.desired_state == "running",
                        sessions.c.state == "running",
                    )
                )
                .values(
                    claim_phase="active",
                    claim_phase_at=now,
                    claims=sessions.c.claims + 1,
                    last_claim_epoch=epoch,
                    last_claim_result="claimed",
                    claim_failure_count=0,
                    claim_failure_reason=None,
                    claim_failure_at=None,
                    claim_refusal=None,
                )
            )
            row = (await c.execute(stmt.returning(*sessions.c))).mappings().fetchone()
            if row is None:
                return None
            # A claim only becomes usable after preparation has succeeded and
            # this session row has atomically moved to ``active``.  Clear an
            # earlier prepare/release warning at that exact point so it never
            # shadows a live task in the dashboard or recovery logic, and --
            # when the caller asks -- the preparation ladder that became
            # stale at the same boundary, in the same statement.
            stale_keys = ["needs_attention"]
            if clear_preparation_metadata:
                stale_keys.extend(CLAIM_PREPARATION_METADATA_KEYS)
            await c.execute(
                delete(task_metadata).where(
                    task_metadata.c.task_id == task_id,
                    task_metadata.c.key.in_(stale_keys),
                )
            )
            if branch_name and claim.branch_name != branch_name:
                # Written only past every activation guard, and under the row
                # lock taken above: an activation that bails leaves the branch
                # exactly as it found it, so no failed prepare can publish a
                # branch the task never got.  Only a differing value is
                # written: a hierarchy claim's branch already comes from the
                # origin chain, and a no-op UPDATE would touch a task whose
                # materialized origin is deliberately frozen.
                await c.execute(
                    update(tasks)
                    .where(tasks.c.id == task_id)
                    .values(branch_name=branch_name, updated_at=now)
                )
            return _row_to_session(row)

        if conn is not None:
            return await _run(conn)
        async with self.immediate() as conn:
            return await _run(conn)

    async def _release_claim_on(
        self,
        conn,
        session_id,
        *,
        task_status,
        context,
        now,
        result,
        needs_attention,
        prepare_backoff=False,
        preparation_expired_before=None,
        expected_task_id=None,
        expected_claim_epoch=None,
        expected_task_status=None,
        expected_task_claim_epoch=None,
        drain_after_release=False,
        release_workspace_lock=False,
        preserve_terminal_task=False,
        stop_after_release=False,
        end_reason=None,
        resume_after=None,
        task_meta=None,
    ) -> TransitionResult:
        row = (
            (
                await conn.execute(
                    select(sessions).where(sessions.c.id == session_id).with_for_update()
                )
            )
            .mappings()
            .fetchone()
        )
        out = TransitionResult()
        if row is None:
            return out
        task_id, agent_id = row["task_id"], row["agent_id"]
        # Timeout observations can arrive after preparation activated. Check
        # its phase and deadline again while holding the session row lock.
        if preparation_expired_before is not None and (
            row["claim_phase"] not in ("claiming", "preparing")
            or (row["claim_phase_at"] or 0.0) > preparation_expired_before
        ):
            return out
        # A task close may race pool reconciliation: the reconciler can
        # release the terminal hold and the worker can claim new work before
        # the original close resumes.  Never let that old close release the
        # successor's task or claim file.
        if (
            expected_task_id is not None
            and task_id != expected_task_id
            or expected_claim_epoch is not None
            and row["last_claim_epoch"] != expected_claim_epoch
        ):
            return out
        # An attached integration owner is durable evidence that this exact
        # session is still responsible for its workspace.  Do not clear the
        # claim or either workspace binding until its handoff completes: a
        # pool reconciler can otherwise destroy the evidence a repair or
        # integration close needs.  Lock the owner row in this transaction so
        # the decision composes with ownership handoff on Postgres too.
        if agent_id:
            protected_owner = (
                await conn.execute(
                    select(integration_branch_owners.c.id)
                    .join(
                        workspaces,
                        integration_branch_owners.c.workspace_id == workspaces.c.id,
                    )
                    .where(
                        integration_branch_owners.c.session_id == session_id,
                        integration_branch_owners.c.fence.is_(None),
                        integration_branch_owners.c.handoff_state.in_(
                            ("attached", "handoff_pending")
                        ),
                        workspaces.c.locked_by_agent_id == agent_id,
                    )
                    .with_for_update()
                )
            ).first()
            if protected_owner is not None:
                return out
        epoch = None
        if task_id:
            if preserve_terminal_task:
                # A close can commit the terminal task transition before it
                # loses its response during a daemon restart.  Recovery must
                # free only the stale holder -- re-applying COMPLETED would
                # rewrite completion timing/context and blur the reviewed
                # terminal record it is meant to preserve.
                terminal = (
                    await conn.execute(
                        select(tasks.c.claim_epoch)
                        .where(
                            tasks.c.id == task_id,
                            tasks.c.status == task_status.value,
                            tasks.c.claim_epoch == expected_claim_epoch,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if terminal is None:
                    return out
                epoch = terminal
            else:
                # ``projection_stable``: IN_PROGRESS -> READY cannot move any
                # task's ``is_blocked`` (see ``_PROJECTION_NEUTRAL_STATUSES``);
                # it is ignored for every other target status, so the FAILED /
                # BLOCKED releases keep the full recompute.  ``returning`` folds
                # what used to be a separate ``claim_epoch`` read into the write.
                transition = dict(
                    context=context,
                    force=True,
                    assigned_agent_id=None,
                    projection_stable=True,
                    returning=True,
                    # Releasing the worker's ownership must not resume or alter
                    # an explicit manual pause.  It only clears its stale agent
                    # assignment after a task moved out from under the claim.
                    _manual_pause_control=task_status is TaskStatus.PAUSED,
                )
                if resume_after is not None:
                    # An automatic pause with a backoff, never a manual hold --
                    # and never over one that landed meanwhile.
                    transition["resume_after"] = resume_after
                    if expected_task_status is None:
                        transition["extra_where"] = tasks.c.status.in_(
                            (TaskStatus.ASSIGNED.value, TaskStatus.IN_PROGRESS.value)
                        )
                if expected_task_status is not None:
                    guards = [tasks.c.status == expected_task_status.value]
                    if expected_task_claim_epoch is not None:
                        guards.append(tasks.c.claim_epoch == expected_task_claim_epoch)
                    transition["extra_where"] = and_(*guards)
                elif task_status == TaskStatus.READY:
                    transition.update(
                        assume_pre_state=(TaskStatus.IN_PROGRESS, False),
                        extra_where=and_(
                            tasks.c.status == TaskStatus.IN_PROGRESS.value,
                            tasks.c.is_blocked == 0,
                        ),
                    )
                out = await self._apply_transition(
                    conn,
                    task_id,
                    task_status,
                    **transition,
                )
                if expected_task_status is not None and out.row is None:
                    return out
                epoch = (out.row or {}).get("claim_epoch")
                if needs_attention:
                    await self._upsert_meta(task_id, "needs_attention", needs_attention, conn=conn)
                if out.row is not None:
                    for key, value in (task_meta or {}).items():
                        await self._upsert_meta(task_id, key, value, conn=conn)
                if prepare_backoff:
                    raw_attempts = (
                        await conn.execute(
                            select(task_metadata.c.value).where(
                                task_metadata.c.task_id == task_id,
                                task_metadata.c.key == PREPARE_BACKOFF_ATTEMPTS_KEY,
                            )
                        )
                    ).scalar_one_or_none()
                    try:
                        attempts = int(json.loads(raw_attempts)) if raw_attempts else 0
                    except (TypeError, ValueError, json.JSONDecodeError):
                        attempts = 0
                    attempts += 1
                    delay = min(
                        PREPARE_BACKOFF_INITIAL_SECONDS * (2 ** (attempts - 1)),
                        PREPARE_BACKOFF_MAX_SECONDS,
                    )
                    await self._upsert_meta_many(
                        task_id,
                        {
                            PREPARE_BACKOFF_ATTEMPTS_KEY: attempts,
                            PREPARE_BACKOFF_UNTIL_KEY: now + delay,
                        },
                        conn=conn,
                    )
        if task_id:
            await self.finish_task_session_attempt(
                session_id,
                task_id=task_id,
                ended_at=now,
                end_reason=end_reason or needs_attention or context,
                conn=conn,
            )
        if task_id:
            from src.integration.lock import release_session_leases_on

            await release_session_leases_on(self, conn, session_id, task_id)
        if agent_id:
            # Release only this claim's task lock (or its idle agent lock).
            # Misplaced agent bindings must not erase another task's lock
            # when preparation discovers and refuses that mismatch.
            await conn.execute(
                update(workspaces)
                .where(
                    workspaces.c.locked_by_agent_id == agent_id,
                    or_(workspaces.c.locked_by_task_id.is_(None),
                        workspaces.c.locked_by_task_id == task_id),
                )
                .values(
                    locked_by_agent_id=None if release_workspace_lock else workspaces.c.locked_by_agent_id,
                    locked_by_task_id=None,
                )
            )
            await conn.execute(
                update(agents)
                .where(agents.c.id == agent_id)
                .values(state=AgentState.IDLE.value, current_task_id=None)
            )
        session_values = dict(
            task_id=None,
            claim_phase=None,
            claim_phase_at=None,
            last_claim_epoch=epoch,
            last_claim_result=result,
        )
        if drain_after_release:
            session_values["desired_state"] = "stopped"
        if stop_after_release:
            session_values.update(
                state="stopped",
                desired_state="stopped",
                end_reason=end_reason or context,
                ended_at=now,
            )
        await conn.execute(
            update(sessions).where(sessions.c.id == session_id).values(**session_values)
        )
        out.released = True
        return out

    async def _after_release(self, out: TransitionResult) -> None:
        await self.log_blocked_flips(out.flipped)
        await self._notify_settled(out.settled)
        await self._notify_ready(out.ready)

    async def release_claim(
        self,
        session_id,
        *,
        task_status,
        context,
        now,
        result="released",
        needs_attention=None,
        expected_task_id=None,
        expected_claim_epoch=None,
        expected_task_status=None,
        expected_task_claim_epoch=None,
        drain_after_release=False,
        release_workspace_lock=False,
        prepare_backoff=False,
        preparation_expired_before=None,
        preserve_terminal_task=False,
        stop_after_release=False,
        end_reason=None,
        conn=None,
    ) -> TransitionResult:
        kwargs = dict(
            task_status=task_status,
            context=context,
            now=now,
            result=result,
            needs_attention=needs_attention,
            expected_task_id=expected_task_id,
            expected_claim_epoch=expected_claim_epoch,
            expected_task_status=expected_task_status,
                expected_task_claim_epoch=expected_task_claim_epoch,
            drain_after_release=drain_after_release,
            release_workspace_lock=release_workspace_lock,
            prepare_backoff=prepare_backoff,
            preparation_expired_before=preparation_expired_before,
            preserve_terminal_task=preserve_terminal_task,
            stop_after_release=stop_after_release,
            end_reason=end_reason,
        )
        if conn is not None:
            return await self._release_claim_on(conn, session_id, **kwargs)
        async with self.immediate() as conn:
            out = await self._release_claim_on(conn, session_id, **kwargs)
        await self._after_release(out)
        return out

    async def list_container_claims(self) -> list[dict]:
        """Every agent whose current task is a container it did not fill (bold-flare-35).

        The claim frontier never offers a container, but a task can become
        one *after* it was claimed: a planner files an epic as a plain task
        and reparents its children under it, and a pool worker leases the
        epic in between (prime-glacier.1).  Its holder then has nothing to
        do — the work is in the children — and cannot close it while they
        are open.

        A task gains children legitimately while held, too: a worker files
        emergent work under the task it holds (swarm-work-model §12), and a
        supervisor may file a follow-up under a task someone is working on.
        What separates those from an epic is who filed what, so a claim is
        reported only when both hold:

        * **none** of the task's children was filed by the session holding
          it (``created_by_kind='session'``, ``created_by_id`` = that
          session), and
        * at least one child was filed by the **same session that filed the
          task itself** — the planner that created the epic and then gave it
          its children.  A follow-up filed by anyone else leaves the holder
          alone.

        The holder is the session pointing at the task through this agent in
        any state but ``stopped`` (a ``sleeping`` session on a rate-limit
        cooldown still holds its claim).  An agent row with no such session
        is reported on the same rule when its task is ``IN_PROGRESS``; an
        ``ASSIGNED`` one may simply be waiting for its session to start.

        Returns one dict per agent: ``agent_id``, ``agent_state``,
        ``task_id``, ``project_id``, ``task_status``, ``task_claim_epoch``,
        and — ``None`` for an orphaned agent row — ``session_id``,
        ``lifecycle`` and ``claim_epoch`` (the session's last claim epoch).
        """
        filer_child = tasks.alias("container_claim_filer_child")
        own_child = tasks.alias("container_claim_own_child")
        holder = sessions.alias("container_claim_holder")
        stmt = (
            select(
                agents.c.id.label("agent_id"),
                agents.c.state.label("agent_state"),
                tasks.c.id.label("task_id"),
                tasks.c.project_id.label("project_id"),
                tasks.c.status.label("task_status"),
                tasks.c.claim_epoch.label("task_claim_epoch"),
                holder.c.id.label("session_id"),
                holder.c.lifecycle.label("lifecycle"),
                holder.c.last_claim_epoch.label("claim_epoch"),
            )
            .select_from(
                agents.join(tasks, tasks.c.id == agents.c.current_task_id).outerjoin(
                    holder,
                    and_(
                        holder.c.task_id == tasks.c.id,
                        holder.c.agent_id == agents.c.id,
                        holder.c.state != "stopped",
                    ),
                )
            )
            .where(
                tasks.c.status.in_(
                    (TaskStatus.ASSIGNED.value, TaskStatus.IN_PROGRESS.value)
                ),
                holder.c.id.is_not(None) | (tasks.c.status == TaskStatus.IN_PROGRESS.value),
                tasks.c.created_by_kind == "session",
                exists(
                    select(literal(1)).where(
                        filer_child.c.parent_task_id == tasks.c.id,
                        filer_child.c.created_by_kind == "session",
                        filer_child.c.created_by_id == tasks.c.created_by_id,
                    )
                ),
                ~exists(
                    select(literal(1)).where(
                        own_child.c.parent_task_id == tasks.c.id,
                        own_child.c.created_by_kind == "session",
                        own_child.c.created_by_id == holder.c.id,
                    )
                ),
            )
            .order_by(agents.c.id)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).mappings().all()
        return [dict(row) for row in rows]

    async def release_container_claim(self, claim: dict, *, now: float) -> TransitionResult:
        """Give a claimed container back to its children (bold-flare-35).

        *claim* is a row of :meth:`list_container_claims`.  The task goes back
        to the shape every container lives in — ``IN_PROGRESS`` with no agent
        (``creator.PARENT_STATUS``) — and settles at once if its children are
        already done.  A live holder's session is released and drained, so
        the pool frees the seat and relaunches a fresh worker instead of
        leaving one idle on a claim it can never close.  An agent row with
        no live session is reset the same way.  Every write is fenced on the
        observed task status and epochs; a claim that moved on is left alone
        and ``released`` stays ``False``.
        """
        task_id, agent_id = claim["task_id"], claim["agent_id"]
        task_status = TaskStatus(claim["task_status"])
        if claim.get("session_id"):
            out = await self.release_claim(
                claim["session_id"],
                task_status=TaskStatus.IN_PROGRESS,
                context=CONTAINER_CLAIM_RELEASED,
                now=now,
                result="container_released",
                expected_task_id=task_id,
                expected_claim_epoch=claim.get("claim_epoch"),
                expected_task_status=task_status,
                expected_task_claim_epoch=claim.get("task_claim_epoch"),
                drain_after_release=True,
                end_reason=CONTAINER_CLAIM_RELEASED,
            )
        else:
            async with self.immediate() as conn:
                out = await self._apply_transition(
                    conn,
                    task_id,
                    TaskStatus.IN_PROGRESS,
                    context=CONTAINER_CLAIM_RELEASED,
                    force=True,
                    assigned_agent_id=None,
                    projection_stable=True,
                    returning=True,
                    extra_where=and_(
                        tasks.c.status == task_status.value,
                        tasks.c.claim_epoch == claim.get("task_claim_epoch"),
                        tasks.c.assigned_agent_id.is_(None)
                        | (tasks.c.assigned_agent_id == agent_id),
                    ),
                )
                if out.row is not None:
                    await conn.execute(
                        update(workspaces)
                        .where(
                            workspaces.c.locked_by_agent_id == agent_id,
                            workspaces.c.locked_by_task_id == task_id,
                        )
                        .values(locked_by_task_id=None)
                    )
                    cleared = await conn.execute(
                        update(agents)
                        .where(agents.c.id == agent_id, agents.c.current_task_id == task_id)
                        .values(state=AgentState.IDLE.value, current_task_id=None)
                    )
                    out.released = cleared.rowcount == 1
            await self._after_release(out)
        if out.released:
            async with self._engine.begin() as conn:
                # A parent caught by ``has_children`` alone may lack the flag,
                # and settlement acts only on flagged containers.
                await self.mark_container(task_id, conn=conn)
                settled = await self.settle_containers({task_id}, conn=conn)
            await self._after_release(settled)
        return out

    async def stale_container_claim_candidates(self) -> list[str]:
        """Containers a stopped pool worker still holds (sharp-ridge-57).

        The backstop's read of :func:`stale_container_claim_clauses`, without
        locks, so the sweep pays for one indexed statement per interval and not
        for the release.  A row that moves on between this read and
        :meth:`release_stale_container_claim` is simply not released.
        """
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stale_container_claim_statement())).mappings().all()
        return [row["id"] for row in rows]

    async def release_stale_container_claim(
        self, task_id: str, *, conn=None, now: float | None = None
    ) -> TransitionResult:
        """Take back a stopped pool worker's claim on a container it cannot close.

        The automatic counterpart of the ``reopen-collection`` /
        ``recover-parent-head`` supervisor controls, which all of them refuse
        while a parent is IN_PROGRESS (they need an unassigned PAUSED one):
        a container is never leased, so a stopped holder's claim is the last
        thing standing between a finished aggregate and the parent runtime
        that would verify it (bold-crest-75).

        The release is proved, not assumed.  Under ``FOR UPDATE`` on the task,
        the same :func:`stale_container_claim_clauses` the backstop read selects
        the row — the holder, its claim epoch and the whole clause set are
        re-checked under that lock, so the scan and this repair can never
        disagree about what qualifies and neither can act on a stale read.  The
        holder's agent is then checked for other work, any workspace still
        locked to the task or to that agent refuses the release, and an operator
        hold refuses it too: a reconciler never supersedes a human pause.

        The claim is released through
        :meth:`release_historical_pool_claim`, which changes only the exact
        task and the exact session: a stopped worker's slot or agent may since
        have been reused, so its workspace lock, agent state and claim file are
        left exactly as the successor found them.  It is handed the task's own
        ``claimed_by_session`` record as the holder's authority
        (``claim_record_holder``), because that is all a real stop leaves
        behind (fleet-delta-97), and clears it in the same transaction so the
        next sweep cannot read the same claim again.  The task lands ``PAUSED``
        with no agent, which is the shape §7 settlement and the parent-episode
        readiness projection both consume.  When *conn* is supplied the caller
        owns the transaction (and the post-commit notifications); otherwise
        this method opens its own, runs the container sweep for the same
        parent, and notifies once.
        """
        if conn is not None:
            return await self._release_stale_container_claim_on(
                conn, task_id, now=time.time() if now is None else now
            )
        async with self.immediate() as owned:
            out = await self._release_stale_container_claim_on(
                owned, task_id, now=time.time() if now is None else now
            )
            if out.released:
                settled = await self.settle_containers({task_id}, conn=owned)
                out.flipped |= settled.flipped
                out.settled.extend(settled.settled)
                out.ready.extend(settled.ready)
        await self._after_release(out)
        return out

    async def _release_stale_container_claim_on(
        self, conn, task_id: str, *, now: float
    ) -> TransitionResult:
        """The transactional body of :meth:`release_stale_container_claim`."""
        out = TransitionResult()
        if (
            await conn.execute(
                select(tasks.c.id).where(tasks.c.id == task_id).with_for_update()
            )
        ).first() is None:
            return out
        claimed = (
            await conn.execute(stale_container_claim_statement(task_ids=[task_id]))
        ).mappings().one_or_none()
        if claimed is None:
            return out
        holder = (
            (
                await conn.execute(
                    select(sessions)
                    .where(sessions.c.id == claimed["session_id"])
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        agent_id = (holder or {}).get("agent_id")
        if agent_id:
            agent = (
                (
                    await conn.execute(
                        select(agents).where(agents.c.id == agent_id).with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if agent is not None and agent["current_task_id"] not in {None, task_id}:
                return out
        # A workspace still locked to the task, or to the holder's agent, means
        # the writer may be alive after all, or that a successor has already
        # taken the slot the release would free.
        locks = [workspaces.c.locked_by_task_id == task_id]
        if agent_id:
            locks.append(workspaces.c.locked_by_agent_id == agent_id)
        if (
            await conn.execute(select(workspaces.c.id).where(or_(*locks)).limit(1))
        ).first() is not None:
            return out
        if await self._read_manual_pause(conn, task_id) is not None:
            return out
        out = await self.release_historical_pool_claim(
            conn,
            claimed["session_id"],
            task_id=task_id,
            claim_epoch=claimed["last_claim_epoch"],
            now=now,
            context=STALE_CONTAINER_CLAIM_RELEASED,
            expected_task_status=TaskStatus.IN_PROGRESS,
            claim_record_holder=True,
        )
        if out.released:
            await self.log_event(
                "task." + STALE_CONTAINER_CLAIM_RELEASED,
                project_id=claimed["project_id"],
                task_id=task_id,
                payload=json.dumps(
                    {
                        "session_id": claimed["session_id"],
                        "claim_epoch": claimed["last_claim_epoch"],
                        "from_status": TaskStatus.IN_PROGRESS.value,
                    }
                ),
                conn=conn,
            )
        return out

    async def release_displaced_pool_claim(self, session_id: str, *, now: float) -> bool:
        """Detach a draining pool session from a task it no longer owns.

        A closed task may be requeued and claimed elsewhere before an old
        session is drained.  In that case the old task pointer is history,
        not authority to transition the new holder's task.  Lock in the same
        session-then-task order as claim release so a concurrent claim cannot
        change the ownership proof underneath this cleanup.
        """
        async with self.immediate() as conn:
            row = (
                await conn.execute(
                    select(sessions)
                    .where(sessions.c.id == session_id)
                    .with_for_update()
                )
            ).mappings().one_or_none()
            if (
                row is None
                or row["lifecycle"] != "pool"
                or row["desired_state"] != "stopped"
                or row["task_id"] is None
            ):
                return False
            task_id, agent_id = row["task_id"], row["agent_id"]
            task = (
                await conn.execute(
                    select(tasks.c.assigned_agent_id, tasks.c.claim_epoch)
                    .where(tasks.c.id == task_id)
                    .with_for_update()
                )
            ).mappings().one_or_none()
            if task is not None and (
                task["assigned_agent_id"] == agent_id
                and (row["last_claim_epoch"] is None
                     or task["claim_epoch"] == row["last_claim_epoch"])
            ):
                return False
            # An attached integration writer keeps the old session and slot
            # as its recovery evidence even after the task status changes.
            if agent_id and (
                await conn.execute(
                    select(integration_branch_owners.c.id)
                    .join(workspaces, integration_branch_owners.c.workspace_id == workspaces.c.id)
                    .where(
                        integration_branch_owners.c.session_id == session_id,
                        integration_branch_owners.c.fence.is_(None),
                        integration_branch_owners.c.handoff_state.in_(
                            ("attached", "handoff_pending")
                        ),
                        workspaces.c.locked_by_agent_id == agent_id,
                    )
                    .with_for_update()
                )
            ).first():
                return False
            await conn.execute(
                update(sessions)
                .where(sessions.c.id == session_id)
                .values(task_id=None, claim_phase=None, claim_phase_at=None,
                        last_claim_result="displaced")
            )
            await self.finish_task_session_attempt(
                session_id, task_id=task_id, ended_at=now,
                end_reason="displaced", conn=conn,
            )
            # Only remove stale references on the old worker.  A successor
            # may already be running the same task on another worker.
            if agent_id and (
                task is None
                or task["assigned_agent_id"] != agent_id
            ):
                await conn.execute(
                    update(agents)
                    .where(agents.c.id == agent_id, agents.c.current_task_id == task_id)
                    .values(state=AgentState.IDLE.value, current_task_id=None)
                )
                await conn.execute(
                    update(workspaces)
                    .where(workspaces.c.locked_by_agent_id == agent_id,
                           workspaces.c.locked_by_task_id == task_id)
                    .values(locked_by_task_id=None)
                )
            return True

    async def get_settled_pool_claim(self, session_id: str, *, conn=None) -> dict | None:
        """Prove a draining pool session's held task is terminal and truthfully settled.

        ``aq task close`` commits the terminal transition first and only then
        hands the branch back, saves the completion record and releases the
        claim.  A daemon restart in between leaves the session bound to a
        finished task while its attached branch owner keeps
        :meth:`release_displaced_pool_claim` from detaching it, so the drained
        worker sat idle until a supervisor killed it (wise-willow).  All of
        these must hold:

        * the session is a pool row wanting ``stopped`` that still names the
          task, and its last claim epoch is the task's: the claim that went
          terminal is this session's, not a pointer at requeued work;
        * the task is ``COMPLETED`` or ``FAILED`` and no agent holds it;
        * the close was recorded after this session's attempt on the task
          began: a completion record, or a ``code``/``noop`` delivery receipt
          (the restart can lose the first; delivery writes the second).
          Before either exists -- the record was never saved and delivery
          has not run (bold-impact-53) -- the close's accepted-close marker
          (``ACCEPTED_CLOSE_KEY``) counts when it names this session and its
          last claim epoch, because the transition that accepted the close
          wrote it in the same transaction.  ``close_session_id`` never
          counts: it is written before the close is accepted, so it outlives
          a refused close and proves nothing about who ended the claim;
        * no running integration operation owns the task in any seat
          (``live_integration_owner``) -- such a seat can still hand work back.

        Returns the proof, or ``None``.  It never reads or changes the branch
        owner: stopping the session leaves that to owner recovery.
        """
        if conn is None:
            async with self._engine.connect() as owned:
                return await self.get_settled_pool_claim(session_id, conn=owned)
        session = (
            await conn.execute(
                select(
                    sessions.c.task_id, sessions.c.lifecycle, sessions.c.desired_state,
                    sessions.c.last_claim_epoch,
                ).where(sessions.c.id == session_id)
            )
        ).mappings().one_or_none()
        if (
            session is None
            or session["lifecycle"] != "pool"
            or session["desired_state"] != "stopped"
            or session["task_id"] is None
            or session["last_claim_epoch"] is None
        ):
            return None
        task_id = session["task_id"]
        task = (
            await conn.execute(
                select(tasks.c.status, tasks.c.assigned_agent_id, tasks.c.claim_epoch)
                .where(tasks.c.id == task_id)
            )
        ).mappings().one_or_none()
        if (
            task is None
            or task["status"] not in SETTLED_POOL_CLAIM_STATUSES
            or task["assigned_agent_id"] is not None
            or task["claim_epoch"] != session["last_claim_epoch"]
        ):
            return None
        attempt_started = (
            await conn.execute(
                select(func.max(task_session_attempts.c.started_at)).where(
                    task_session_attempts.c.session_id == session_id,
                    task_session_attempts.c.task_id == task_id,
                )
            )
        ).scalar_one_or_none()
        if attempt_started is None:
            return None
        evidence = None
        record = (
            await conn.execute(
                select(task_completion_records.c.id, task_completion_records.c.outcome)
                .where(
                    task_completion_records.c.task_id == task_id,
                    task_completion_records.c.completed_at >= attempt_started,
                )
                .order_by(task_completion_records.c.completed_at.desc())
                .limit(1)
            )
        ).mappings().first()
        if record is not None:
            evidence = {
                "kind": "completion_record",
                "id": record["id"],
                "detail": f"completion record {record['id']} (outcome {record['outcome']})",
            }
        else:
            receipt = (
                await conn.execute(
                    select(task_delivery_receipts.c.id, task_delivery_receipts.c.disposition)
                    .where(
                        task_delivery_receipts.c.source_task_id == task_id,
                        task_delivery_receipts.c.disposition.in_(("code", "noop")),
                        task_delivery_receipts.c.created_at >= attempt_started,
                    )
                    .order_by(task_delivery_receipts.c.created_at.desc())
                    .limit(1)
                )
            ).mappings().first()
            if receipt is not None:
                evidence = {
                    "kind": "delivery_receipt",
                    "id": receipt["id"],
                    "detail": f"{receipt['disposition']} delivery receipt {receipt['id']}",
                }
        if evidence is None:
            marker = (
                await conn.execute(
                    select(task_metadata.c.value).where(
                        task_metadata.c.task_id == task_id,
                        task_metadata.c.key == ACCEPTED_CLOSE_KEY,
                    )
                )
            ).scalar_one_or_none()
            try:
                accepted = json.loads(marker) if marker is not None else None
            except ValueError:
                accepted = None
            if (
                isinstance(accepted, dict)
                and accepted.get("session_id") == session_id
                and accepted.get("claim_epoch") == session["last_claim_epoch"]
            ):
                evidence = {
                    "kind": "accepted_close",
                    "id": accepted.get("completion_id"),
                    "detail": (
                        f"accepted close {accepted.get('completion_id')} by session "
                        f"{session_id} (claim epoch {accepted['claim_epoch']})"
                    ),
                }
        if evidence is None:
            return None
        from src.integration.delegate_release import live_integration_owner

        if await live_integration_owner(conn, [task_id]) is not None:
            return None
        return {
            "task_id": task_id,
            "status": task["status"],
            "claim_epoch": int(task["claim_epoch"]),
            "evidence": evidence,
            "reason": f"task {task_id} is {task['status']} with {evidence['detail']}",
        }

    async def release_historical_pool_claim(
        self,
        conn,
        session_id: str,
        *,
        task_id: str,
        claim_epoch: int,
        now: float,
        context: str = "integration_handoff_recovery",
        expected_task_status: TaskStatus = TaskStatus.BLOCKED,
        claim_record_holder: bool = False,
    ) -> TransitionResult:
        """Release a stopped historical claim without touching its former holder.

        This is intentionally not a ``release_claim`` mode.  That normal path
        unwinds every workspace lock held by the session's agent and clears the
        agent's current task, which is correct for a live holder but corrupts a
        slot or agent that has since been reused.  The caller proves the
        detached integration handoff separately while holding its owner row;
        this method changes only the exact old task and exact old session.

        *context* and *expected_task_status* name the repair's own audit trail
        and the one pre-state it is allowed to move off.  The task is always
        returned to ``PAUSED`` with no agent, because a stopped session has no
        authority left to hold anything; only the status it was proved to be in
        is the caller's to choose (``integration_handoff_recovery`` has always
        meant a stranded ``BLOCKED`` repair delegate, while
        :meth:`release_stale_container_claim` proves an ``IN_PROGRESS``
        container).

        *claim_record_holder* says whose claim this is in the shape the real
        stop path leaves (fleet-delta-97): the session's own ``task_id`` is
        already ``NULL`` and the task's ``claimed_by_session`` record is the
        only surviving statement of the claim, so that record — read and
        cleared under this transaction's locks — is the authority instead of
        the session pointer.  Every other clause is unchanged, and the other
        callers keep the pointer as their authority.
        """
        row = (
            await conn.execute(
                select(sessions).where(sessions.c.id == session_id).with_for_update()
            )
        ).mappings().one_or_none()
        out = TransitionResult()
        if (
            row is None
            or row["lifecycle"] != "pool"
            or row["state"] != "stopped"
            or row["desired_state"] != "stopped"
            or row["last_claim_epoch"] != claim_epoch
        ):
            return out
        if claim_record_holder:
            if row["task_id"] not in (None, task_id) or row["claim_phase"] in CLAIM_PHASE_IN_FLIGHT:
                return out
            record = (
                await conn.execute(
                    select(task_metadata.c.value)
                    .where(
                        task_metadata.c.task_id == task_id,
                        task_metadata.c.key == CLAIMED_BY_SESSION_KEY,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            try:
                named = json.loads(record) if record is not None else None
            except ValueError:
                return out
            if named != session_id:
                return out
        elif row["task_id"] != task_id or row["claim_phase"] != "active":
            return out

        out = await self._apply_transition(
            conn,
            task_id,
            TaskStatus.PAUSED,
            context=context,
            force=True,
            assigned_agent_id=None,
            _manual_pause_control=True,
            extra_where=and_(
                tasks.c.status == expected_task_status.value,
                tasks.c.assigned_agent_id.is_(None),
                tasks.c.claim_epoch == claim_epoch,
            ),
            returning=True,
        )
        if out.row is None:
            return out
        session_fence = and_(
            sessions.c.id == session_id,
            sessions.c.lifecycle == "pool",
            sessions.c.state == "stopped",
            sessions.c.desired_state == "stopped",
            sessions.c.last_claim_epoch == claim_epoch,
        )
        if claim_record_holder:
            session_fence = and_(
                session_fence,
                or_(sessions.c.task_id.is_(None), sessions.c.task_id == task_id),
                or_(
                    sessions.c.claim_phase.is_(None),
                    sessions.c.claim_phase.notin_(CLAIM_PHASE_IN_FLIGHT),
                ),
            )
        else:
            session_fence = and_(
                session_fence,
                sessions.c.task_id == task_id,
                sessions.c.claim_phase == "active",
            )
        released = await conn.execute(
            update(sessions).where(session_fence).values(
                task_id=None,
                claim_phase=None,
                claim_phase_at=None,
                last_claim_result=context,
            )
        )
        if released.rowcount != 1:
            # The task transition and session release are one atomic repair.
            # A changed holder after the row was observed is not a partial
            # recovery; make the surrounding transaction roll back.
            raise RuntimeError("historical pool claim release lost its fence")
        if claim_record_holder:
            # The record is the claim, so releasing the claim releases it: a
            # stale one left behind is what this whole repair exists to find.
            cleared = await conn.execute(
                delete(task_metadata).where(
                    task_metadata.c.task_id == task_id,
                    task_metadata.c.key == CLAIMED_BY_SESSION_KEY,
                    task_metadata.c.value == json.dumps(session_id),
                )
            )
            if cleared.rowcount != 1:
                raise RuntimeError("historical pool claim release lost its claim record")
        await self.finish_task_session_attempt(
            session_id,
            task_id=task_id,
            ended_at=now,
            end_reason=context,
            conn=conn,
        )
        out.released = True
        return out

    async def terminate_pool_session(
        self,
        session_id,
        *,
        reason,
        task_status=TaskStatus.READY,
        resume_after=None,
        task_meta=None,
        conn=None,
    ) -> TransitionResult:
        """Release a stopped pool session's claim, workspace and worker.

        *resume_after* and *task_meta* serve a provider-caused pause
        (provider-failover D13/D17): the held task goes ``PAUSED`` with its
        backoff and its ``provider_pause`` record in this one transaction.
        """
        async def _run(c):
            out = await self._release_claim_on(
                c,
                session_id,
                task_status=task_status,
                context=f"session_{reason}",
                end_reason=reason,
                now=time.time(),
                result="released",
                needs_attention=None,
                resume_after=resume_after,
                task_meta=task_meta,
            )
            if not out.released:
                return out
            row = (
                await c.execute(select(sessions.c.agent_id).where(sessions.c.id == session_id))
            ).fetchone()
            agent_id = row[0] if row else None
            if agent_id:
                await self.release_workspaces_for_agent(agent_id, conn=c)
                await c.execute(
                    update(agents)
                    .where(agents.c.id == agent_id)
                    .values(state=AgentState.RETIRED.value, current_task_id=None)
                )
            return out

        if conn is not None:
            return await _run(conn)
        async with self.immediate() as c:
            out = await _run(c)
        await self._after_release(out)
        return out

    async def lock_filing_scope(self, conn, task_ids: list[str]) -> dict[str, str | None]:
        """Lock the task rows a worker filing's scope is derived from.

        Returns ``{id: parent_task_id}`` for the rows that exist (a missing
        id is simply absent). On Postgres the filing first takes the same
        project-scoped advisory transaction lock as ``set_parent``,
        serializing scope reads with every hierarchy move without contending
        on ordinary project-row updates. It then row-locks the requested
        tasks in ascending id order so deletion cannot invalidate the result.
        The shared hierarchy lock covers intermediate ancestors: moving one
        waits even though it is not itself named by the filing. On SQLite
        ``immediate()`` already holds the database write lock.

        Called first in the filing transaction, before ``reserve_filing``
        takes the same row lock on the held task by writing to it.
        """
        anchor_id = task_ids[0] if task_ids else None
        ids = sorted(set(task_ids))
        if not ids:
            return {}
        project_id = await conn.scalar(select(tasks.c.project_id).where(tasks.c.id == anchor_id))
        if project_id is None:
            return {}
        await self.lock_hierarchy_project(conn, project_id)
        stmt = (
            select(tasks.c.id, tasks.c.parent_task_id)
            .where(tasks.c.id.in_(ids))
            .order_by(tasks.c.id)
            .with_for_update()
        )
        rows = (await conn.execute(stmt)).fetchall()
        return {r.id: r.parent_task_id for r in rows if r.id in ids}

    async def reserve_filing(
        self, conn, task_id: str, *, max_filings: int, count: int = 1
    ) -> bool:
        """Atomically reserve ``count`` worker filings against one held task.

        The guarded increment is deliberately one statement: graph filing
        spends its whole node batch together, so two concurrent graphs at a
        quota boundary cannot each observe room for part of the other.
        """
        if count <= 0:
            raise ValueError("filing reservation count must be positive")
        res = await conn.execute(
            update(tasks)
            .where(
                and_(
                    tasks.c.id == task_id,
                    tasks.c.filed_count + count <= max_filings,
                )
            )
            .values(filed_count=tasks.c.filed_count + count)
        )
        return res.rowcount == 1

    async def count_ready_by_profile(
        self, project_id: str, *, allowed_task_ids=None, router_ready: bool | None = None,
        hierarchy_mode: ProjectIntegrationMode | None = None,
        pool_profiles: Mapping[str, AgentProfile] | None = None,
        include_repair_counts: bool = False,
    ) -> dict[str | None, int] | dict[str | None, tuple[int, int]]:
        """Count structural work, restricted to verified development admission when supplied.

        *router_ready* counts only claimable work (:func:`route_claimable`),
        which is what pool demand is (mandatory routing §9.1); ``None``
        counts every READY frontier row, unrouted ones under ``None``.

        *pool_profiles* restricts sizing demand to the fixed class and operator
        role each pool can execute. A stale route must not start a session that
        cannot claim its task. Structural callers may omit this snapshot.
        With *include_repair_counts*, values are ``(ready, train_repairs)``;
        both counts use the same frontier and one database round trip.
        """
        if hierarchy_mode is None:
            from src.integration.delivery_observer import hierarchy_frontier_modes

            hierarchy_mode = (await hierarchy_frontier_modes(
                self, project_ids={project_id}
            )).get(project_id)
        columns = [tasks.c.profile_id, func.count()]
        if include_repair_counts:
            columns.append(func.count().filter(active_train_repair()))
        stmt = select(*columns).where(
            _frontier_where(project_id, hierarchy_mode, router_ready=router_ready),
        ).group_by(tasks.c.profile_id)
        if allowed_task_ids is not None:
            stmt = stmt.where(tasks.c.id.in_(allowed_task_ids))
        if pool_profiles is not None:
            stmt = stmt.where(_pool_profiles_predicate(pool_profiles))
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).fetchall()
        if include_repair_counts:
            return {pid: (int(n), int(repairs)) for pid, n, repairs in rows}
        return {pid: int(n) for pid, n in rows}
