"""Remove task branches an operator explicitly asked to discard.

Deleting a task in a hierarchy/train project may also remove the branch that
task put on the remote, but only on an explicit choice — see
``docs/superpowers/specs/2026-09-08-task-deletion-with-materialized-branches-design.md``.
``guard_integration_mutation`` records that choice by retiring the origin and
marking it ``discard_state='pending'``; this service is what actually deletes
the ref.

The work is deliberately *not* on the integration outbox.  That outbox
dispatches only to playbooks (``Orchestrator.accept_integration_event``), and
no shipped playbook consumes branch-level events — the sibling
``integration.branch_materialization_pending`` event has no consumer at all
(design §6).  A drain over the origin rows is durable in the same way and
actually runs.

Origin rows carry no foreign key to ``tasks`` and ``_delete_one`` leaves them
alone, so a retired origin outlives the task it describes and this service can
still find its work after the subtree is gone.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import or_, select, update

from src.database.tables import integration_branch_owners, task_branch_origins
from src.git.github_app import GitHubRepositoryBinding
from src.git.manager import GitError

logger = logging.getLogger(__name__)

#: A discard that keeps failing backs off from ~30s toward this ceiling.
RETRY_BASE_SECONDS = 30.0
RETRY_MAX_SECONDS = 3600.0
#: Transport trouble is retried this many times before the row is parked as
#: ``failed`` for an operator.  Conflicts never retry — they are a statement
#: about the remote, not about the transport.
MAX_ATTEMPTS = 8


class BranchDiscardResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    outcome: Literal["complete", "conflict", "failed", "retryable", "wait", "stale"]
    origin_id: str
    task_id: str
    branch: str
    attempts: int
    error: str | None = None


class BranchDiscardService:
    """Drain ``task_branch_origins`` rows marked for branch discard."""

    def __init__(
        self,
        db: Any,
        *,
        data_dir: str | Path,
        git_manager: Any | None = None,
        app_client_factory: Any | None = None,
        repository_binding_resolver: Any | None = None,
        clock=time.time,
    ) -> None:
        self.db = db
        self.data_dir = Path(data_dir)
        self.git = git_manager
        self.app_client_factory = app_client_factory
        self.repository_binding_resolver = repository_binding_resolver
        self.clock = clock

    # -- draining -------------------------------------------------------

    async def drain_due(self, *, now: float | None = None, limit: int = 20) -> list[
        BranchDiscardResult
    ]:
        """Advance every discard whose backoff has elapsed, oldest request first."""
        observed_at = self.clock() if now is None else now
        async with self.db._engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        select(task_branch_origins)
                        .where(
                            task_branch_origins.c.discard_state == "pending",
                            or_(
                                task_branch_origins.c.discard_next_attempt_at.is_(None),
                                task_branch_origins.c.discard_next_attempt_at <= observed_at,
                            ),
                        )
                        .order_by(
                            task_branch_origins.c.discard_requested_at,
                            task_branch_origins.c.id,
                        )
                        .limit(limit)
                    )
                )
                .mappings()
                .all()
            )
        results = []
        for row in rows:
            results.append(await self.advance(row["id"], now=observed_at))
        return results

    async def advance(self, origin_id: str, *, now: float | None = None) -> BranchDiscardResult:
        """Attempt one discard, claiming it by its attempt count."""
        observed_at = self.clock() if now is None else now
        async with self.db.immediate() as conn:
            row = (
                (
                    await conn.execute(
                        select(task_branch_origins)
                        .where(task_branch_origins.c.id == origin_id)
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return BranchDiscardResult(
                    outcome="stale", origin_id=origin_id, task_id="", branch="", attempts=0
                )
            if row["discard_state"] != "pending":
                return self._result("stale", row)
            if (
                row["discard_next_attempt_at"] is not None
                and float(row["discard_next_attempt_at"]) > observed_at
            ):
                return self._result("wait", row)
            # Claim by attempt count: two daemons draining the same row cannot
            # both reach the delete.
            claimed = await conn.execute(
                update(task_branch_origins)
                .where(
                    task_branch_origins.c.id == origin_id,
                    task_branch_origins.c.discard_state == "pending",
                    task_branch_origins.c.discard_attempts == row["discard_attempts"],
                )
                .values(
                    discard_attempts=int(row["discard_attempts"]) + 1,
                    discard_next_attempt_at=observed_at
                    + self._backoff(int(row["discard_attempts"]) + 1),
                )
            )
            if claimed.rowcount != 1:
                return self._result("wait", row)
            claimed_row = dict(row)
            claimed_row["discard_attempts"] = int(row["discard_attempts"]) + 1

        try:
            outcome, error = await self._perform(claimed_row)
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except Exception as exc:  # noqa: BLE001 - transport trouble is retryable by default
            outcome, error = "retryable", str(exc) or type(exc).__name__
        return await self._finalize(claimed_row, observed_at, outcome, error)

    # -- the actual removal ---------------------------------------------

    async def _perform(self, row: dict[str, Any]) -> tuple[str, str | None]:
        branch = self._branch(row)
        repository = await self.db.get_repo(row["repository_id"])
        if repository is None:
            return "failed", "discard repository is unavailable"
        if branch == repository.default_branch:
            # Unreachable through the guard (a task branch is always
            # ``aq/<task-id>``), and catastrophic if it ever became reachable.
            return "conflict", "default branch discard is forbidden"
        owner = await self._live_owner(row["repository_id"], branch)
        if owner is not None:
            # Something re-acquired the ref after the delete — a repair
            # delegate or a verifier.  Deleting it under a live writer would
            # lose work the operator never asked to discard.
            return "conflict", "branch has an active owner"
        binding = await self._binding(repository)
        if binding is None:
            return "retryable", "repository binding is unavailable"
        client = await self._app_client(binding)
        if client is None or self.git is None:
            return "retryable", "authenticated discard transport is unavailable"
        head = await client.exact_head_ref(branch)
        if head is None:
            return "complete", None
        try:
            await self.git.adelete_ref_with_app_auth(
                str(self.retained_store(row["repository_id"])),
                repository=binding,
                token=await client.installation_token(),
                branch=branch,
                expected_old_oid=head,
            )
        except GitError:
            observed = await client.exact_head_ref(branch)
            if observed is None:
                return "complete", None
            if observed != head:
                return "conflict", "branch moved during discard"
            raise
        return "complete", None

    async def _live_owner(self, repository_id: str, branch: str):
        async with self.db._engine.connect() as conn:
            return (
                (
                    await conn.execute(
                        select(integration_branch_owners.c.id).where(
                            integration_branch_owners.c.repository_id == repository_id,
                            integration_branch_owners.c.ref == branch,
                            integration_branch_owners.c.handoff_state != "released",
                        )
                    )
                )
                .mappings()
                .first()
            )

    async def _binding(self, repository) -> GitHubRepositoryBinding | None:
        if self.repository_binding_resolver is None:
            return None
        binding = self.repository_binding_resolver(repository)
        if inspect.isawaitable(binding):
            binding = await binding
        return binding

    async def _app_client(self, binding: GitHubRepositoryBinding):
        if self.app_client_factory is None:
            return None
        client = self.app_client_factory(binding)
        if inspect.isawaitable(client):
            client = await client
        if client is None or client.repository != binding:
            return None
        return client

    # -- bookkeeping ----------------------------------------------------

    async def _finalize(
        self, row: dict[str, Any], now: float, outcome: str, error: str | None
    ) -> BranchDiscardResult:
        attempts = int(row["discard_attempts"])
        if outcome == "retryable" and attempts >= MAX_ATTEMPTS:
            outcome, error = "failed", error or "discard exhausted its attempts"
        if outcome == "retryable":
            state, next_attempt = "pending", now + self._backoff(attempts)
        else:
            state, next_attempt = outcome, None
        async with self.db.immediate() as conn:
            await conn.execute(
                update(task_branch_origins)
                .where(
                    task_branch_origins.c.id == row["id"],
                    task_branch_origins.c.discard_attempts == attempts,
                )
                .values(
                    discard_state=state,
                    discard_next_attempt_at=next_attempt,
                    discard_last_error=error,
                )
            )
        if outcome in {"conflict", "failed"}:
            logger.warning(
                "Branch discard for %s (%s) ended %s: %s",
                self._branch(row), row["id"], outcome, error,
            )
        return self._result(outcome, row, error=error)

    @staticmethod
    def _backoff(attempts: int) -> float:
        return min(RETRY_BASE_SECONDS * (2 ** max(attempts - 1, 0)), RETRY_MAX_SECONDS)

    @staticmethod
    def _branch(row: dict[str, Any]) -> str:
        return f"aq/{row['task_id']}"

    def _result(
        self, outcome: str, row: dict[str, Any], *, error: str | None = None
    ) -> BranchDiscardResult:
        return BranchDiscardResult(
            outcome=outcome,
            origin_id=row["id"],
            task_id=row["task_id"],
            branch=self._branch(row),
            attempts=int(row["discard_attempts"]),
            error=error,
        )

    def retained_store(self, repository_id: str) -> Path:
        digest = hashlib.sha256(repository_id.encode()).hexdigest()
        return self.data_dir / "integration-repositories" / f"{digest}.git"


__all__ = ["BranchDiscardResult", "BranchDiscardService"]
