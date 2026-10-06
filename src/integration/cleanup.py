"""Independent, restartable cleanup for terminal root integration trains."""

from __future__ import annotations

import hashlib
import inspect
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Literal
from dataclasses import dataclass
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict
from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.integration.engine import root_engine_guard

from src.database.tables import (
    archived_tasks,
    integration_batch_members,
    integration_batches,
    integration_branch_owners,
    integration_candidate_publications,
    integration_candidate_ref_mutations,
    integration_cleanup_items,
    integration_promotion_intents,
    integration_repair_operations,
    integration_repair_stages,
    integration_root_intent_members,
    task_delivery_receipts,
    tasks,
    workspaces,
)
from src.git.github_contracts import GitHubRepositoryBinding
from src.git.manager import GitError
from src.integration.delivery_branches import branch_of, deletable, repository_protected_branches


logger = logging.getLogger(__name__)


class CleanupMaterializationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    outcome: Literal["materialized", "already_materialized", "stale", "conflict", "invariant_error"]
    batch_id: str
    item_count: int = 0


class CleanupExecutionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    outcome: Literal[
        "complete", "already_complete", "retryable", "conflict", "failed", "wait", "stale"
    ]
    batch_id: str
    kind: str
    identity: str
    attempts: int


class IntegrationCleanupService:
    """Materialize immutable cleanup work; later calls execute one claimed item."""

    def __init__(
        self,
        db: Any,
        *,
        data_dir: str | Path,
        git_manager: Any | None = None,
        github_client_factory: Any | None = None,
        forge_provider: Any | None = None,
        clock=time.time,
    ) -> None:
        self.db = db
        self.data_dir = Path(data_dir)
        self.git = git_manager
        self.github_client_factory = github_client_factory
        self.forge_provider = forge_provider
        self.clock = clock

    async def advance(
        self, batch_id: str, *, now: float | None = None, limit: int = 100
    ) -> list[CleanupExecutionResult]:
        observed_at = self.clock() if now is None else now
        async with self.db._engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        select(integration_cleanup_items)
                        .where(
                            integration_cleanup_items.c.batch_id == batch_id,
                            integration_cleanup_items.c.state.in_(("pending", "retryable")),
                            integration_cleanup_items.c.next_attempt_at <= observed_at,
                            or_(
                                integration_cleanup_items.c.execution_nonce.is_(None),
                                integration_cleanup_items.c.claim_expires_at <= observed_at,
                            ),
                        )
                        .order_by(
                            integration_cleanup_items.c.next_attempt_at,
                            integration_cleanup_items.c.domain_key,
                        )
                        .limit(limit)
                    )
                )
                .mappings()
                .all()
            )
        results = [
            await self.execute(row["batch_id"], row["kind"], row["identity"], now=observed_at)
            for row in rows
        ]
        await self.reconcile_aggregate(batch_id, observed_at)
        return results

    async def handle_item(self, row: dict[str, Any], now: float) -> CleanupExecutionResult:
        return await self.execute(row["batch_id"], row["kind"], row["identity"], now=now)

    @root_engine_guard(
        "batch", outcome="wait", result_model=CleanupExecutionResult, aborted_pr_cleanup=True,
    )
    async def execute(
        self, batch_id: str, kind: str, identity: str, *, now: float | None = None
    ) -> CleanupExecutionResult:
        observed_at = self.clock() if now is None else now
        nonce = uuid.uuid4().hex
        async with self.db.immediate() as conn:
            row = (
                (
                    await conn.execute(
                        select(integration_cleanup_items)
                        .where(
                            integration_cleanup_items.c.batch_id == batch_id,
                            integration_cleanup_items.c.kind == kind,
                            integration_cleanup_items.c.identity == identity,
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return CleanupExecutionResult(
                    outcome="stale", batch_id=batch_id, kind=kind, identity=identity, attempts=0
                )
            if row["state"] in {"complete", "conflict", "failed"}:
                return self._execution_result("already_complete", row)
            if float(row["next_attempt_at"]) > observed_at:
                return self._execution_result("wait", row)
            if row["execution_nonce"] is not None and float(row["claim_expires_at"]) > observed_at:
                return self._execution_result("wait", row)
            claimed = await conn.execute(
                update(integration_cleanup_items)
                .where(
                    integration_cleanup_items.c.batch_id == batch_id,
                    integration_cleanup_items.c.kind == kind,
                    integration_cleanup_items.c.identity == identity,
                    integration_cleanup_items.c.state.in_(("pending", "retryable")),
                    integration_cleanup_items.c.attempts == row["attempts"],
                )
                .values(
                    attempts=int(row["attempts"]) + 1,
                    execution_nonce=nonce,
                    claim_expires_at=observed_at + 300.0,
                    updated_at=observed_at,
                )
            )
            if claimed.rowcount != 1:
                return self._execution_result("wait", row)
            claimed_row = dict(row)
            claimed_row.update(
                attempts=int(row["attempts"]) + 1,
                execution_nonce=nonce,
                claim_expires_at=observed_at + 300.0,
                updated_at=observed_at,
            )
        try:
            outcome, error = await self._perform(claimed_row)
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            import asyncio

            if isinstance(exc, asyncio.CancelledError):
                raise
            outcome, error = "retryable", str(exc) or type(exc).__name__
        return await self._finalize(claimed_row, nonce, observed_at, outcome, error)

    async def _perform(self, row: dict[str, Any]) -> tuple[str, str | None]:
        repository = await self.db.get_repo(row["repository_id"])
        if repository is None:
            return "failed", "cleanup repository is unavailable"
        binding = GitHubRepositoryBinding(
            repository_id=int(row["repository_numeric_id"]),
            full_name=row["repository_full_name"],
        )
        kind = row["kind"]
        if kind in {"source_pr", "audit_pr"}:
            return await self._cleanup_pr(row, binding)
        if kind == "remote_ref":
            return await self._cleanup_remote_ref(row, repository, binding)
        if kind == "local_ref":
            return await self._cleanup_local_ref(row, repository)
        if kind == "worktree":
            return await self._cleanup_worktree(row)
        return "failed", "unknown cleanup kind"

    async def _cleanup_pr(
        self, row: dict[str, Any], binding: GitHubRepositoryBinding
    ) -> tuple[str, str | None]:
        provider = self.forge_provider or await self._github_client(binding)
        if provider is None:
            return "retryable", "cleanup forge provider is unavailable"
        current = await provider.exact_pull_request(number=int(row["target_pr_number"]))
        if current is None:
            return "complete", None
        if (
            current.get("repository_numeric_id") != int(row["repository_numeric_id"])
            or current.get("repository_full_name") != row["repository_full_name"]
            or current.get("head_sha") != row["expected_sha"]
        ):
            return "conflict", "pull request repository or delivered head changed"
        if row["kind"] == "source_pr":
            marker = f"<!-- aq-delivery:{row['receipt_id']}:{row['expected_sha']} -->"
            if not await provider.has_comment_marker(
                number=int(row["target_pr_number"]), marker=marker
            ):
                summary = await self._repair_commit_summary(row["batch_id"])
                reservation = await self._mark_irreversible_prewrite(row)
                if reservation != "owner":
                    return "retryable", "pull request comment publication is unresolved"
                await provider.comment_pull_request(
                    number=int(row["target_pr_number"]),
                    marker=marker,
                    body=(
                        f"{marker}\nDelivered by integration batch `{row['batch_id']}` "
                        f"at `{row['expected_sha']}` via receipt `{row['receipt_id']}`."
                        + summary
                    ),
                )
        else:
            batch = await self.db.get_integration_batch(row["batch_id"])
            if batch is not None and batch["lifecycle"] == "aborted":
                marker = f"<!-- aq-aborted-batch:{row['batch_id']}:{row['expected_sha']} -->"
                if not await provider.has_comment_marker(
                    number=int(row["target_pr_number"]), marker=marker
                ):
                    if await self._mark_irreversible_prewrite(row) != "owner":
                        return "retryable", "abort comment publication is unresolved"
                    await provider.comment_pull_request(
                        number=int(row["target_pr_number"]), marker=marker,
                        body=(f"{marker}\nClosing integration batch `{row['batch_id']}`: "
                              f"outcome **aborted**. {batch['human_abort_reason'] or ''}"),
                    )
        # A comment can race a new push. Re-read before closing the bound head.
        current = await provider.exact_pull_request(number=int(row["target_pr_number"]))
        if current is None:
            return "complete", None
        if (
            current.get("repository_numeric_id") != int(row["repository_numeric_id"])
            or current.get("repository_full_name") != row["repository_full_name"]
            or current.get("head_sha") != row["expected_sha"]
        ):
            return "conflict", "pull request repository or head changed before closure"
        if current.get("state") != "closed":
            await provider.close_pull_request(number=int(row["target_pr_number"]))
        return "complete", None

    async def _repair_commit_summary(self, batch_id: str) -> str:
        async with self.db._engine.connect() as conn:
            dossiers = (
                await conn.execute(
                    select(integration_repair_stages.c.dossier)
                    .select_from(
                        integration_repair_operations.join(
                            integration_repair_stages,
                            integration_repair_stages.c.operation_id
                            == integration_repair_operations.c.id,
                        )
                    )
                    .where(integration_repair_operations.c.batch_id == batch_id)
                    .order_by(integration_repair_stages.c.ordinal)
                )
            ).scalars().all()
        commits = list(dict.fromkeys(
            sha for dossier in dossiers for sha in (dossier or {}).get("repair_commits", [])
        ))
        if not commits:
            return "\n\nNo integration repair commits were recorded."
        return "\n\nIntegration repair commits included in the promoted batch:\n" + "\n".join(
            f"- `{sha}`" for sha in commits
        )

    async def _mark_irreversible_prewrite(self, row: dict[str, Any]) -> str:
        """Freeze one claim before an ambiguous external write; marked writes never transfer."""
        if row.get("irreversible_prewrite_at") is not None:
            return "reconcile"
        now = float(row["updated_at"])
        async with self.db.immediate() as conn:
            current = (
                (
                    await conn.execute(
                        select(integration_cleanup_items)
                        .where(
                            integration_cleanup_items.c.batch_id == row["batch_id"],
                            integration_cleanup_items.c.kind == row["kind"],
                            integration_cleanup_items.c.identity == row["identity"],
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if current is None or current["state"] not in {"pending", "retryable"}:
                return "stale"
            if current["irreversible_prewrite_at"] is not None:
                return "reconcile"
            if (
                current["execution_nonce"] != row["execution_nonce"]
                or current["claim_expires_at"] is None
                or float(current["claim_expires_at"]) <= now
            ):
                return "stale"
            marked = await conn.execute(
                update(integration_cleanup_items)
                .where(
                    integration_cleanup_items.c.batch_id == row["batch_id"],
                    integration_cleanup_items.c.kind == row["kind"],
                    integration_cleanup_items.c.identity == row["identity"],
                    integration_cleanup_items.c.execution_nonce == row["execution_nonce"],
                    integration_cleanup_items.c.irreversible_prewrite_at.is_(None),
                )
                .values(
                    irreversible_nonce=row["execution_nonce"],
                    irreversible_prewrite_at=now,
                )
            )
            if marked.rowcount != 1:
                return "stale"
        row["irreversible_nonce"] = row["execution_nonce"]
        row["irreversible_prewrite_at"] = now
        return "owner"

    async def _github_client(self, binding: GitHubRepositoryBinding):
        if self.github_client_factory is None:
            return None
        client = self.github_client_factory(binding)
        if inspect.isawaitable(client):
            client = await client
        if client is None or client.repository != binding:
            return None
        return client

    async def _cleanup_remote_ref(self, row, repository, binding):
        short = self._short_head(row["target_ref"])
        async with self.db._engine.connect() as conn:
            protected = await repository_protected_branches(
                conn, row["repository_id"], default_branch=repository.default_branch,
            )
        if short in protected:
            return "conflict", "protected branch cleanup is forbidden"
        if row["member_ordinal"] is not None:
            async with self.db._engine.connect() as conn:
                owner = (
                    (
                        await conn.execute(
                            select(integration_branch_owners).where(
                                integration_branch_owners.c.repository_id == row["repository_id"],
                                integration_branch_owners.c.ref == row["target_ref"],
                                integration_branch_owners.c.handoff_state != "released",
                            )
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
            if owner is not None:
                return "conflict", "source ref has an active branch owner"
        app = await self._github_client(binding)
        if app is None or self.git is None:
            return "retryable", "authenticated cleanup transport is unavailable"
        current = await app.exact_head_ref(short)
        if current is None:
            return "complete", None
        if current != row["expected_sha"]:
            return "conflict", "remote ref moved after delivery"
        try:
            await self.git.adelete_repository_ref(
                str(self.retained_store(row["repository_id"])),
                repository=binding,
                branch=short,
                expected_old_oid=row["expected_sha"],
            )
        except GitError:
            observed = await app.exact_head_ref(short)
            if observed is None:
                return "complete", None
            if observed != row["expected_sha"]:
                return "conflict", "remote ref moved during cleanup"
            raise
        return "complete", None

    async def _cleanup_local_ref(self, row, repository):
        short = self._short_head(row["target_ref"])
        async with self.db._engine.connect() as conn:
            protected = await repository_protected_branches(
                conn, row["repository_id"], default_branch=repository.default_branch,
            )
        if short in protected:
            return "conflict", "protected branch cleanup is forbidden"
        if self.git is None:
            return "retryable", "local cleanup transport is unavailable"
        store = str(self.retained_store(row["repository_id"]))
        async with self.db._engine.connect() as conn:
            intent = (
                (
                    await conn.execute(
                        select(integration_promotion_intents).where(
                            integration_promotion_intents.c.intent_kind == "root",
                            integration_promotion_intents.c.root_batch_id == row["batch_id"],
                            integration_promotion_intents.c.root_candidate_revision
                            == row["revision"],
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            mutation = (
                (
                    await conn.execute(
                        select(integration_candidate_ref_mutations).where(
                            integration_candidate_ref_mutations.c.batch_id == row["batch_id"],
                            integration_candidate_ref_mutations.c.revision == row["revision"],
                            integration_candidate_ref_mutations.c.purpose == "root_main",
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            owner = (
                (
                    await conn.execute(
                        select(integration_branch_owners).where(
                            integration_branch_owners.c.repository_id == row["repository_id"],
                            integration_branch_owners.c.ref == row["target_ref"],
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
        if (
            intent is None
            or mutation is None
            or intent["state"] != "committed"
            or mutation["state"] != "applied"
            or intent["branch_fence_owner_id"] != mutation["branch_owner_id"]
            or int(intent["branch_fence_token"]) != int(mutation["branch_fence_token"])
            or mutation["branch_owner_role"] != "collector"
        ):
            return "conflict", "recorded integration branch ownership is incomplete"
        if owner is not None and (
            owner["owner_id"] != intent["branch_fence_owner_id"]
            or int(owner["fence_token"]) != int(intent["branch_fence_token"])
            or owner["owner_role"] != "collector"
        ):
            return "conflict", "local ref has a foreign branch owner"
        occupied = await self.git.aworktree_list(store)
        if any(entry.get("branch") == short for entry in occupied):
            return "conflict", "local ref is checked out in a worktree"
        current = await self.git.arev_parse(store, row["target_ref"])
        if current is None:
            return "complete", None
        if current != row["expected_sha"]:
            return "conflict", "local ref moved after delivery"
        await self.git.adelete_local_ref_exact(
            store, ref=row["target_ref"], expected_old_oid=row["expected_sha"]
        )
        return "complete", None

    async def _cleanup_worktree(self, row):
        if self.git is None:
            return "retryable", "worktree cleanup transport is unavailable"
        async with self.db._engine.connect() as conn:
            workspace = (
                (await conn.execute(select(workspaces).where(workspaces.c.id == row["identity"])))
                .mappings()
                .one_or_none()
            )
            retained = (
                (
                    await conn.execute(
                        select(
                            integration_repair_operations.c.id.label("operation_id"),
                            integration_repair_operations.c.batch_id,
                            integration_repair_operations.c.state.label("operation_state"),
                            integration_repair_stages.c.state.label("stage_state"),
                            integration_repair_stages.c.retained_handoff,
                        )
                        .select_from(
                            integration_repair_operations.join(
                                integration_repair_stages,
                                integration_repair_stages.c.operation_id
                                == integration_repair_operations.c.id,
                            )
                        )
                        .where(
                            integration_repair_operations.c.batch_id == row["batch_id"],
                            integration_repair_stages.c.retained_workspace_id == row["identity"],
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            base_path = None
            if workspace is not None:
                base_path = (
                    await conn.execute(
                        select(workspaces.c.workspace_path).where(
                            workspaces.c.id == workspace["base_workspace_id"],
                            workspaces.c.project_id == row["project_id"],
                        )
                    )
                ).scalar_one_or_none()
        handoff = retained["retained_handoff"] if retained is not None else None
        if (
            workspace is None
            or workspace["project_id"] != row["project_id"]
            or workspace["workspace_path"] != row["workspace_path"]
            or workspace["base_workspace_id"] is None
            or getattr(workspace["source_type"], "value", workspace["source_type"]) != "worktree"
            or retained is None
            or retained["operation_state"] != "completed"
            or retained["stage_state"] not in {"passed", "failed", "expired"}
            or not isinstance(handoff, dict)
            or handoff.get("workspace_id") != row["identity"]
            or handoff.get("operation_id", retained["operation_id"]) != retained["operation_id"]
            or handoff.get("head_sha") != row["expected_sha"]
            or base_path is None
        ):
            return "conflict", "retained worktree ownership changed"
        current = await self.git.arev_parse(row["workspace_path"], "HEAD")
        if current is None:
            if row.get("irreversible_prewrite_at") is not None:
                retained_path = Path(row["workspace_path"])
                if os.path.lexists(retained_path):
                    return "retryable", "retained worktree HEAD is temporarily unreadable"
                target_identity = retained_path.resolve(strict=False)
                registrations = await self.git.aworktree_list(base_path)
                if any(
                    entry.get("path") is not None
                    and Path(entry["path"]).resolve(strict=False) == target_identity
                    for entry in registrations
                ):
                    return "retryable", "retained worktree remains registered in its base"
                return "complete", None
            return "conflict", "retained worktree disappeared without removal evidence"
        if current != row["expected_sha"]:
            return "conflict", "retained worktree head changed"
        base = await self.git.aworktree_base_path(row["workspace_path"])
        if base != base_path:
            return "conflict", "retained worktree is foreign"
        reservation = await self._mark_irreversible_prewrite(row)
        if reservation != "owner":
            return "retryable", "worktree removal is unresolved"
        await self.git.aremove_worktree_exact(base, row["workspace_path"])
        return "complete", None

    async def _finalize(self, row, nonce, now, outcome, error):
        policy = await self._cleanup_policy(row["batch_id"])
        attempts = int(row["attempts"])
        terminal = outcome in {"complete", "conflict"}
        if outcome == "retryable" and attempts >= policy["max_attempts"]:
            outcome, error, terminal = "failed", error, True
        values: dict[str, Any] = {
            "state": outcome,
            "execution_nonce": None,
            "claim_expires_at": None,
            "last_error": error,
            "updated_at": now,
            "terminal_at": now if terminal or outcome == "failed" else None,
        }
        if outcome == "retryable":
            delay = min(
                policy["retry_base_seconds"] * (2 ** max(0, attempts - 1)),
                policy["retry_max_seconds"],
            )
            values["next_attempt_at"] = now + delay
        async with self.db.immediate() as conn:
            batch = (
                await conn.execute(
                    select(integration_batches.c.id)
                    .where(integration_batches.c.id == row["batch_id"])
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if batch is None:
                return self._execution_result("stale", row)
            result = await conn.execute(
                update(integration_cleanup_items)
                .where(
                    integration_cleanup_items.c.batch_id == row["batch_id"],
                    integration_cleanup_items.c.kind == row["kind"],
                    integration_cleanup_items.c.identity == row["identity"],
                    integration_cleanup_items.c.execution_nonce == nonce,
                    integration_cleanup_items.c.state.in_(("pending", "retryable")),
                )
                .values(**values)
            )
            if result.rowcount != 1:
                current = (
                    (
                        await conn.execute(
                            select(integration_cleanup_items).where(
                                integration_cleanup_items.c.batch_id == row["batch_id"],
                                integration_cleanup_items.c.kind == row["kind"],
                                integration_cleanup_items.c.identity == row["identity"],
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                return self._execution_result(
                    "already_complete"
                    if current["state"] in {"complete", "conflict", "failed"}
                    else "wait",
                    current,
                )
            await self._project_aggregate_on(conn, row["batch_id"], now)
        finalized = dict(row) | values
        return self._execution_result(outcome, finalized)

    async def reconcile_aggregate(self, batch_id, now):
        """Reconcile cleanup completion and its detached collector reservation."""
        async with self.db.immediate() as conn:
            batch = (
                await conn.execute(
                    select(integration_batches.c.id)
                    .where(integration_batches.c.id == batch_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if batch is not None:
                await self._project_aggregate_on(conn, batch_id, now)

    async def _project_aggregate_on(self, conn, batch_id, now):
        rows = (
            (
                await conn.execute(
                    select(integration_cleanup_items.c.state).where(
                        integration_cleanup_items.c.batch_id == batch_id
                    )
                )
            )
            .scalars()
            .all()
        )
        if not rows or any(state in {"pending", "retryable"} for state in rows):
            return
        aggregate = (
            "conflict" if any(state in {"conflict", "failed"} for state in rows) else "complete"
        )
        if aggregate == "complete":
            # Cleanup consumed the collector's exact fence. Retain that fence
            # until every item has finished, then release only its detached
            # reservation; never release a successor or an attached writer.
            intent = (await conn.execute(
                select(integration_promotion_intents, integration_batches.c.integration_branch)
                .join(integration_batches,
                      integration_batches.c.id == integration_promotion_intents.c.root_batch_id)
                .where(
                    integration_batches.c.id == batch_id,
                    integration_batches.c.lifecycle == "promoted",
                    integration_promotion_intents.c.intent_kind == "root",
                    integration_promotion_intents.c.root_candidate_revision
                    == integration_batches.c.current_revision,
                    integration_promotion_intents.c.state == "committed",
                    integration_promotion_intents.c.branch_fence_owner_id.is_not(None),
                )
            )).mappings().all()
            for delivered in intent:
                await conn.execute(
                    update(integration_branch_owners).where(
                        integration_branch_owners.c.repository_id == delivered["repository_id"],
                        integration_branch_owners.c.ref == delivered["integration_branch"],
                        integration_branch_owners.c.owner_id == delivered["branch_fence_owner_id"],
                        integration_branch_owners.c.fence_token == delivered["branch_fence_token"],
                        integration_branch_owners.c.owner_role == "collector",
                        integration_branch_owners.c.handoff_state == "reserved",
                        integration_branch_owners.c.session_id.is_(None),
                        integration_branch_owners.c.workspace_id.is_(None),
                    ).values(handoff_state="released", updated_at=now)
                )
        await conn.execute(
            update(integration_batches)
            .where(
                integration_batches.c.id == batch_id,
                integration_batches.c.lifecycle.in_(("promoted", "aborted")),
                integration_batches.c.cleanup_state == "pending",
            )
            .values(cleanup_state=aggregate, updated_at=now)
        )

    async def _cleanup_policy(self, batch_id):
        async with self.db._engine.connect() as conn:
            snapshot = (
                await conn.execute(
                    select(integration_batches.c.policy_snapshot).where(
                        integration_batches.c.id == batch_id
                    )
                )
            ).scalar_one()
        cleanup = snapshot.get("cleanup", {})
        return {
            "max_attempts": int(cleanup.get("max_attempts", 5)),
            "retry_base_seconds": float(cleanup.get("retry_base_seconds", 30.0)),
            "retry_max_seconds": float(cleanup.get("retry_max_seconds", 3600.0)),
            "successful_source_refs": cleanup.get("successful_source_refs", "delete"),
            "failed_work_retention_seconds": int(
                cleanup.get("failed_work_retention_seconds", 604800)
            ),
        }

    @staticmethod
    def _short_head(ref: str) -> str:
        prefix = "refs/heads/"
        if not isinstance(ref, str) or not ref.startswith(prefix):
            raise ValueError("cleanup ref must be a complete head ref")
        return ref.removeprefix(prefix)

    @staticmethod
    def _execution_result(outcome: str, row: Any) -> CleanupExecutionResult:
        return CleanupExecutionResult(
            outcome=outcome,
            batch_id=row["batch_id"],
            kind=row["kind"],
            identity=row["identity"],
            attempts=int(row["attempts"]),
        )

    @root_engine_guard(
        "batch", outcome="stale", result_model=CleanupMaterializationResult, aborted_pr_cleanup=True,
    )
    async def materialize(self, batch_id: str, *, now: float | None = None):
        observed_at = self.clock() if now is None else now
        async with self.db._engine.connect() as conn:
            project_id = (
                await conn.execute(
                    select(integration_batches.c.project_id).where(
                        integration_batches.c.id == batch_id
                    )
                )
            ).scalar_one_or_none()
        if project_id is None:
            return CleanupMaterializationResult(outcome="stale", batch_id=batch_id)
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, str(project_id))
            batch = (
                (
                    await conn.execute(
                        select(integration_batches)
                        .where(integration_batches.c.id == batch_id)
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if batch is not None and batch["lifecycle"] == "aborted":
                return await self.materialize_aborted_on(conn, batch, observed_at)
            publication = (
                (
                    await conn.execute(
                        select(integration_candidate_publications).where(
                            integration_candidate_publications.c.batch_id == batch_id,
                            integration_candidate_publications.c.revision
                            == (batch["current_revision"] if batch is not None else -1),
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if (
                batch is None
                or batch["lifecycle"] != "promoted"
                or batch["final_main_sha"] is None
                or publication is None
                or publication["state"] != "pr_published"
            ):
                return CleanupMaterializationResult(outcome="invariant_error", batch_id=batch_id)
            members = (
                (
                    await conn.execute(
                        select(integration_batch_members)
                        .where(integration_batch_members.c.batch_id == batch_id)
                        .order_by(integration_batch_members.c.ordinal)
                    )
                )
                .mappings()
                .all()
            )
            # A batch repaired after a root intent was reserved keeps that
            # superseded intent's member rows beside the committed one's.
            # Only the current revision's reservations describe what landed,
            # exactly as only its receipts do below.
            reservations = (
                (
                    await conn.execute(
                        select(integration_root_intent_members)
                        .where(
                            integration_root_intent_members.c.batch_id == batch_id,
                            integration_root_intent_members.c.candidate_revision
                            == batch["current_revision"],
                        )
                        .order_by(integration_root_intent_members.c.member_ordinal)
                    )
                )
                .mappings()
                .all()
            )
            receipts = {
                row["id"]: dict(row)
                for row in (
                    await conn.execute(
                        select(task_delivery_receipts).where(
                            task_delivery_receipts.c.batch_id == batch_id,
                            task_delivery_receipts.c.candidate_revision
                            == batch["current_revision"],
                        )
                    )
                )
                .mappings()
                .all()
            }
            if (
                not members
                or len(members) != len(reservations)
                or any(row["receipt_id"] not in receipts for row in reservations)
            ):
                return CleanupMaterializationResult(outcome="invariant_error", batch_id=batch_id)
            if any(
                row["source_ref"] is None or row["source_ref_retention"] is None for row in members
            ):
                await conn.execute(
                    update(integration_batches)
                    .where(
                        integration_batches.c.id == batch_id,
                        integration_batches.c.cleanup_state == "pending",
                    )
                    .values(cleanup_state="conflict", updated_at=observed_at)
                )
                return CleanupMaterializationResult(outcome="conflict", batch_id=batch_id)
            items = self._items(batch, publication, members, reservations, receipts, observed_at)
            items.extend(await self._descendant_ref_items(
                conn, batch, publication, members, observed_at,
                existing_refs={item["target_ref"] for item in items if item.get("target_ref")},
            ))
            items.extend(await self._worktree_items(conn, batch, publication, observed_at))
            insert_fn = pg_insert
            for item in items:
                await conn.execute(
                    insert_fn(integration_cleanup_items)
                    .values(**item)
                    .on_conflict_do_nothing(index_elements=["batch_id", "kind", "identity"])
                )
            persisted = (
                (
                    await conn.execute(
                        select(integration_cleanup_items).where(
                            integration_cleanup_items.c.batch_id == batch_id
                        )
                    )
                )
                .mappings()
                .all()
            )
            expected = {(item["batch_id"], item["kind"], item["identity"]): item for item in items}
            if len(persisted) != len(expected) or any(
                not self._same_identity(
                    dict(row), expected[(row["batch_id"], row["kind"], row["identity"])]
                )
                for row in persisted
            ):
                return CleanupMaterializationResult(outcome="invariant_error", batch_id=batch_id)
            return CleanupMaterializationResult(
                outcome="materialized",
                batch_id=batch_id,
                item_count=len(persisted),
            )

    @classmethod
    async def materialize_aborted_on(cls, conn, batch, now):
        """Queue audit PR retirement atomically with abort; retain all source work."""
        publications = (await conn.execute(
            select(integration_candidate_publications).where(
                integration_candidate_publications.c.batch_id == batch["id"],
                integration_candidate_publications.c.state == "pr_published",
            ).order_by(integration_candidate_publications.c.revision.desc())
        )).mappings().all()
        seen = set()
        for publication in publications:
            identity = f"{publication['repository_numeric_id']}#{publication['pr_number']}"
            if identity in seen:
                continue
            seen.add(identity)
            number = cls._pr_number(publication["pr_url"], publication["repository_full_name"])
            if number != publication["pr_number"] or publication["repository_id"] != batch[
                "repository_id"
            ]:
                raise ValueError("aborted batch PR identity is inconsistent")
            await conn.execute(pg_insert(integration_cleanup_items).values(
                batch_id=batch["id"], project_id=batch["project_id"],
                repository_id=publication["repository_id"],
                repository_numeric_id=publication["repository_numeric_id"],
                repository_full_name=publication["repository_full_name"],
                revision=publication["revision"], kind="audit_pr", identity=identity,
                domain_key=f"cleanup:{batch['id']}:audit_pr:{identity}",
                target_pr_number=number, target_pr_url=publication["pr_url"],
                expected_sha=publication["head_sha"], state="pending", attempts=0,
                next_attempt_at=now, created_at=now, updated_at=now,
            ).on_conflict_do_nothing(index_elements=["batch_id", "kind", "identity"]))
        if not seen:
            await conn.execute(update(integration_batches).where(
                integration_batches.c.id == batch["id"],
                integration_batches.c.lifecycle == "aborted",
                integration_batches.c.cleanup_state == "pending",
            ).values(cleanup_state="complete", updated_at=now))
        return CleanupMaterializationResult(
            outcome="materialized", batch_id=batch["id"], item_count=len(seen),
        )

    async def reconcile_aborted(self, now: float) -> None:
        """Backfill audit PR cleanup after a restart or an older daemon's abort."""
        async with self.db._engine.connect() as conn:
            batch_ids = list((await conn.execute(select(integration_batches.c.id).where(
                integration_batches.c.lifecycle == "aborted",
                integration_batches.c.cleanup_state == "pending",
                ~select(integration_cleanup_items.c.domain_key).where(
                    integration_cleanup_items.c.batch_id == integration_batches.c.id,
                ).exists(),
            ).order_by(integration_batches.c.id).limit(100))).scalars())
        for batch_id in batch_ids:
            try:
                result = await self.materialize(batch_id, now=now)
                if result.outcome not in {"materialized", "already_materialized"}:
                    logger.warning("Aborted batch PR cleanup %s: %s", batch_id, result.outcome)
            except Exception:
                logger.warning("Could not queue aborted batch PR cleanup %s", batch_id,
                               exc_info=True)

    async def _descendant_ref_items(
        self, conn, batch, publication, members, now, *, existing_refs: set[str]
    ) -> list[dict[str, Any]]:
        """Only delete descendant refs with a delivered, exact source head."""
        parent_ordinals = {member["task_id"]: int(member["ordinal"]) for member in members}
        delete_ordinals = {
            int(member["ordinal"])
            for member in members
            if member["source_ref_retention"] == "delete"
        }
        descendants: dict[str, tuple[dict[str, Any], int]] = {}
        frontier = set(parent_ordinals)
        while frontier:
            found: dict[str, dict[str, Any]] = {}
            for table in (tasks, archived_tasks):
                rows = (
                    await conn.execute(
                        select(table.c.id, table.c.parent_task_id, table.c.branch_name,
                               table.c.status)
                        .where(table.c.parent_task_id.in_(frontier))
                    )
                ).mappings().all()
                for row in rows:
                    found.setdefault(row["id"], dict(row))
            next_frontier: set[str] = set()
            for task_id, row in found.items():
                if task_id in parent_ordinals:
                    continue
                ordinal = parent_ordinals[row["parent_task_id"]]
                parent_ordinals[task_id] = ordinal
                descendants[task_id] = row, ordinal
                next_frontier.add(task_id)
            frontier = next_frontier
        if not descendants:
            return []
        promoted_at = (
            await conn.execute(
                select(integration_promotion_intents.c.committed_at).where(
                    integration_promotion_intents.c.root_batch_id == batch["id"],
                    integration_promotion_intents.c.state == "committed",
                )
            )
        ).scalar_one_or_none()
        if promoted_at is None:
            return []
        receipt_rows = (
            await conn.execute(
                select(task_delivery_receipts.c.source_task_id,
                       task_delivery_receipts.c.target_task_id,
                       task_delivery_receipts.c.reviewed_head_sha)
                .where(
                    task_delivery_receipts.c.source_task_id.in_(descendants),
                    task_delivery_receipts.c.repository_id == batch["repository_id"],
                    task_delivery_receipts.c.disposition.in_(("code", "noop")),
                    task_delivery_receipts.c.created_at <= promoted_at,
                )
                .order_by(task_delivery_receipts.c.created_at.desc(),
                          task_delivery_receipts.c.id.desc())
            )
        ).mappings().all()
        heads = {}
        for receipt in receipt_rows:
            row, _ = descendants[receipt["source_task_id"]]
            if receipt["target_task_id"] == row["parent_task_id"]:
                heads.setdefault(receipt["source_task_id"], receipt["reviewed_head_sha"])
        common = {
            "batch_id": batch["id"],
            "project_id": batch["project_id"],
            "repository_id": batch["repository_id"],
            "repository_numeric_id": publication["repository_numeric_id"],
            "repository_full_name": publication["repository_full_name"],
            "revision": int(batch["current_revision"]),
            "state": "pending",
            "attempts": 0,
            "next_attempt_at": now,
            "created_at": now,
            "updated_at": now,
        }
        items = []
        repo = await self.db.get_repo(batch["repository_id"])
        protected = await repository_protected_branches(
            conn, batch["repository_id"], default_branch=repo.default_branch,
        )
        for task_id, (row, ordinal) in sorted(descendants.items()):
            branch = branch_of(row["branch_name"])
            head = heads.get(task_id)
            if (
                ordinal not in delete_ordinals
                or row["status"] != "COMPLETED"
                or not branch
                or not deletable(branch, repo.default_branch, protected=protected)
                or not head
            ):
                continue
            ref = f"refs/heads/{branch}"
            if ref in existing_refs:
                continue
            existing_refs.add(ref)
            items.append(common | {
                "kind": "remote_ref",
                "identity": ref,
                "domain_key": f"cleanup:{batch['id']}:remote_ref:{ref}",
                "member_ordinal": ordinal,
                "target_ref": ref,
                "expected_sha": head,
            })
        return items

    def _items(self, batch, publication, members, reservations, receipts, now):
        common = {
            "batch_id": batch["id"],
            "project_id": batch["project_id"],
            "repository_id": batch["repository_id"],
            "repository_numeric_id": publication["repository_numeric_id"],
            "repository_full_name": publication["repository_full_name"],
            "revision": int(batch["current_revision"]),
            "state": "pending",
            "attempts": 0,
            "next_attempt_at": now,
            "created_at": now,
            "updated_at": now,
        }
        items = []
        for member, reservation in zip(members, reservations, strict=True):
            if member["pr_url"]:
                number = self._pr_number(member["pr_url"], publication["repository_full_name"])
                identity = f"{publication['repository_numeric_id']}#{number}"
                items.append(
                    common
                    | {
                        "kind": "source_pr",
                        "identity": identity,
                        "domain_key": f"cleanup:{batch['id']}:source_pr:{identity}",
                        "member_ordinal": int(member["ordinal"]),
                        "receipt_id": reservation["receipt_id"],
                        "target_pr_number": number,
                        "target_pr_url": member["pr_url"],
                        "expected_sha": member["reviewed_head_sha"],
                    }
                )
            if member["source_ref_retention"] == "delete":
                source_ref = member["source_ref"]
                items.append(
                    common
                    | {
                        "kind": "remote_ref",
                        "identity": source_ref,
                        "domain_key": f"cleanup:{batch['id']}:remote_ref:{source_ref}",
                        "member_ordinal": int(member["ordinal"]),
                        "target_ref": source_ref,
                        "expected_sha": member["reviewed_head_sha"],
                    }
                )
        audit_identity = f"{publication['repository_numeric_id']}#{publication['pr_number']}"
        items.append(
            common
            | {
                "kind": "audit_pr",
                "identity": audit_identity,
                "domain_key": f"cleanup:{batch['id']}:audit_pr:{audit_identity}",
                "target_pr_number": publication["pr_number"],
                "target_pr_url": publication["pr_url"],
                "expected_sha": publication["head_sha"],
            }
        )
        for kind in ("remote_ref", "local_ref"):
            identity = batch["integration_branch"]
            items.append(
                common
                | {
                    "kind": kind,
                    "identity": identity,
                    "domain_key": f"cleanup:{batch['id']}:{kind}:{identity}",
                    "target_ref": identity,
                    "expected_sha": publication["head_sha"],
                }
            )
        return items

    async def _worktree_items(self, conn, batch, publication, now):
        rows = (
            (
                await conn.execute(
                    select(
                        workspaces.c.id.label("workspace_id"),
                        workspaces.c.workspace_path,
                        integration_repair_operations.c.id.label("operation_id"),
                        integration_repair_stages.c.state.label("stage_state"),
                        integration_repair_stages.c.completed_at,
                        integration_repair_stages.c.retained_handoff,
                    )
                    .select_from(
                        integration_repair_operations.join(
                            integration_repair_stages,
                            integration_repair_stages.c.operation_id
                            == integration_repair_operations.c.id,
                        ).join(
                            workspaces,
                            workspaces.c.id == integration_repair_stages.c.retained_workspace_id,
                        )
                    )
                    .where(integration_repair_operations.c.batch_id == batch["id"])
                )
            )
            .mappings()
            .all()
        )
        common = {
            "batch_id": batch["id"],
            "project_id": batch["project_id"],
            "repository_id": batch["repository_id"],
            "repository_numeric_id": publication["repository_numeric_id"],
            "repository_full_name": publication["repository_full_name"],
            "revision": int(batch["current_revision"]),
            "state": "pending",
            "attempts": 0,
            "next_attempt_at": now,
            "created_at": now,
            "updated_at": now,
        }
        items = []
        cleanup = (batch["policy_snapshot"] or {}).get("cleanup", {})
        failed_retention = int(cleanup.get("failed_work_retention_seconds", 604800))
        for row in rows:
            handoff = row["retained_handoff"] or {}
            if (
                handoff.get("workspace_id") != row["workspace_id"]
                or handoff.get("operation_id", row["operation_id"]) != row["operation_id"]
                or handoff.get("head_sha") is None
            ):
                raise ValueError("retained worktree provenance is incomplete")
            items.append(
                common
                | {
                    "kind": "worktree",
                    "identity": row["workspace_id"],
                    "domain_key": f"cleanup:{batch['id']}:worktree:{row['workspace_id']}",
                    "workspace_path": row["workspace_path"],
                    "expected_sha": handoff["head_sha"],
                    "next_attempt_at": (
                        max(now, float(row["completed_at"]) + failed_retention)
                        if row["stage_state"] in {"failed", "expired"}
                        and row["completed_at"] is not None
                        else now
                    ),
                }
            )
        return items

    @staticmethod
    def _pr_number(url: str, full_name: str) -> int:
        parsed = urlparse(url)
        prefix = f"/{full_name}/pull/"
        if (
            parsed.scheme != "https"
            or parsed.netloc != "github.com"
            or not parsed.path.startswith(prefix)
        ):
            raise ValueError("cleanup PR identity does not match repository")
        suffix = parsed.path.removeprefix(prefix).strip("/")
        if not suffix.isdigit() or int(suffix) <= 0:
            raise ValueError("cleanup PR number is invalid")
        return int(suffix)

    @staticmethod
    def _same_identity(row: dict[str, Any], expected: dict[str, Any]) -> bool:
        mutable = {
            "state",
            "attempts",
            "next_attempt_at",
            "execution_nonce",
            "claim_expires_at",
            "last_error",
            "created_at",
            "updated_at",
            "terminal_at",
        }
        return all(row.get(key) == value for key, value in expected.items() if key not in mutable)

    def retained_store(self, repository_id: str) -> Path:
        digest = hashlib.sha256(repository_id.encode()).hexdigest()
        return self.data_dir / "integration-repositories" / f"{digest}.git"


__all__ = [
    "CleanupExecutionResult",
    "CleanupMaterializationResult",
    "IntegrationCleanupService",
    "SubjectCleanup",
    "SubjectCleanupItem",
]


@dataclass(frozen=True)
class SubjectCleanupItem:
    """Retention facts supplied by a published subject's existing inventory.

    The inventory keeps existing branch-discard backups and preserved-repair
    retention. Failed work is retained until its explicit deadline; it is never
    deleted merely because a retry counter was exhausted.
    """

    kind: Literal["remote_ref", "local_ref", "pull_request"]
    identity: str
    expected_sha: str
    successful_source: bool = False
    failed_at: float | None = None
    retain_until: float | None = None
    irreversible: bool = False

    def __post_init__(self):
        if self.kind not in {"remote_ref", "local_ref", "pull_request"} or not self.identity:
            raise ValueError("cleanup requires an exact ref or PR identity")
        if len(self.expected_sha) != 40 or any(
            c not in "0123456789abcdef" for c in self.expected_sha
        ):
            raise ValueError("cleanup requires a full expected SHA")
        if self.kind != "pull_request" and not self.identity.startswith("refs/"):
            raise ValueError("cleanup requires a fully qualified ref")


class SubjectCleanup:
    """Primitive 20 over shared Git/authority ports, beside legacy cleanup.

    ``inventory`` returns current cleanup identities after proving publication;
    it omits already-closed PRs on replay. ``held`` rechecks live reference holds.
    ``close_pr`` must bind repository, PR identity and expected head, refusing a
    moved PR. Each delete uses an expected-old lease and authenticated read-back.
    Cleanup failures retry opportunistically without a deletion journal.
    """

    def __init__(self, gitops, *, inventory, held, close_pr=None, clock=time.time):
        self.gitops = gitops
        self.inventory, self.held, self.close_pr = inventory, held, close_pr
        self.clock = clock

    def bind(self, ports):
        from src.integration.subjects import Primitive

        ports.bind(Primitive.CLEANUP, self)

    async def __call__(self, subject, args):
        from dataclasses import asdict

        from src.integration.development import DevelopmentBusy
        from src.integration.ownership import BranchOwnershipError
        from src.integration.subjects import PrimitiveOutcome, SubjectPhase

        p = args.primitive
        if subject.phase not in {SubjectPhase.PUBLISHED, SubjectPhase.CLEANING}:
            return PrimitiveOutcome.unknown(p, "cleanup_requires_published_subject")
        pending, retained, deleted, retention_deadlines = [], [], [], []
        try:
            repo = await self.gitops._repository(subject)
            async with self.gitops.db._engine.connect() as conn:
                protected = await repository_protected_branches(
                    conn, subject.repository_id, default_branch=repo.default_branch,
                )
            async with self.gitops.exclusion(repo.repository_id, subject):
                await self.gitops.authority(subject)
                items = await self.inventory(subject)
                for item in items:
                    if item.irreversible:
                        return PrimitiveOutcome(primitive=p, outcome="irreversible_marker",
                                                detail={"item": asdict(item)})
                for item in items:
                    deadline = max(item.retain_until or 0, (
                        item.failed_at + args.retain_failed_seconds
                        if item.failed_at is not None else 0
                    ))
                    if (item.identity == subject.target_ref
                            or branch_of(item.identity) in protected
                            or item.successful_source and not args.delete_successful_sources
                            or self.clock() < deadline):
                        retained.append(item.identity)
                        if self.clock() < deadline:
                            retention_deadlines.append(deadline)
                        continue
                    if await self.held(subject, item):
                        pending.append({"item": item.identity, "reason": "live_reference"})
                        continue
                    # Derive completed deletion from the current external ref.
                    if item.kind == "remote_ref":
                        absent = await self.gitops.remote(repo, item.identity) is None
                    elif item.kind == "local_ref":
                        exists = await self.gitops.git.aref_exists(str(repo.store), item.identity)
                        if exists is None:
                            pending.append({"item": item.identity, "reason": "local_ref_unknown"})
                            continue
                        absent = not exists
                    else:
                        absent = False
                    if absent:
                        deleted.append(item.identity)
                        continue
                    await self.gitops.authority(subject)
                    if item.kind != "pull_request" and not await self.gitops.authority.unowned(
                        repo.repository_id, item.identity
                    ):
                        pending.append({"item": item.identity, "reason": "writer_owned"})
                        continue
                    # Recheck holds immediately before bounded expected-old deletion.
                    await self.gitops.authority(subject)
                    if await self.held(subject, item):
                        pending.append({"item": item.identity, "reason": "live_reference"})
                        continue
                    try:
                        async with self.gitops.authority.mutation(
                            subject,
                            unowned_ref=item.identity if item.kind != "pull_request" else None,
                        ) as deadline:
                            await self._apply(repo, item, deadline)
                    except Exception as exc:
                        pending.append({"item": item.identity, "reason": str(exc),
                                        "attempts": 1})
                        continue
                    deleted.append(item.identity)
            return PrimitiveOutcome(
                primitive=p,
                outcome="pending" if pending or retention_deadlines else "clean",
                detail={
                    "pending": pending, "retained": retained, "deleted": deleted,
                    "retention_due_at": min(retention_deadlines) if retention_deadlines else None,
                },
            )
        except (GitError, BranchOwnershipError, DevelopmentBusy, ValueError) as exc:
            return PrimitiveOutcome.unknown(p, str(exc))

    async def _apply(self, repo, item, deadline):
        from src.integration.gitops import branch
        from src.integration.ownership import StaleFence

        if item.kind == "remote_ref":
            actual = await self.gitops.remote(repo, item.identity)
            if actual is not None:
                if actual != item.expected_sha:
                    raise StaleFence("cleanup ref moved")
                try:
                    await self.gitops.git.adelete_repository_ref(
                        str(repo.store), repository=repo.binding,
                        branch=branch(item.identity), expected_old_oid=item.expected_sha,
                        authority_deadline=deadline,
                    )
                except Exception:
                    if await self.gitops.remote(repo, item.identity) is not None:
                        raise
                if await self.gitops.remote(repo, item.identity) is not None:
                    raise StaleFence("cleanup delete is unconfirmed")
        elif item.kind == "local_ref":
            exists = await self.gitops.git.aref_exists(str(repo.store), item.identity)
            if exists is None:
                raise StaleFence("cleanup local ref unknown")
            if exists:
                actual = await self.gitops.git.arev_parse(str(repo.store), item.identity)
                if actual != item.expected_sha:
                    raise StaleFence("cleanup local ref moved")
                if item.identity.startswith("refs/heads/"):
                    await self.gitops.git.adelete_local_ref_exact(
                        str(repo.store), ref=item.identity, expected_old_oid=item.expected_sha,
                    )
                else:
                    # Construction pins live under refs/aq, whereas the legacy
                    # local-deletion helper accepts heads only. Keep Git's exact
                    # old-object comparison for these retained-store refs too.
                    await self.gitops.run(repo, "check-ref-format", item.identity)
                    await self.gitops.run(
                        repo, "update-ref", "-d", item.identity, item.expected_sha
                    )
        elif self.close_pr is None or not await self.close_pr(repo, item):
            raise StaleFence("PR close is unavailable or unconfirmed")
