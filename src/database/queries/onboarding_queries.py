"""Durable idempotency and recovery state for project onboarding.

The onboarding saga makes filesystem and external-service changes that cannot
share a database transaction.  This small repository is therefore deliberately
strict: a request starts pending, its phase and owned-resource ledger can only
advance while pending, and ``finish`` is a one-way transition.  A retried
request can safely inspect the first record rather than start a second saga.
"""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import and_, delete, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.database.tables import project_onboarding_requests, projects, workspaces
from src.models import Project, Workspace

_PENDING = "pending"
_TERMINAL = ("succeeded", "failed")


class OnboardingQueryMixin:
    """Persistence methods used by ``ProjectOnboardingService``.

    Both adapters supply ``immediate()``.  Besides serialising SQLite writers,
    it makes the read-modify-write ledger append atomic without relying on a
    backend-specific JSON mutation function.
    """

    async def create_onboarding_request(
        self,
        request_id: str,
        input_fingerprint: str,
        *,
        phase: str = _PENDING,
        now: float | None = None,
    ) -> tuple[bool, str]:
        """Insert a pending request unless it already exists.

        Returns ``(already_exists, stored_fingerprint)``.  In particular, a
        caller must compare the returned fingerprint with its normalized input
        before resuming an idempotent replay.
        """
        now = time.time() if now is None else now
        values = {
            "request_id": request_id,
            "input_fingerprint": input_fingerprint,
            "status": _PENDING,
            "phase": phase,
            "created_resources": [],
            "created_at": now,
            "updated_at": now,
            "finished_at": None,
        }
        async with self.immediate() as conn:
            dialect = conn.dialect.name
            if dialect == "sqlite":
                statement = sqlite_insert(project_onboarding_requests).values(**values)
                result = await conn.execute(statement.on_conflict_do_nothing())
            elif dialect == "postgresql":
                statement = pg_insert(project_onboarding_requests).values(**values)
                result = await conn.execute(statement.on_conflict_do_nothing())
            else:  # pragma: no cover - adapters are SQLite/PostgreSQL today.
                result = await conn.execute(project_onboarding_requests.insert().values(**values))
            row = (
                await conn.execute(
                    select(project_onboarding_requests.c.input_fingerprint).where(
                        project_onboarding_requests.c.request_id == request_id
                    )
                )
            ).first()
            # A successful insert and the select above are one transaction; an
            # absent row would therefore indicate a broken backend contract.
            if row is None:  # pragma: no cover - defensive
                raise RuntimeError("onboarding request insert did not persist")
            return result.rowcount == 0, str(row.input_fingerprint)

    async def register_onboarded_project(self, project: Project, workspace: Workspace) -> None:
        """Atomically insert the project and its primary repository workspace."""
        now = time.time()
        async with self.immediate() as conn:
            await conn.execute(
                insert(projects).values(
                    id=project.id,
                    name=project.name,
                    credit_weight=project.credit_weight,
                    max_concurrent_agents=project.max_concurrent_agents,
                    status=project.status.value,
                    total_tokens_used=project.total_tokens_used,
                    budget_limit=project.budget_limit,
                    discord_channel_id=project.discord_channel_id,
                    repo_url=project.repo_url,
                    repo_default_branch=project.repo_default_branch,
                    default_profile_id=project.default_profile_id,
                    assignment_playbook_id=project.assignment_playbook_id,
                    integration_mode=project.integration_mode,
                    created_at=now,
                )
            )
            await conn.execute(
                insert(workspaces).values(
                    id=workspace.id,
                    project_id=workspace.project_id,
                    workspace_path=workspace.workspace_path,
                    source_type=workspace.source_type.value,
                    name=workspace.name,
                    kind_id=workspace.kind_id or "project-repo",
                    locked_by_agent_id=workspace.locked_by_agent_id,
                    locked_by_task_id=workspace.locked_by_task_id,
                    locked_at=workspace.locked_at,
                    lock_mode=(workspace.lock_mode.value if workspace.lock_mode else None),
                    enabled=workspace.enabled,
                    slot_index=workspace.slot_index,
                    base_workspace_id=workspace.base_workspace_id,
                    created_at=now,
                )
            )

    async def rollback_onboarded_project(self, project_id: str, workspace_id: str) -> None:
        """Remove exactly the two rows inserted by an onboarding request."""
        async with self.immediate() as conn:
            await conn.execute(
                delete(workspaces).where(
                    workspaces.c.id == workspace_id,
                    workspaces.c.project_id == project_id,
                )
            )
            await conn.execute(delete(projects).where(projects.c.id == project_id))

    async def get_onboarding_request(self, request_id: str) -> dict[str, Any] | None:
        """Return the stored request, including its safe JSON ledger/result."""
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(project_onboarding_requests).where(
                            project_onboarding_requests.c.request_id == request_id
                        )
                    )
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    async def update_onboarding_phase(
        self, request_id: str, phase: str, *, now: float | None = None
    ) -> bool:
        """Set the current phase while the saga is still pending."""
        now = time.time() if now is None else now
        async with self.immediate() as conn:
            result = await conn.execute(
                update(project_onboarding_requests)
                .where(
                    and_(
                        project_onboarding_requests.c.request_id == request_id,
                        project_onboarding_requests.c.status == _PENDING,
                    )
                )
                .values(phase=phase, updated_at=now)
            )
        return bool(result.rowcount)

    async def append_onboarding_resource(
        self, request_id: str, resource: dict[str, Any], *, now: float | None = None
    ) -> bool:
        """Atomically append one scrubbed resource record to a pending request."""
        now = time.time() if now is None else now
        async with self.immediate() as conn:
            row = (
                await conn.execute(
                    select(project_onboarding_requests.c.created_resources).where(
                        and_(
                            project_onboarding_requests.c.request_id == request_id,
                            project_onboarding_requests.c.status == _PENDING,
                        )
                    )
                )
            ).first()
            if row is None:
                return False
            resources = row.created_resources
            # Metadata defaults and query writes use a JSON list.  Retaining
            # defensively-normalized older data is safer than making recovery
            # fail because one historical row has malformed JSON shape.
            if not isinstance(resources, list):
                resources = []
            resources = [*resources, dict(resource)]
            result = await conn.execute(
                update(project_onboarding_requests)
                .where(
                    and_(
                        project_onboarding_requests.c.request_id == request_id,
                        project_onboarding_requests.c.status == _PENDING,
                    )
                )
                .values(created_resources=resources, updated_at=now)
            )
        return bool(result.rowcount)

    async def finish_onboarding_request(
        self,
        request_id: str,
        status: str,
        *,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> bool:
        """Finish a request once; terminal rows never change on replay."""
        if status not in _TERMINAL:
            raise ValueError(
                f"onboarding terminal status must be one of {_TERMINAL}, got {status!r}"
            )
        now = time.time() if now is None else now
        values: dict[str, Any] = {
            "status": status,
            "updated_at": now,
            "finished_at": now,
            "result": dict(result) if result is not None else None,
            "error": dict(error) if error is not None else None,
        }
        async with self.immediate() as conn:
            update_result = await conn.execute(
                update(project_onboarding_requests)
                .where(
                    and_(
                        project_onboarding_requests.c.request_id == request_id,
                        project_onboarding_requests.c.status == _PENDING,
                    )
                )
                .values(**values)
            )
        return bool(update_result.rowcount)

    async def purge_finished_onboarding_requests(self, cutoff: float, *, limit: int = 1000) -> int:
        """Delete a bounded batch of terminal records older than ``cutoff``.

        Pending rows and terminal rows missing a completion timestamp are kept:
        neither has a safe age horizon to use for collection.
        """
        async with self.immediate() as conn:
            doomed = (
                (
                    await conn.execute(
                        select(project_onboarding_requests.c.request_id)
                        .where(
                            project_onboarding_requests.c.status.in_(_TERMINAL),
                            project_onboarding_requests.c.finished_at.is_not(None),
                            project_onboarding_requests.c.finished_at < cutoff,
                        )
                        .order_by(project_onboarding_requests.c.finished_at)
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            if not doomed:
                return 0
            await conn.execute(
                delete(project_onboarding_requests).where(
                    project_onboarding_requests.c.request_id.in_(list(doomed))
                )
            )
        return len(doomed)

    # Concise aliases mirror the persistence operations in the onboarding
    # design while the longer names keep the shared database facade legible.
    async def update_phase(self, request_id: str, phase: str, *, now: float | None = None) -> bool:
        return await self.update_onboarding_phase(request_id, phase, now=now)

    async def append_resource(
        self, request_id: str, resource: dict[str, Any], *, now: float | None = None
    ) -> bool:
        return await self.append_onboarding_resource(request_id, resource, now=now)

    async def finish(
        self,
        request_id: str,
        status: str,
        *,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> bool:
        return await self.finish_onboarding_request(
            request_id, status, result=result, error=error, now=now
        )

    async def purge_finished_before(self, cutoff: float, *, limit: int = 1000) -> int:
        return await self.purge_finished_onboarding_requests(cutoff, limit=limit)
