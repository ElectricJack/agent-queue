"""Read projections over durable hierarchical-integration state."""

from __future__ import annotations

from sqlalchemy import and_, func, literal, or_, select

from src.database.tables import (
    integration_batches,
    integration_branch_owners,
    integration_candidate_resolutions,
    integration_operation_artifact_pins,
    integration_parent_episodes,
    integration_promotion_intents,
    integration_repair_operations,
    integration_repair_stages,
    repos,
    sessions,
    task_integration_checkpoints,
    task_metadata,
    tasks,
    workspaces,
)


def session_attached_clause():
    """A ``sessions`` row that may still write for the task it names.

    ``stopped`` is terminal -- ``_SESSION_TRANSITIONS`` never revives the row --
    and a claim activates only on a running session, so a row stopped in both
    ``state`` and ``desired_state`` is history.  ``claim_phase`` is deliberately
    not consulted: ``_terminate_pool_session_locked`` keeps a confirmed-stopped
    writer's claim as handoff evidence while an integration owner retains its
    checkout, and reading that leftover as a writer left an ended operation's
    delegate unreleasable and invisible to ``integration.stranded_delegates``.
    What the claim protects -- the branch owner and the checkout -- is reported
    as its own cleanup blocker.
    """
    return or_(sessions.c.state != "stopped", sessions.c.desired_state != "stopped")


def unresolved_claim_clause():
    """Claim metadata whose exact named session is not durably stopped.

    Claim records survive supported pause and close as provenance. A named
    holder stopped in both observed and desired state cannot act on that
    record, even when its last claim phase or task pointer remains. Branch
    owners and workspaces must still be checked separately. Missing, malformed
    or revivable holders fail closed; this predicate never deletes evidence.
    """
    holder = sessions.alias("claim_history_holder")
    return ~select(holder.c.id).where(
        task_metadata.c.value == literal('"').concat(holder.c.id).concat(literal('"')),
        holder.c.state == "stopped",
        holder.c.desired_state == "stopped",
        holder.c.instance_token.is_not(None),
        holder.c.instance_token != "",
    ).correlate(task_metadata).exists()


#: Operation states in which an operation still runs and owns its delegates.
_LIVE_OPERATION_STATES = ("active", "escalated", "human_required")

#: Stage states no transition leaves while the operation is ``active`` or
#: ``escalated``: every write that sets a stage ``active`` is guarded on
#: ``active``/``awaiting_completion``.  The one way back is a human
#: ``integration resume`` of a ``human_required`` operation's current stage,
#: which is why that state never proves a retirement.
_RETIRED_STAGE_STATES = ("passed", "failed", "expired", "cancelled")


