"""Independent, restartable cleanup for terminal root integration trains."""

from __future__ import annotations

import hashlib
import inspect
import time
import uuid
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict
from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.database.tables import (
    integration_batch_members,
    integration_batches,
    integration_candidate_publications,
    integration_cleanup_items,
    integration_repair_operations,
    integration_repair_stages,
    integration_root_intent_members,
    task_delivery_receipts,
    workspaces,
)
from src.git.github_app import GitHubRepositoryBinding
from src.git.manager import GitError


class CleanupMaterializationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    outcome: Literal["materialized", "already_materialized", "stale", "invariant_error"]
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
        app_client_factory: Any | None = None,
        forge_provider: Any | None = None,
        clock=time.time,
    ) -> None:
        self.db = db
        self.data_dir = Path(data_dir)
        self.git = git_manager
        self.app_client_factory = app_client_factory
        self.forge_provider = forge_provider
        self.clock = clock

    async def advance(
        self, batch_id: str, *, now: float | None = None, limit: int = 100
    ) -> list[CleanupExecutionResult]:
        observed_at = self.clock() if now is None else now
        async with self.db._engine.connect() as conn:
            rows = (
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
            ).mappings().all()
        return [
            await self.execute(
                row["batch_id"], row["kind"], row["identity"], now=observed_at
            )
            for row in rows
        ]

    async def handle_item(self, row: dict[str, Any], now: float) -> CleanupExecutionResult:
        return await self.execute(row["batch_id"], row["kind"], row["identity"], now=now)

    async def execute(
        self, batch_id: str, kind: str, identity: str, *, now: float | None = None
    ) -> CleanupExecutionResult:
        observed_at = self.clock() if now is None else now
        nonce = uuid.uuid4().hex
        async with self.db.immediate() as conn:
            row = (
                await conn.execute(
                    select(integration_cleanup_items)
                    .where(
                        integration_cleanup_items.c.batch_id == batch_id,
                        integration_cleanup_items.c.kind == kind,
                        integration_cleanup_items.c.identity == identity,
                    )
                    .with_for_update()
                )
            ).mappings().one_or_none()
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
        provider = self.forge_provider or await self._app_client(binding)
        if provider is None:
            return "retryable", "cleanup forge provider is unavailable"
        current = await provider.exact_pull_request(
            number=int(row["target_pr_number"])
        )
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
                await provider.comment_pull_request(
                    number=int(row["target_pr_number"]),
                    marker=marker,
                    body=(
                        f"{marker}\nDelivered by integration batch `{row['batch_id']}` "
                        f"at `{row['expected_sha']}` via receipt `{row['receipt_id']}`."
                    ),
                )
        if current.get("state") != "closed":
            await provider.close_pull_request(
                number=int(row["target_pr_number"])
            )
        return "complete", None

    async def _app_client(self, binding: GitHubRepositoryBinding):
        if self.app_client_factory is None:
            return None
        client = self.app_client_factory(binding)
        if inspect.isawaitable(client):
            client = await client
        if client is None or client.repository != binding:
            return None
        return client

    async def _cleanup_remote_ref(self, row, repository, binding):
        short = self._short_head(row["target_ref"])
        if short == repository.default_branch:
            return "conflict", "default branch cleanup is forbidden"
        app = await self._app_client(binding)
        if app is None or self.git is None:
            return "retryable", "authenticated cleanup transport is unavailable"
        current = await app.exact_head_ref(short)
        if current is None:
            return "complete", None
        if current != row["expected_sha"]:
            return "conflict", "remote ref moved after delivery"
        try:
            await self.git.adelete_ref_with_app_auth(
                str(self.retained_store(row["repository_id"])),
                repository=binding,
                token=await app.installation_token(),
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
        if short == repository.default_branch:
            return "conflict", "default branch cleanup is forbidden"
        if self.git is None:
            return "retryable", "local cleanup transport is unavailable"
        store = str(self.retained_store(row["repository_id"]))
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
                await conn.execute(
                    select(workspaces).where(workspaces.c.id == row["identity"])
                )
            ).mappings().one_or_none()
            retained = (
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
            ).mappings().one_or_none()
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
            or getattr(workspace["source_type"], "value", workspace["source_type"])
            != "worktree"
            or retained is None
            or retained["operation_state"] != "completed"
            or retained["stage_state"] != "passed"
            or not isinstance(handoff, dict)
            or handoff.get("workspace_id") != row["identity"]
            or handoff.get("operation_id", retained["operation_id"])
            != retained["operation_id"]
            or handoff.get("head_sha") != row["expected_sha"]
            or base_path is None
        ):
            return "conflict", "retained worktree ownership changed"
        current = await self.git.arev_parse(row["workspace_path"], "HEAD")
        if current != row["expected_sha"]:
            return "conflict", "retained worktree head changed"
        base = await self.git.aworktree_base_path(row["workspace_path"])
        if base != base_path:
            return "conflict", "retained worktree is foreign"
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
                    await conn.execute(
                        select(integration_cleanup_items).where(
                            integration_cleanup_items.c.batch_id == row["batch_id"],
                            integration_cleanup_items.c.kind == row["kind"],
                            integration_cleanup_items.c.identity == row["identity"],
                        )
                    )
                ).mappings().one()
                return self._execution_result(
                    "already_complete" if current["state"] in {"complete", "conflict", "failed"}
                    else "wait",
                    current,
                )
            await self._project_aggregate_on(conn, row["batch_id"], now)
        finalized = dict(row) | values
        return self._execution_result(outcome, finalized)

    async def _project_aggregate_on(self, conn, batch_id, now):
        rows = (
            await conn.execute(
                select(integration_cleanup_items.c.state).where(
                    integration_cleanup_items.c.batch_id == batch_id
                )
            )
        ).scalars().all()
        if not rows or any(state in {"pending", "retryable"} for state in rows):
            return
        aggregate = "conflict" if any(state in {"conflict", "failed"} for state in rows) else "complete"
        await conn.execute(
            update(integration_batches)
            .where(
                integration_batches.c.id == batch_id,
                integration_batches.c.lifecycle == "promoted",
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
                await conn.execute(
                    select(integration_batches)
                    .where(integration_batches.c.id == batch_id)
                    .with_for_update()
                )
            ).mappings().one_or_none()
            publication = (
                await conn.execute(
                    select(integration_candidate_publications).where(
                        integration_candidate_publications.c.batch_id == batch_id,
                        integration_candidate_publications.c.revision
                        == (batch["current_revision"] if batch is not None else -1),
                    )
                )
            ).mappings().one_or_none()
            if (
                batch is None
                or batch["lifecycle"] != "promoted"
                or batch["final_main_sha"] is None
                or publication is None
                or publication["state"] != "pr_published"
            ):
                return CleanupMaterializationResult(
                    outcome="invariant_error", batch_id=batch_id
                )
            members = (
                await conn.execute(
                    select(integration_batch_members)
                    .where(integration_batch_members.c.batch_id == batch_id)
                    .order_by(integration_batch_members.c.ordinal)
                )
            ).mappings().all()
            reservations = (
                await conn.execute(
                    select(integration_root_intent_members)
                    .where(integration_root_intent_members.c.batch_id == batch_id)
                    .order_by(integration_root_intent_members.c.member_ordinal)
                )
            ).mappings().all()
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
                ).mappings().all()
            }
            if (
                not members
                or len(members) != len(reservations)
                or any(row["receipt_id"] not in receipts for row in reservations)
            ):
                return CleanupMaterializationResult(
                    outcome="invariant_error", batch_id=batch_id
                )
            items = self._items(batch, publication, members, reservations, receipts, observed_at)
            items.extend(await self._worktree_items(conn, batch, publication, observed_at))
            insert_fn = pg_insert if conn.dialect.name == "postgresql" else sqlite_insert
            for item in items:
                await conn.execute(
                    insert_fn(integration_cleanup_items)
                    .values(**item)
                    .on_conflict_do_nothing(
                        index_elements=["batch_id", "kind", "identity"]
                    )
                )
            persisted = (
                await conn.execute(
                    select(integration_cleanup_items).where(
                        integration_cleanup_items.c.batch_id == batch_id
                    )
                )
            ).mappings().all()
            expected = {
                (item["batch_id"], item["kind"], item["identity"]): item for item in items
            }
            if len(persisted) != len(expected) or any(
                not self._same_identity(dict(row), expected[(row["batch_id"], row["kind"], row["identity"])])
                for row in persisted
            ):
                return CleanupMaterializationResult(
                    outcome="invariant_error", batch_id=batch_id
                )
            return CleanupMaterializationResult(
                outcome="materialized",
                batch_id=batch_id,
                item_count=len(persisted),
            )

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
                number = self._pr_number(
                    member["pr_url"], publication["repository_full_name"]
                )
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
            await conn.execute(
                select(
                    workspaces.c.id.label("workspace_id"),
                    workspaces.c.workspace_path,
                    integration_repair_operations.c.id.label("operation_id"),
                    integration_repair_stages.c.retained_handoff,
                )
                .select_from(
                    integration_repair_operations
                    .join(
                        integration_repair_stages,
                        integration_repair_stages.c.operation_id
                        == integration_repair_operations.c.id,
                    )
                    .join(
                        workspaces,
                        workspaces.c.id == integration_repair_stages.c.retained_workspace_id,
                    )
                )
                .where(integration_repair_operations.c.batch_id == batch["id"])
            )
        ).mappings().all()
        common = {
            "batch_id": batch["id"], "project_id": batch["project_id"],
            "repository_id": batch["repository_id"],
            "repository_numeric_id": publication["repository_numeric_id"],
            "repository_full_name": publication["repository_full_name"],
            "revision": int(batch["current_revision"]), "state": "pending", "attempts": 0,
            "next_attempt_at": now, "created_at": now, "updated_at": now,
        }
        items = []
        for row in rows:
            handoff = row["retained_handoff"] or {}
            if (
                handoff.get("workspace_id") != row["workspace_id"]
                or handoff.get("operation_id", row["operation_id"])
                != row["operation_id"]
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
                }
            )
        return items

    @staticmethod
    def _pr_number(url: str, full_name: str) -> int:
        parsed = urlparse(url)
        prefix = f"/{full_name}/pull/"
        if parsed.scheme != "https" or parsed.netloc != "github.com" or not parsed.path.startswith(prefix):
            raise ValueError("cleanup PR identity does not match repository")
        suffix = parsed.path.removeprefix(prefix).strip("/")
        if not suffix.isdigit() or int(suffix) <= 0:
            raise ValueError("cleanup PR number is invalid")
        return int(suffix)

    @staticmethod
    def _same_identity(row: dict[str, Any], expected: dict[str, Any]) -> bool:
        mutable = {
            "state", "attempts", "next_attempt_at", "execution_nonce", "claim_expires_at",
            "last_error", "created_at", "updated_at", "terminal_at",
        }
        return all(row.get(key) == value for key, value in expected.items() if key not in mutable)

    def retained_store(self, repository_id: str) -> Path:
        digest = hashlib.sha256(repository_id.encode()).hexdigest()
        return self.data_dir / "integration-repositories" / f"{digest}.git"


__all__ = [
    "CleanupExecutionResult",
    "CleanupMaterializationResult",
    "IntegrationCleanupService",
]
