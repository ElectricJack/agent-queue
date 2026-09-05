"""Durable, fenced ownership of repository-qualified integration branches."""

from __future__ import annotations

import inspect
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

from src.database.tables import integration_branch_owners, sessions, workspaces
from src.integration.models import BranchKey, Fence


class BranchOwnershipError(RuntimeError):
    """Base class for ownership failures with a deterministic caller outcome."""


class BranchBusy(BranchOwnershipError):
    """A branch still has a writer, or its handoff cannot be proven safe."""


class StaleFence(BranchOwnershipError):
    """The caller's ownership fence was superseded."""


HandoffConfirmation = Callable[[dict[str, Any]], Awaitable[bool] | bool]


class BranchOwnership:
    """Serialize writers using a monotonic database fence.

    ``reserved`` means a domain owner has the branch but has not attached a
    session/workspace yet.  ``attached`` is a live writer.  A transfer first
    persists ``handoff_pending``; only a fresh external confirmation that the
    old session stopped *and* its checkout was released/detached may allocate
    the next token.  ``released`` is durable proof from a previous handoff
    and can be claimed without treating expiry as liveness evidence.
    """

    def __init__(self, db, *, confirm_handoff: HandoffConfirmation | None = None) -> None:
        self._db = db
        self._confirm_handoff = confirm_handoff

    async def get_owner(self, target: BranchKey) -> dict[str, Any] | None:
        """Return the current durable owner snapshot for command validation."""
        async with self._db.immediate() as conn:
            return await self._locked_row(conn, target)

    async def acquire(
        self, target: BranchKey, owner_id: str, role: str, *, conn=None
    ) -> Fence:
        """Reserve an unowned branch; expiry never overrides an attached owner."""
        self._validate_identity(target, owner_id, role)
        if conn is not None:
            return await self._acquire_on(conn, target, owner_id, role)
        async with self._db.immediate() as owned_conn:
            return await self._acquire_on(owned_conn, target, owner_id, role)

    async def _acquire_on(self, conn, target: BranchKey, owner_id: str, role: str) -> Fence:
        row = await self._locked_row(conn, target)
        if row is None:
            token = 1
            try:
                # PostgreSQL cannot lock a row that does not exist.  Keep
                # the unique insert inside a savepoint so a concurrent
                # first acquirer names the existing owner as busy instead
                # of aborting this transaction with a raw IntegrityError.
                async with conn.begin_nested():
                    await conn.execute(
                        insert(integration_branch_owners).values(
                            id=str(uuid.uuid4()),
                            repository_id=target.repository_id,
                            ref=target.branch,
                            owner_id=owner_id,
                            owner_role=role,
                            fence_token=token,
                            handoff_state="reserved",
                            created_at=time.time(),
                            updated_at=time.time(),
                        )
                    )
            except IntegrityError:
                row = await self._locked_row(conn, target)
                if row is None:
                    raise BranchBusy("branch acquisition raced another owner") from None
            else:
                return Fence(target=target, owner_id=owner_id, token=token)

        if row is None:  # pragma: no cover - narrowed by the insert race above
            raise BranchBusy("branch acquisition could not resolve its owner")

        if row["handoff_state"] == "released":
            return await self._claim_released(conn, row, target, owner_id, role)
        if row["owner_id"] == owner_id and row["owner_role"] == role:
            if row["handoff_state"] == "handoff_pending":
                raise BranchBusy("branch handoff is awaiting termination evidence")
            return self._fence(row)
        raise BranchBusy("branch has an active or unresolved owner")

    async def transfer(self, fence: Fence, next_owner_id: str, next_role: str) -> Fence:
        """Transfer only after the old writer's server-side handoff is proven."""
        self._validate_identity(fence.target, next_owner_id, next_role)
        async with self._db.immediate() as conn:
            row = await self._locked_row(conn, fence.target)
            self._require_current(row, fence)
            state = row["handoff_state"]
            needs_confirmation = state in {"attached", "handoff_pending"}
            if needs_confirmation and (not row["session_id"] or not row["workspace_id"]):
                raise BranchBusy("attached owner lacks session/workspace handoff evidence")
            if state == "attached":
                await conn.execute(
                    update(integration_branch_owners)
                    .where(integration_branch_owners.c.id == row["id"])
                    .where(integration_branch_owners.c.fence_token == fence.token)
                    .values(handoff_state="handoff_pending", updated_at=time.time())
                )
            elif state not in {"reserved", "released", "handoff_pending"}:
                raise BranchBusy("branch ownership state is not transferable")

        if needs_confirmation:
            if self._confirm_handoff is None:
                raise BranchBusy("no server-side handoff confirmer is installed")
            confirmed = self._confirm_handoff(dict(row))
            if inspect.isawaitable(confirmed):
                confirmed = await confirmed
            if not confirmed:
                raise BranchBusy("previous writer has not confirmed stopped and detached")

        async with self._db.immediate() as conn:
            current = await self._locked_row(conn, fence.target)
            self._require_current(current, fence)
            confirmed_state = current["handoff_state"]
            released_by_callback = (
                needs_confirmation
                and confirmed_state == "released"
                and current.get("confirmed_workspace_id") == row.get("workspace_id")
            )
            required_state = "handoff_pending" if needs_confirmation else state
            if confirmed_state != required_state and not released_by_callback:
                raise BranchBusy("branch handoff changed while confirmation ran")
            return await self._claim_released(conn, current, fence.target, next_owner_id, next_role)

    async def assert_current(self, fence: Fence, *, expected_role: str | None = None) -> None:
        """Raise unless *fence* remains the current write authority."""
        async with self._db.immediate() as conn:
            row = await self._locked_row(conn, fence.target)
            self._require_current(row, fence)
            if expected_role is not None and row["owner_role"] != expected_role:
                raise BranchBusy(f"branch ownership role must be {expected_role}")
            if row["handoff_state"] not in {"reserved", "attached"}:
                raise BranchBusy("branch ownership is not write-authoritative")

    @asynccontextmanager
    async def mutation_exclusion(
        self,
        fence: Fence,
        *,
        state: str = "reserved",
        expected_role: str | None = None,
    ):
        """Hold the ownership row across one bounded external mutation.

        The row lock (and SQLite's immediate writer transaction) prevents a
        transfer from passing while workspace preparation or provider startup
        is mutating the branch.  The caller must not perform unrelated or
        unbounded work inside this context.
        """
        async with self._db.immediate() as conn:
            async with self.mutation_exclusion_on(
                conn, fence, state=state, expected_role=expected_role
            ):
                yield conn

    @asynccontextmanager
    async def mutation_exclusion_on(
        self,
        conn,
        fence: Fence,
        *,
        state: str = "reserved",
        expected_role: str | None = None,
    ):
        """Recheck and hold ownership inside a caller's canonically ordered transaction."""
        row = await self._locked_row(conn, fence.target)
        self._require_current(row, fence)
        if expected_role is not None and row["owner_role"] != expected_role:
            raise BranchBusy(f"branch ownership role must be {expected_role}")
        if row["handoff_state"] != state:
            raise BranchBusy(f"branch ownership must be {state} for this mutation")
        yield row

    async def attach(
        self,
        fence: Fence,
        session_id: str,
        workspace_id: str,
        *,
        agent_id: str | None = None,
        expected_role: str | None = None,
        conn=None,
    ) -> Fence:
        """CAS-bind a reserved task owner to its durable writer identity."""
        if not session_id or not workspace_id:
            raise ValueError("branch attachment requires session and workspace ids")
        if conn is not None:
            return await self._attach_on(
                conn,
                fence,
                session_id,
                workspace_id,
                agent_id=agent_id,
                expected_role=expected_role,
            )
        async with self._db.immediate() as owned_conn:
            return await self._attach_on(
                owned_conn,
                fence,
                session_id,
                workspace_id,
                agent_id=agent_id,
                expected_role=expected_role,
            )

    async def _attach_on(
        self,
        conn,
        fence: Fence,
        session_id: str,
        workspace_id: str,
        *,
        agent_id: str | None,
        expected_role: str | None,
    ) -> Fence:
        row = await self._locked_row(conn, fence.target)
        self._require_current(row, fence)
        if expected_role is not None and row["owner_role"] != expected_role:
            raise BranchBusy(f"branch ownership role must be {expected_role}")
        if row["handoff_state"] == "attached":
            if row.get("session_id") == session_id and row.get("workspace_id") == workspace_id:
                return fence
            raise BranchBusy("branch is already attached to another writer")
        if row["handoff_state"] != "reserved":
            raise BranchBusy("branch ownership is not reserved for attachment")
        workspace = (
            await conn.execute(
                select(workspaces)
                .where(workspaces.c.id == workspace_id)
                .with_for_update()
            )
        ).mappings().one_or_none()
        if (
            workspace is None
            or workspace["locked_by_task_id"] != fence.owner_id
            or (agent_id is not None and workspace["locked_by_agent_id"] != agent_id)
        ):
            raise BranchBusy("workspace is no longer locked by the branch owner")
        result = await conn.execute(
            update(integration_branch_owners)
            .where(integration_branch_owners.c.id == row["id"])
            .where(integration_branch_owners.c.fence_token == fence.token)
            .where(integration_branch_owners.c.handoff_state == "reserved")
            .where(integration_branch_owners.c.session_id.is_(None))
            .where(integration_branch_owners.c.workspace_id.is_(None))
            .values(
                handoff_state="attached",
                session_id=session_id,
                workspace_id=workspace_id,
                updated_at=time.time(),
            )
        )
        if result.rowcount != 1:
            raise StaleFence("branch attachment lost its compare-and-swap")
        return fence

    async def begin_startup_reconciliation(
        self,
        fence: Fence,
        session_id: str,
        workspace_id: str,
        *,
        now: float,
        min_age_seconds: float = 60.0,
    ) -> bool:
        """Fence a stale STARTING writer before reconciliation releases it.

        A scan racing provider startup waits on the same owner row.  Fresh
        startup is left alone; once stale, ``handoff_pending`` prevents the
        launcher from crossing its final attached-state check.
        """
        async with self._db.immediate() as conn:
            row = await self._locked_row(conn, fence.target)
            self._require_current(row, fence)
            if (
                row["handoff_state"] != "attached"
                or row.get("session_id") != session_id
                or row.get("workspace_id") != workspace_id
            ):
                return False
            session = (
                await conn.execute(
                    select(sessions.c.state, sessions.c.started_at)
                    .where(sessions.c.id == session_id)
                    .with_for_update()
                )
            ).one_or_none()
            if session is None or session.state != "starting":
                return False
            if now - float(session.started_at or now) < min_age_seconds:
                return False
            result = await conn.execute(
                update(integration_branch_owners)
                .where(integration_branch_owners.c.id == row["id"])
                .where(integration_branch_owners.c.fence_token == fence.token)
                .where(integration_branch_owners.c.handoff_state == "attached")
                .values(handoff_state="handoff_pending", updated_at=now)
            )
            return result.rowcount == 1

    async def _claim_released(self, conn, row: dict[str, Any], target, owner_id, role) -> Fence:
        token = int(row["fence_token"]) + 1
        result = await conn.execute(
            update(integration_branch_owners)
            .where(integration_branch_owners.c.id == row["id"])
            .where(integration_branch_owners.c.fence_token == row["fence_token"])
            .values(
                owner_id=owner_id,
                owner_role=role,
                fence_token=token,
                handoff_state="reserved",
                session_id=None,
                workspace_id=None,
                confirmed_workspace_id=(
                    row.get("confirmed_workspace_id") or row.get("workspace_id")
                ),
                expires_at=None,
                updated_at=time.time(),
            )
        )
        if result.rowcount != 1:
            raise StaleFence("branch owner changed while transferring")
        return Fence(target=target, owner_id=owner_id, token=token)

    async def _locked_row(self, conn, target: BranchKey) -> dict[str, Any] | None:
        result = await conn.execute(
            select(integration_branch_owners)
            .where(integration_branch_owners.c.repository_id == target.repository_id)
            .where(integration_branch_owners.c.ref == target.branch)
            .with_for_update()
        )
        row = result.mappings().one_or_none()
        return dict(row) if row is not None else None

    @staticmethod
    def _validate_identity(target: BranchKey, owner_id: str, role: str) -> None:
        if not target.repository_id or not target.branch:
            raise ValueError("branch ownership target must name repository and branch")
        if not owner_id or not role:
            raise ValueError("branch ownership requires non-empty owner and role")

    @staticmethod
    def _fence(row: dict[str, Any]) -> Fence:
        return Fence(
            target=BranchKey(repository_id=row["repository_id"], branch=row["ref"]),
            owner_id=row["owner_id"],
            token=int(row["fence_token"]),
        )

    @classmethod
    def _require_current(cls, row: dict[str, Any] | None, fence: Fence) -> None:
        if row is None:
            raise StaleFence("branch ownership record does not exist")
        if cls._fence(row) != fence:
            raise StaleFence("branch ownership fence is stale")


__all__ = ["BranchBusy", "BranchOwnership", "BranchOwnershipError", "StaleFence"]