class IntegrationStateQueriesMixin:
    """Integration-state reads; state mutations stay caller-transaction owned."""

    async def get_terminal_integration_delegate_operation(self, task_id: str, *, conn=None):
        """Resolve obsolete repair/verifier work even while its owner is retained."""
        operation = integration_repair_operations
        stage = integration_repair_stages
        statement = select(operation.c.id, operation.c.state).where(
            operation.c.state.in_(("completed", "cancelled")),
            or_(operation.c.verifier_task_id == task_id,
                select(stage.c.operation_id).where(
                    stage.c.operation_id == operation.c.id,
                    stage.c.repair_task_id == task_id,
                ).correlate(operation).exists()),
        ).order_by(operation.c.updated_at.desc(), operation.c.id).limit(1)
        if conn is None:
            async with self._engine.connect() as owned:
                return await self.get_terminal_integration_delegate_operation(task_id, conn=owned)
        row = (await conn.execute(statement)).mappings().one_or_none()
        return dict(row) if row else None

    async def get_retired_integration_writer(self, task_id: str, *, conn=None) -> dict | None:
        """Prove that every integration writer seat *task_id* held is gone for good.

        A seat is a repair stage naming the task as its writer, or an operation
        naming it as verifier.  A seat is retired when its operation ended
        (``completed``/``cancelled``), or -- for a stage -- when the stage is
        terminal and its ``active``/``escalated`` operation has moved its
        ``active_stage`` past it.  A close of such a delegate can never be
        accepted again, so a worker that acknowledged its drain over the claim
        has nothing left to do (bold-impact-53).

        Anything a running operation could still hand back answers ``None``: a
        pending, active or awaiting-completion stage; any stage of a
        ``human_required`` operation, whose resume may revive it with this
        same writer; a terminal stage that is still the operation's current
        one; the verifier, parent or candidate-resolution seat of a running
        operation; and a task that never held a seat.  So does an ``accepted``
        candidate repair, whatever its operation's state: its pass close --
        or ``reconcile_stopped_accepted_delegates`` for a stopped writer that
        kept its claim -- still completes it truthfully, and treating it as
        retired would trade that completion for a ``superseded`` failure.  The
        proof is read from durable state only -- never from the task row or
        its status.
        """
        if conn is None:
            async with self._engine.connect() as owned:
                return await self.get_retired_integration_writer(task_id, conn=owned)
        operation = integration_repair_operations
        stage = integration_repair_stages
        resolution = integration_candidate_resolutions
        accepted = (
            await conn.execute(
                select(resolution.c.id).where(
                    resolution.c.repair_task_id == task_id,
                    resolution.c.state == "accepted",
                ).limit(1)
            )
        ).first()
        if accepted is not None:
            return None
        live_seat = (
            await conn.execute(
                select(operation.c.id).where(
                    operation.c.state.in_(_LIVE_OPERATION_STATES),
                    or_(
                        operation.c.parent_task_id == task_id,
                        operation.c.verifier_task_id == task_id,
                        select(resolution.c.id).where(
                            resolution.c.operation_id == operation.c.id,
                            resolution.c.repair_task_id == task_id,
                        ).correlate(operation).exists(),
                    ),
                ).limit(1)
            )
        ).first()
        if live_seat is not None:
            return None
        seats: list[dict] = [
            {**dict(row), "role": "repair_stage"}
            for row in (
                await conn.execute(
                    select(
                        operation.c.id.label("operation_id"),
                        operation.c.state.label("operation_state"),
                        operation.c.active_stage,
                        operation.c.updated_at,
                        stage.c.ordinal.label("stage"),
                        stage.c.state.label("stage_state"),
                    )
                    .select_from(stage.join(operation, operation.c.id == stage.c.operation_id))
                    .where(
                        stage.c.repair_task_id == task_id,
                        stage.c.writer_kind.in_(("repair_delegate", "existing_verifier")),
                    )
                )
            ).mappings()
        ]
        seats.extend(
            {**dict(row), "role": "verifier", "stage": None, "stage_state": None}
            for row in (
                await conn.execute(
                    select(
                        operation.c.id.label("operation_id"),
                        operation.c.state.label("operation_state"),
                        operation.c.active_stage,
                        operation.c.updated_at,
                    ).where(operation.c.verifier_task_id == task_id)
                )
            ).mappings()
        )
        retired = []
        for seat in seats:
            ended = seat["operation_state"] in ("completed", "cancelled")
            superseded_stage = (
                seat["role"] == "repair_stage"
                and seat["operation_state"] in ("active", "escalated")
                and seat["stage_state"] in _RETIRED_STAGE_STATES
                and int(seat["active_stage"]) != int(seat["stage"])
            )
            if not ended and not superseded_stage:
                return None
            retired.append(seat)
        if not retired:
            return None
        seat = max(retired, key=lambda item: (float(item["updated_at"]), item["operation_id"]))
        if seat["operation_state"] in ("completed", "cancelled"):
            reason = f"integration operation {seat['operation_id']} is {seat['operation_state']}"
        else:
            reason = (
                f"repair stage {seat['stage']} of integration operation "
                f"{seat['operation_id']} is {seat['stage_state']}; the operation moved on "
                f"to stage {seat['active_stage']}"
            )
        cancelled = "cancelled" in (seat["operation_state"], seat["stage_state"])
        return {
            "operation_id": seat["operation_id"],
            "operation_state": seat["operation_state"],
            "role": seat["role"],
            "stage": seat["stage"],
            "stage_state": seat["stage_state"],
            "active_stage": int(seat["active_stage"]),
            "disposition": "cancelled" if cancelled else "superseded",
            "reason": reason,
        }

    async def get_integration_delegate_cleanup(self, task_id: str, *, conn=None) -> list[dict]:
        """Name what a delegate still holds, without releasing any of it.

        Retiring a delegate settles its ticket; these are the preserved
        resources that must still be cleared, each through its own guarded
        control.  An empty list means the delegate holds nothing.
        """
        if conn is None:
            async with self._engine.connect() as owned:
                return await self.get_integration_delegate_cleanup(task_id, conn=owned)
        owners = integration_branch_owners
        blockers: list[dict] = []
        for row in (await conn.execute(
            select(owners).where(
                owners.c.owner_id == task_id, owners.c.handoff_state != "released",
            ).order_by(owners.c.repository_id, owners.c.ref)
        )).mappings():
            blockers.append({
                "code": "branch_owner_retained", "owner_row_id": row["id"],
                "repository_id": row["repository_id"], "ref": row["ref"],
                "handoff_state": row["handoff_state"], "session_id": row["session_id"],
                "workspace_id": row["workspace_id"],
            })
        for row in (await conn.execute(
            select(workspaces.c.id, workspaces.c.workspace_path)
            .where(workspaces.c.locked_by_task_id == task_id)
            .order_by(workspaces.c.id)
        )).mappings():
            blockers.append({
                "code": "workspace_locked", "workspace_id": row["id"],
                "workspace_path": row["workspace_path"],
            })
        for row in (await conn.execute(
            select(sessions.c.id, sessions.c.state).where(
                sessions.c.task_id == task_id, session_attached_clause(),
            ).order_by(sessions.c.id)
        )).mappings():
            blockers.append({
                "code": "session_attached", "session_id": row["id"], "state": row["state"],
            })
        return blockers

    async def get_integration_checkpoint(self, task_id: str) -> dict | None:
        statement = select(task_integration_checkpoints).where(
            task_integration_checkpoints.c.task_id == task_id
        )
        async with self._engine.connect() as conn:
            row = (await conn.execute(statement)).mappings().one_or_none()
        return dict(row) if row is not None else None

    async def get_integration_batch(self, batch_id: str) -> dict | None:
        statement = select(integration_batches).where(integration_batches.c.id == batch_id)
        async with self._engine.connect() as conn:
            row = (await conn.execute(statement)).mappings().one_or_none()
        return dict(row) if row is not None else None

    async def get_integration_operation(self, operation_id: str) -> dict | None:
        statement = select(integration_repair_operations).where(
            integration_repair_operations.c.id == operation_id
        )
        async with self._engine.connect() as conn:
            row = (await conn.execute(statement)).mappings().one_or_none()
        return dict(row) if row is not None else None

    async def get_integration_operation_artifact_route(
        self, operation_id: str
    ) -> dict | None:
        """Return an operation's immutable owner route, if it has one.

        The activation id is retained for audit only.  Admission joins the
        stable playbook/scope address to the currently enabled activation and
        uses the normalized operation pin as the artifact identity.
        """

        statement = (
            select(
                integration_repair_operations.c.route_playbook_id.label(
                    "playbook_id"
                ),
                integration_repair_operations.c.route_scope.label("scope"),
                integration_repair_operations.c.route_scope_identifier.label(
                    "scope_identifier"
                ),
                integration_repair_operations.c.route_activation_id.label(
                    "activation_id"
                ),
                func.coalesce(
                    integration_batches.c.project_id,
                    tasks.c.project_id,
                    repos.c.project_id,
                ).label("project_id"),
                integration_repair_operations.c.artifact_snapshot,
                integration_operation_artifact_pins.c.artifact_sha256,
            )
            .select_from(
                integration_repair_operations
                .outerjoin(
                    integration_operation_artifact_pins,
                    integration_operation_artifact_pins.c.operation_id
                    == integration_repair_operations.c.id,
                )
                .outerjoin(
                    integration_batches,
                    integration_batches.c.id == integration_repair_operations.c.batch_id,
                )
                .outerjoin(
                    tasks,
                    tasks.c.id == integration_repair_operations.c.parent_task_id,
                )
                .outerjoin(
                    integration_parent_episodes,
                    and_(
                        integration_parent_episodes.c.parent_task_id
                        == integration_repair_operations.c.parent_task_id,
                        integration_parent_episodes.c.id == integration_repair_operations.c.episode_id,
                    ),
                )
                .outerjoin(repos, repos.c.id == integration_parent_episodes.c.repository_id)
            )
            .where(integration_repair_operations.c.id == operation_id)
        )
        async with self._engine.connect() as conn:
            row = (await conn.execute(statement)).mappings().one_or_none()
        if row is None or not any(row.values()):
            return None
        return dict(row)

    async def get_active_integration_repair_for_task(
        self, repair_task_id: str
    ) -> dict | None:
        """Resolve one repair task's current active, nonterminal operation."""
        statement = (
            select(
                integration_repair_operations,
                integration_repair_stages.c.starting_sha.label("stage_starting_sha"),
                integration_repair_stages.c.current_subject.label("stage_subject"),
                integration_repair_stages.c.dossier.label("stage_dossier"),
                integration_repair_stages.c.writer_kind.label("writer_kind"),
                integration_repair_stages.c.retained_workspace_id.label(
                    "retained_workspace_id"
                ),
                integration_repair_stages.c.retained_handoff.label(
                    "retained_handoff"
                ),
            )
            .select_from(
                integration_repair_operations.join(
                    integration_repair_stages,
                    and_(
                        integration_repair_stages.c.operation_id
                        == integration_repair_operations.c.id,
                        integration_repair_stages.c.ordinal
                        == integration_repair_operations.c.active_stage,
                    ),
                )
            )
            .where(integration_repair_stages.c.repair_task_id == repair_task_id)
            .where(integration_repair_stages.c.writer_kind == "repair_delegate")
            .where(
                integration_repair_stages.c.state.in_(("active", "awaiting_completion"))
            )
            .where(
                integration_repair_operations.c.state.in_(
                    ("active", "escalated")
                )
            )
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(statement)).mappings().all()
        return dict(rows[0]) if len(rows) == 1 else None

    async def get_parent_repair_prime_context(
        self, repair_task_id: str, *, session_id: str
    ) -> dict | None:
        """Read the current conflict and attached fence, never a historical dossier."""
        async with self._engine.connect() as conn:
            scope = await self.get_repair_filing_scope(
                repair_task_id, session_id=session_id, conn=conn
            )
            if (
                not scope or not scope["active"]
                or scope["target_kind"] != "parent"
                or scope["writer_kind"] != "repair_delegate"
            ):
                return None
            session = (await conn.execute(
                select(sessions).where(sessions.c.id == session_id)
            )).mappings().one()
            task = (await conn.execute(
                select(tasks).where(tasks.c.id == repair_task_id)
            )).mappings().one()
            if session["lifecycle"] == "pool" and (
                session["claim_phase"] != "active"
                or session["last_claim_epoch"] != task["claim_epoch"]
            ):
                return None
            intent = (await conn.execute(
                select(integration_promotion_intents).where(
                    integration_promotion_intents.c.id == scope["trigger_id"],
                    integration_promotion_intents.c.operation_key == scope["operation_id"],
                    integration_promotion_intents.c.project_id == scope["project_id"],
                    integration_promotion_intents.c.target_task_id == scope["parent_task_id"],
                    integration_promotion_intents.c.repository_id == scope["repository_id"],
                    integration_promotion_intents.c.state.in_(
                        ("conflict", "resolution_reserved")
                    ),
                )
            )).mappings().one_or_none()
            subject = scope["current_subject"] or {}
            if (
                intent is None
                or str(intent["target_branch"]).removeprefix("refs/heads/")
                != str(task["branch_name"]).removeprefix("refs/heads/")
                or subject.get("kind") != "parent"
                or not subject.get("head_sha")
                or subject["head_sha"] not in {
                    intent["expected_target"], intent["resolution_head_sha"]
                }
            ):
                return None
            return {
                "operation_id": scope["operation_id"],
                "stage": scope["stage"],
                "intent_id": intent["id"],
                **{key: intent[key] for key in (
                    "state", "source_task_id", "source_base", "source_head",
                    "target_task_id", "expected_target", "conflict_diagnostics",
                )},
                "fence": {
                    "target": {
                        "repository_id": intent["repository_id"],
                        "branch": intent["target_branch"],
                    },
                    "owner_id": repair_task_id,
                    "token": scope["fence_token"],
                },
            }

    async def get_repair_filing_scope(
        self, repair_task_id: str, *, session_id: str | None = None, conn=None
    ) -> dict | None:
        """Resolve a delegate's server-owned logical filing scope, including expiry."""

        async def read(connection):
            statement = (
                select(
                    integration_repair_operations,
                    integration_repair_stages.c.ordinal.label("stage_ordinal"),
                    integration_repair_stages.c.state.label("stage_state"),
                    integration_repair_stages.c.writer_kind.label("writer_kind"),
                    integration_repair_stages.c.trigger_id.label("stage_trigger_id"),
                    integration_repair_stages.c.current_subject.label("current_subject"),
                    integration_repair_stages.c.deadline_at.label("stage_deadline_at"),
                )
                .select_from(
                    integration_repair_operations.join(
                        integration_repair_stages,
                        integration_repair_stages.c.operation_id
                        == integration_repair_operations.c.id,
                    )
                )
                .where(
                    integration_repair_stages.c.repair_task_id == repair_task_id,
                    integration_repair_stages.c.writer_kind.in_(
                        ("repair_delegate", "existing_verifier")
                    ),
                )
                .with_for_update()
            )
            rows = (await connection.execute(statement)).mappings().all()
            if len(rows) != 1:
                return None
            row = dict(rows[0])
            delegate = (
                await connection.execute(
                    select(tasks).where(tasks.c.id == repair_task_id)
                )
            ).mappings().one_or_none()
            if delegate is None:
                return None
            if row["target_kind"] == "parent":
                from src.database.queries.task_identity import resolve_task_identity_on

                target = await resolve_task_identity_on(connection, row["parent_task_id"])
                if target is None:
                    return None
                target_project_id = target.project_id
                repository_id = target.repo_id
                branch = target.branch_name
                parent_task_id = target.task_id
            elif row["target_kind"] == "batch":
                target = (
                    await connection.execute(
                        select(integration_batches).where(
                            integration_batches.c.id == row["batch_id"]
                        )
                    )
                ).mappings().one_or_none()
                if target is None:
                    return None
                target_project_id = target["project_id"]
                repository_id = target["repository_id"]
                branch = target["integration_branch"]
                parent_task_id = None
            else:
                return None
            if (
                delegate["project_id"] != target_project_id
                or delegate["repo_id"] != repository_id
                or str(delegate["branch_name"] or "").removeprefix("refs/heads/")
                != str(branch or "").removeprefix("refs/heads/")
            ):
                return None
            owner = (
                await connection.execute(
                    select(integration_branch_owners)
                    .where(
                        integration_branch_owners.c.repository_id == repository_id,
                        integration_branch_owners.c.ref == branch,
                    )
                    .with_for_update()
                )
            ).mappings().one_or_none()
            workspace = None
            if owner is not None and owner["workspace_id"]:
                workspace = (
                    await connection.execute(
                        select(workspaces)
                        .where(workspaces.c.id == owner["workspace_id"])
                        .with_for_update()
                    )
                ).mappings().one_or_none()
            attached_session = (
                await connection.execute(
                    select(sessions)
                    .where(sessions.c.id == session_id)
                    .with_for_update()
                )
            ).mappings().one_or_none() if session_id else None
            active = bool(
                int(row["active_stage"]) == int(row["stage_ordinal"])
                and row["state"] in {"active", "escalated"}
                and row["stage_state"] in {"active", "awaiting_completion"}
                and delegate["status"] in {"ASSIGNED", "IN_PROGRESS"}
                and owner is not None
                and owner["owner_id"] == repair_task_id
                and (row["writer_kind"], owner["owner_role"])
                in {
                    ("repair_delegate", "repair"),
                    ("existing_verifier", "verifier"),
                }
                and owner["handoff_state"] == "attached"
                and owner["session_id"] == session_id
                and owner["workspace_id"]
                and attached_session is not None
                and attached_session["task_id"] == repair_task_id
                and attached_session["project_id"] == target_project_id
                and attached_session["state"] in {"starting", "running", "draining"}
                and workspace is not None
                and workspace["id"] == owner["workspace_id"]
                and workspace["locked_by_task_id"] == repair_task_id
                and workspace["project_id"] == target_project_id
                and workspace["enabled"]
                and attached_session["work_dir"] == workspace["workspace_path"]
            )
            return {
                "operation_id": row["id"],
                "target_kind": row["target_kind"],
                "project_id": target_project_id,
                "repository_id": repository_id,
                "parent_task_id": parent_task_id,
                "stage": int(row["stage_ordinal"]),
                "writer_kind": row["writer_kind"],
                "trigger_id": row["stage_trigger_id"],
                "current_subject": row["current_subject"],
                "deadline_at": row["stage_deadline_at"],
                "session_id": owner["session_id"] if owner is not None else None,
                "instance_token": (
                    attached_session["instance_token"]
                    if attached_session is not None
                    else None
                ),
                "workspace_id": owner["workspace_id"] if owner is not None else None,
                "workspace_path": (
                    workspace["workspace_path"] if workspace is not None else None
                ),
                "fence_token": (
                    int(owner["fence_token"]) if owner is not None else None
                ),
                "active": active,
            }

        if conn is not None:
            return await read(conn)
        async with self._engine.connect() as owned_conn:
            return await read(owned_conn)
