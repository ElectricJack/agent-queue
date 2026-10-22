"""Exact operation locks and ambiguity checks shared by integration repair recovery.

``OperationSafety`` serves the repair, cancelled-collection, suspended-parent and
failed-verification recovery paths. The unbound subject writer primitives that
once lived here were retired with the primitive port adapters they served.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import and_, select

from src.database.queries.task_queries import TERMINAL_BLOCKED_META_KEY
from src.database.tables import (
    integration_attestation_publications,
    integration_batches,
    integration_branch_owners,
    integration_candidate_publications,
    integration_candidate_ref_mutations,
    integration_candidate_resolutions,
    integration_cleanup_items,
    integration_parent_episodes,
    integration_promotion_intents,
    integration_repair_stages,
    sessions,
    task_integration_checkpoints,
    task_metadata,
    tasks,
    workspaces,
)
from src.models import TaskStatus


class OperationSafety:
    """Exact operation locks and ambiguity checks shared by subject primitives."""

    def __init__(self, db):
        self.db = db

    async def _restore_parent_collection_on(
        self,
        conn: Any,
        operation: dict[str, Any],
        *,
        allow_paused: bool,
        validate_only: bool = False,
    ) -> tuple[Any | None, str | None]:
        """Restore only the terminally blocked parent for this exact episode.

        The transition owns terminal-block metadata and the blocked projection;
        the surrounding checks are repeated in its UPDATE guard so an old
        recovery request cannot revive a parent a newer writer has claimed.
        """
        if operation["target_kind"] != "parent":
            return None, None
        parent = (
            await conn.execute(
                select(tasks)
                .where(tasks.c.id == operation["parent_task_id"])
                .with_for_update()
            )
        ).mappings().one_or_none()
        checkpoint = (
            await conn.execute(
                select(task_integration_checkpoints)
                .where(task_integration_checkpoints.c.task_id == operation["parent_task_id"])
                .with_for_update()
            )
        ).mappings().one_or_none()
        episode = (
            await conn.execute(
                select(integration_parent_episodes)
                .where(integration_parent_episodes.c.id == operation["episode_id"])
                .with_for_update()
            )
        ).mappings().one_or_none()
        if (
            parent is None
            or checkpoint is None
            or episode is None
            or checkpoint["episode_id"] != operation["episode_id"]
            or episode["parent_task_id"] != operation["parent_task_id"]
            or parent["repo_id"] != checkpoint["repository_id"]
            or parent["branch_name"] != checkpoint["branch"]
            or episode["repository_id"] != parent["repo_id"]
            or int(checkpoint["generation"]) < int(episode["generation"])
        ):
            return None, "stale"
        if await self.db._read_manual_pause(conn, operation["parent_task_id"]) is not None:
            return None, "invalid_state"
        if parent["assigned_agent_id"] is not None:
            return None, "invalid_state"
        if parent["status"] == TaskStatus.PAUSED.value:
            return (None, None) if allow_paused else (None, "invalid_state")
        terminal_context = (
            await conn.execute(
                select(task_metadata.c.value).where(
                    task_metadata.c.task_id == operation["parent_task_id"],
                    task_metadata.c.key == TERMINAL_BLOCKED_META_KEY,
                )
            )
        ).scalar_one_or_none()
        encoded_terminal_context = terminal_context
        try:
            terminal_context = json.loads(terminal_context)
        except (TypeError, ValueError):
            pass  # Older metadata may store the context without JSON encoding.
        if (
            parent["status"] != TaskStatus.BLOCKED.value
            or terminal_context != "integration_repair_exhausted"
        ):
            return None, "invalid_state"
        if validate_only:
            return None, None
        transition = await self.db._apply_transition(
            conn,
            operation["parent_task_id"],
            TaskStatus.PAUSED,
            context="integration_repair_resume",
            force=True,
            resume_after=None,
            assigned_agent_id=None,
            extra_where=and_(
                tasks.c.status == TaskStatus.BLOCKED.value,
                tasks.c.assigned_agent_id.is_(None),
                select(task_metadata.c.task_id)
                .where(
                    task_metadata.c.task_id == operation["parent_task_id"],
                    task_metadata.c.key == TERMINAL_BLOCKED_META_KEY,
                    task_metadata.c.value == encoded_terminal_context,
                )
                .exists(),
            ),
            returning=True,
        )
        if transition.row is None:
            return None, "stale"
        return transition, None

    @staticmethod
    async def _ambiguous_writes_on(
        conn: Any,
        operation: dict[str, Any],
        *,
        allow_reserved_delegate: bool = False,
        allowed_writer_id: str | None = None,
        allowed_promotion_intent_id: str | None = None,
    ) -> list[str]:
        operation_id = operation["id"]
        writer = select(integration_branch_owners.c.id).where(
            (
                integration_branch_owners.c.owner_id.in_(
                    select(integration_repair_stages.c.repair_task_id).where(
                        integration_repair_stages.c.operation_id == operation_id,
                        integration_repair_stages.c.repair_task_id.is_not(None),
                    )
                )
            )
            | (
                integration_branch_owners.c.owner_id.in_(
                    select(integration_candidate_ref_mutations.c.branch_owner_id).where(
                        integration_candidate_ref_mutations.c.operation_id
                        == operation_id
                    )
                )
            ),
            integration_branch_owners.c.handoff_state != "released",
        )
        if allow_reserved_delegate:
            exact_delegate = select(
                integration_repair_stages.c.repair_task_id
            ).where(
                integration_repair_stages.c.operation_id == operation_id,
                integration_repair_stages.c.ordinal == operation["active_stage"],
                integration_repair_stages.c.writer_kind == "repair_delegate",
            )
            writer = writer.where(
                ~and_(
                    integration_branch_owners.c.owner_id.in_(exact_delegate),
                    integration_branch_owners.c.owner_role == "repair",
                    integration_branch_owners.c.handoff_state == "reserved",
                    integration_branch_owners.c.session_id.is_(None),
                    integration_branch_owners.c.workspace_id.is_(None),
                )
            )
            # A rejected reservation excuses only the exact stopped writer
            # that created it. A successor with the same task id is a live
            # ambiguous writer until its own handoff completes.
            resolution = integration_candidate_resolutions.c
            owner = integration_branch_owners.c
            exact_rejected_writer = select(resolution.id).select_from(
                integration_candidate_resolutions.join(
                    sessions, sessions.c.id == resolution.repair_session_id
                ).join(
                    workspaces, workspaces.c.id == resolution.repair_workspace_id
                ).join(
                    integration_repair_stages,
                    (integration_repair_stages.c.operation_id == resolution.operation_id)
                    & (integration_repair_stages.c.ordinal == resolution.stage_ordinal),
                )
            ).where(
                resolution.operation_id == operation_id,
                resolution.stage_ordinal == operation["active_stage"],
                resolution.state == "rejected",
                integration_repair_stages.c.repair_task_id == resolution.repair_task_id,
                integration_repair_stages.c.writer_kind == "repair_delegate",
                owner.owner_id == resolution.repair_task_id,
                owner.owner_role == "repair",
                owner.fence_token == resolution.fence_token,
                owner.handoff_state == "attached",
                owner.session_id == resolution.repair_session_id,
                owner.workspace_id == resolution.repair_workspace_id,
                sessions.c.task_id == resolution.repair_task_id,
                sessions.c.project_id == resolution.project_id,
                sessions.c.instance_token == resolution.repair_session_instance_token,
                sessions.c.state == "stopped",
                sessions.c.desired_state == "stopped",
                sessions.c.work_dir == resolution.repair_workspace_path,
                workspaces.c.project_id == resolution.project_id,
                workspaces.c.locked_by_task_id == resolution.repair_task_id,
                workspaces.c.workspace_path == resolution.repair_workspace_path,
            ).correlate(integration_branch_owners).exists()
            writer = writer.where(
                ~exact_rejected_writer
            )
        if allow_reserved_delegate and operation["batch_id"] is not None:
            # A detached collector is the daemon's reservation, not a live
            # worker. Applied writes are checked separately below; retain all
            # ambiguity guards for attached collectors and other targets.
            exact_target = select(integration_batches.c.id).where(
                integration_batches.c.id == operation["batch_id"],
                integration_batches.c.repository_id == integration_branch_owners.c.repository_id,
                integration_batches.c.integration_branch == integration_branch_owners.c.ref,
            ).exists()
            writer = writer.where(
                ~and_(
                    integration_branch_owners.c.owner_id == operation_id,
                    integration_branch_owners.c.owner_role == "collector",
                    exact_target,
                    integration_branch_owners.c.handoff_state == "reserved",
                    integration_branch_owners.c.session_id.is_(None),
                    integration_branch_owners.c.workspace_id.is_(None),
                )
            )
        if allowed_writer_id is not None:
            writer = writer.where(integration_branch_owners.c.id != allowed_writer_id)
        promotion = select(integration_promotion_intents.c.id).where(
            (integration_promotion_intents.c.operation_key == operation_id)
            | (integration_promotion_intents.c.resolution_operation_id == operation_id),
            integration_promotion_intents.c.state.not_in(("committed", "conflict", "superseded")),
        )
        if allowed_promotion_intent_id is not None:
            promotion = promotion.where(
                integration_promotion_intents.c.id != allowed_promotion_intent_id
            )
        statements = {
            "ref_mutation": select(integration_candidate_ref_mutations.c.id).where(
                integration_candidate_ref_mutations.c.operation_id == operation_id,
                integration_candidate_ref_mutations.c.state == "reserved",
            ),
            "resolution": select(integration_candidate_resolutions.c.id).where(
                integration_candidate_resolutions.c.operation_id == operation_id,
                integration_candidate_resolutions.c.state.in_(("reserved", "pushed")),
            ),
            "attestation": select(integration_attestation_publications.c.id).where(
                integration_attestation_publications.c.operation_id == operation_id,
                integration_attestation_publications.c.state == "reserved",
            ),
            "promotion": promotion,
            "writer": writer,
        }
        if operation["batch_id"] is not None:
            statements["candidate_publication"] = select(
                integration_candidate_publications.c.batch_id
            ).where(
                integration_candidate_publications.c.batch_id == operation["batch_id"],
                integration_candidate_publications.c.state != "pr_published",
            )
            if allow_reserved_delegate:
                # pr_reserved follows a confirmed ref write. Audit-PR metadata
                # may remain pending without making that branch write uncertain.
                pub = integration_candidate_publications.c
                mutation = integration_candidate_ref_mutations.c
                applied_ref = select(mutation.id).where(
                    mutation.purpose == "candidate_final",
                    mutation.state == "applied",
                    mutation.operation_id == operation_id,
                    mutation.operation_episode_id == operation["episode_id"],
                    mutation.batch_id == pub.batch_id,
                    mutation.revision == pub.revision,
                    mutation.repository_id == pub.repository_id,
                    mutation.target_branch == "refs/heads/" + pub.head_ref,
                    mutation.branch == mutation.target_branch,
                    mutation.target_branch == select(integration_batches.c.integration_branch)
                        .where(integration_batches.c.id == operation["batch_id"]).scalar_subquery(),
                    mutation.expected_old_sha == pub.expected_old_sha,
                    mutation.desired_sha == pub.head_sha,
                    mutation.remote_sha == pub.head_sha,
                ).exists()
                statements["candidate_publication"] = statements["candidate_publication"].where(
                    ~and_(pub.state == "pr_reserved", applied_ref)
                )
            statements["cleanup_prewrite"] = select(
                integration_cleanup_items.c.domain_key
            ).where(
                integration_cleanup_items.c.batch_id == operation["batch_id"],
                integration_cleanup_items.c.irreversible_prewrite_at.is_not(None),
                integration_cleanup_items.c.state.in_(("pending", "retryable")),
            )
        blockers = []
        for kind, statement in statements.items():
            value = (await conn.execute(statement.limit(1))).scalar_one_or_none()
            if value is not None:
                # A pushed repair is deliberately not collapsed into an
                # anonymous generic write.  It is frozen external evidence,
                # and the public recovery refusal must tell the operator that
                # it needs exact lineage acceptance rather than a retry that
                # could create a competing writer.
                blockers.append(
                    f"resolution:{value}"
                    if kind == "resolution"
                    else kind
                )
        return sorted(blockers)
