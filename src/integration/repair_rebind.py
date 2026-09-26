"""Operator proof and reservation for a superseded parent repair intent."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select, update

from src.database.tables import (
    integration_branch_owners,
    integration_promotion_intents,
    integration_repair_stages,
    tasks,
)
from src.git.manager import RemoteRefState, is_valid_git_oid
from src.integration.models import BranchKey, Fence
from src.integration.promotion import PromotionInvariantError, PromotionTargetMoved


class RepairRebind:
    def __init__(self, promotion):
        self.promotion = promotion
        self.db = promotion.db

    async def run(
        self, task_id: str, *, dry_run: bool, expected_head_sha: str | None = None
    ) -> dict:
        """Prove a live delegate's candidate and reserve it under the current intent.

        The operator supplies a task identity, never a fence or session identity.
        Both are taken from the attached owner and checked again under lock.
        The worker still performs the exact fenced push and close afterward.
        """
        async with self.db.immediate() as conn:
            delegate = (
                (await conn.execute(select(tasks).where(tasks.c.id == task_id).with_for_update()))
                .mappings()
                .one_or_none()
            )
            stage_rows = (
                (
                    await conn.execute(
                        select(integration_repair_stages)
                        .where(
                            integration_repair_stages.c.repair_task_id == task_id,
                            integration_repair_stages.c.writer_kind == "repair_delegate",
                            integration_repair_stages.c.state.in_(
                                ("active", "awaiting_completion")
                            ),
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .all()
            )
            if delegate is None or len(stage_rows) != 1:
                return {"outcome": "not_found", "task_id": task_id}
            stage = stage_rows[0]
            if (
                delegate["created_by_kind"] != "integration_repair"
                or delegate["created_by_id"] != stage["operation_id"]
            ):
                return {"outcome": "blocked", "reason": "task is not this stage's repair delegate"}
            named = (
                (
                    await conn.execute(
                        select(integration_promotion_intents)
                        .where(integration_promotion_intents.c.id == stage["trigger_id"])
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if named is None:
                return {"outcome": "blocked", "reason": "stage has no conflict intent"}
            intent = named
            if named["state"] == "superseded":
                intent = (
                    (
                        await conn.execute(
                            select(integration_promotion_intents)
                            .where(
                                integration_promotion_intents.c.id
                                == named["superseded_by_intent_id"]
                            )
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
            if intent is None or intent["state"] not in {"conflict", "resolution_reserved"}:
                return {"outcome": "blocked", "reason": "current conflict intent is unavailable"}
            if not intent["supersedes_intent_id"]:
                return {"outcome": "blocked", "reason": "repair intent was not superseded"}
            if intent["operation_key"] != stage["operation_id"]:
                return {"outcome": "blocked", "reason": "intent is outside the repair operation"}
            repository = await self.promotion._resolve_repository(intent["repository_id"])
            self.promotion._assert_resolution_repository(intent, repository)
            owner = (
                (
                    await conn.execute(
                        select(integration_branch_owners)
                        .where(
                            integration_branch_owners.c.repository_id == intent["repository_id"],
                            integration_branch_owners.c.ref == intent["target_branch"],
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if owner is None or not owner["session_id"]:
                return {"outcome": "blocked", "reason": "repair writer is not attached"}
            scope = await self.db.get_repair_filing_scope(
                task_id, session_id=owner["session_id"], conn=conn
            )
            if (
                scope is None
                or not scope["active"]
                or scope["operation_id"] != intent["operation_key"]
                or scope["target_kind"] != "parent"
                or scope["parent_task_id"] != intent["target_task_id"]
                or scope["project_id"] != intent["project_id"]
                or scope["repository_id"] != intent["repository_id"]
                or scope["trigger_id"] != named["id"]
                or not scope["instance_token"]
                or not self.promotion._repair_subject_matches_intent(scope, intent)
                or scope["deadline_at"] is None
                or self.promotion.clock() >= float(scope["deadline_at"])
            ):
                return {"outcome": "blocked", "reason": "repair writer authority is stale"}
            fence = Fence(
                target=BranchKey(
                    repository_id=intent["repository_id"], branch=intent["target_branch"]
                ),
                owner_id=task_id,
                token=scope["fence_token"],
            )
            async with self.promotion.ownership.mutation_exclusion_on(
                conn, fence, state="attached", expected_role="repair"
            ):
                workspace = Path(scope["workspace_path"])
                async with self.promotion.git.arepository_transaction(str(workspace)):
                    head_result = await self.promotion.git.arun_git_result(
                        ["rev-parse", "HEAD"],
                        cwd=str(workspace),
                        env={"LC_ALL": "C"},
                        lock_held=True,
                    )
                    head = head_result.stdout.strip().lower()
                    if head_result.returncode != 0 or not is_valid_git_oid(head):
                        raise PromotionInvariantError("repair workspace has no candidate commit")
                    if expected_head_sha is not None and head != expected_head_sha:
                        return {"outcome": "changed", "head_sha": head}
                    tree = await self.promotion._tree_oid(workspace, head)
                    commits = await self.promotion._resolution_commit_range(
                        workspace, intent["expected_target"], head
                    )
                    if not commits:
                        raise PromotionInvariantError("repair candidate has no commit range")
                    await self.promotion._assert_exact_resolution(
                        workspace,
                        {
                            **intent,
                            "resolution_head_sha": head,
                            "resolution_tree_sha": tree,
                            "resolution_commit_shas": commits,
                        },
                    )
                    remote = await self.promotion.git.als_remote_ref(
                        str(workspace), intent["target_branch"], remote=intent["origin_url"]
                    )
                    if (
                        remote.state is not RemoteRefState.PRESENT
                        or remote.oid != intent["expected_target"]
                    ):
                        raise PromotionTargetMoved("target is not the current conflict's old tip")
                result = {
                    "task_id": task_id,
                    "intent_id": intent["id"],
                    "head_sha": head,
                    "tree_sha": tree,
                    "repair_commit_shas": commits,
                    "fence_token": fence.token,
                    "session_id": scope["session_id"],
                }
                if dry_run:
                    return {"outcome": "would_rebind", **result}
                dossier = dict(stage["dossier"] or {})
                conflict = dict(dossier.get("current_conflict") or {})
                if stage["trigger_id"] != intent["id"] or conflict.get("intent_id") != intent["id"]:
                    conflict["intent_id"] = intent["id"]
                    dossier["current_conflict"] = conflict
                    dossier["trigger_id"] = intent["id"]
                    if named["id"] != intent["id"]:
                        dossier["superseded_conflict_intent_id"] = named["id"]
                    await conn.execute(
                        update(integration_repair_stages)
                        .where(
                            integration_repair_stages.c.operation_id == stage["operation_id"],
                            integration_repair_stages.c.ordinal == stage["ordinal"],
                            integration_repair_stages.c.trigger_id == named["id"],
                        )
                        .values(trigger_id=intent["id"], dossier=dossier)
                    )
                    if named["id"] != intent["id"]:
                        await conn.execute(
                            update(tasks)
                            .where(tasks.c.id == task_id)
                            .values(
                                description=(
                                    f"Current conflict intent: {intent['id']} "
                                    f"(supersedes {named['id']}).\n\n" + delegate["description"]
                                )
                            )
                        )
                reserved = await self.db.reserve_integration_conflict_resolution(
                    conn,
                    intent["id"],
                    {
                        "resolved_head_sha": head,
                        "resolved_tree_sha": tree,
                        "repair_commit_shas": commits,
                        "operation_id": scope["operation_id"],
                        "stage_ordinal": scope["stage"],
                        "repair_task_id": task_id,
                        "repair_session_id": scope["session_id"],
                        "repair_session_instance_token": scope["instance_token"],
                        "repair_workspace_id": scope["workspace_id"],
                        "fence_owner_id": task_id,
                        "fence_token": fence.token,
                    },
                )
                return {
                    "outcome": "already_reserved"
                    if reserved.get("_resolution_replayed")
                    else "rebound",
                    **result,
                    "next_step": "repair session must push this reserved resolution with its current fence",
                }
