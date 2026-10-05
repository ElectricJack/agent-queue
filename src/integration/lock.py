"""Expiring per-ref authority for the git-first managed publisher.

Acquisition and publication share a PostgreSQL transaction advisory lock and
row lock. Git only compares OIDs; a credentialed external push is outside this
authority and is observed as remote movement. No project lease or workspace
handoff proves authority here. Legacy execution must remain on ownership.py
under shadow; invoking this port is the active protocol's admission boundary.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import time
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy import func, insert, select, update

from src.database.tables import integration_branch_owners as owners
from src.database.tables import sessions, tasks
from src.git.github_contracts import GitHubRepositoryBinding
from src.git.manager import GitError, GitManager, RemoteRefState
from src.integration.models import BranchKey, Fence
from src.integration.ownership import BranchBusy, StaleFence

DEFAULT_TTL_SECONDS = 480.0
CRITICAL_SECTION_SECONDS = 30.0


class RemoteMoved(GitError):
    """The captured base moved externally; rebuild against the observed ref."""

    def __init__(self, observed_oid: str | None):
        self.observed_oid = observed_oid
        super().__init__("remote ref moved; rebuild against its current tip")


def canonical_target(target: BranchKey) -> BranchKey:
    branch = target.branch.removeprefix("refs/heads/")
    if not target.repository_id or not branch or branch.startswith("refs/"):
        raise ValueError("ref lease requires a repository and a branch")
    return BranchKey(repository_id=target.repository_id, branch=branch)


def managed(row: dict | None) -> bool:
    """A retained fence, including a released lease, marks the active protocol."""
    return row is not None and row.get("fence") is not None


@dataclass(frozen=True)
class RefLease:
    target: BranchKey
    holder: str | None
    fence: int
    expires_at: float

    def grant(self) -> Fence:
        if self.holder is None:
            raise StaleFence("ref lease is released")
        return Fence(target=self.target, owner_id=self.holder, token=self.fence)


class BranchLock:
    """One finite lease per repository/ref; expiry is enough to reacquire it."""

    def __init__(self, db, *, clock: Callable[[], float] = time.time):
        self.db, self.clock = db, clock

    @staticmethod
    def _ttl(ttl_seconds: float) -> float:
        if isinstance(ttl_seconds, bool) or not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
            raise ValueError("ref lease TTL must be finite and positive")
        return ttl_seconds

    async def lock_on(self, conn, target: BranchKey) -> dict | None:
        """Also exclude first insert and aliases when the ref has no row yet."""
        target = canonical_target(target)
        identity = f"aq-ref-lease:{target.repository_id}:{target.branch}".encode()
        key = int.from_bytes(hashlib.sha256(identity).digest()[:8], "big", signed=True)
        await conn.execute(select(func.pg_advisory_xact_lock(key)))
        rows = (
            (
                await conn.execute(
                    select(owners)
                    .where(
                        owners.c.repository_id == target.repository_id,
                        owners.c.ref.in_((target.branch, f"refs/heads/{target.branch}")),
                    )
                    .order_by(owners.c.ref)
                    .with_for_update()
                )
            )
            .mappings()
            .all()
        )
        if len(rows) > 1:
            raise BranchBusy("multiple rows name the same ref")
        return dict(rows[0]) if rows else None

    @asynccontextmanager
    async def exclusion(self, fence: Fence, *, conn=None):
        """Hold current authority through bounded I/O, shared with reacquisition."""
        async with asyncio.timeout(CRITICAL_SECTION_SECONDS):
            if conn is not None:
                row = await self.lock_on(conn, fence.target)
                self.require_current(row, fence)
                yield row
            else:
                async with self.db.immediate() as owned:
                    row = await self.lock_on(owned, fence.target)
                    self.require_current(row, fence)
                    yield row

    def require_current(self, row: dict | None, fence: Fence) -> None:
        if (
            not managed(row)
            or row["holder"] != fence.owner_id
            or row["fence"] != fence.token
            or canonical_target(fence.target)
            != canonical_target(BranchKey(repository_id=row["repository_id"], branch=row["ref"]))
        ):
            raise StaleFence("ref lease holder or fence is stale")
        if row["expires_at"] is None or row["expires_at"] <= self.clock():
            raise StaleFence("ref lease expired")

    async def get(self, target: BranchKey) -> RefLease | None:
        async with self.db.immediate() as conn:
            row = await self.lock_on(conn, target)
            if not managed(row):
                return None
            return RefLease(
                canonical_target(target), row["holder"], row["fence"], row["expires_at"]
            )

    async def acquire(
        self,
        target: BranchKey,
        holder: str,
        *,
        ttl_seconds=DEFAULT_TTL_SECONDS,
        role="worker",
        conn=None,
    ) -> Fence:
        self._ttl(ttl_seconds)
        if not holder:
            raise ValueError("ref lease holder is required")
        async with asyncio.timeout(CRITICAL_SECTION_SECONDS):
            if conn is not None:
                return await self.acquire_on(
                    conn, target, holder, ttl_seconds=ttl_seconds, role=role
                )
            async with self.db.immediate() as owned:
                return await self.acquire_on(
                    owned, target, holder, ttl_seconds=ttl_seconds, role=role
                )

    async def acquire_on(
        self, conn, target, holder, *, ttl_seconds=DEFAULT_TTL_SECONDS, role="worker"
    ) -> Fence:
        self._ttl(ttl_seconds)
        target = canonical_target(target)
        if not holder:
            raise ValueError("ref lease holder is required")
        row = await self.lock_on(conn, target)
        now = self.clock()
        if row and managed(row) and row["holder"] and row["expires_at"] > now:
            if row["holder"] != holder:
                raise BranchBusy("ref has an unexpired lease")
            # Idempotent acquire never extends authority; heartbeat renews it.
            return Fence(target=target, owner_id=holder, token=row["fence"])
        if (
            row
            and not managed(row)
            and row["handoff_state"] != "released"
            and (row["expires_at"] is not None and row["expires_at"] > now)
        ):
            raise BranchBusy("ref has an unexpired legacy lease")
        token = max(row["fence"] or 0, row["fence_token"]) + 1 if row else 1
        values = dict(
            holder=holder,
            fence=token,
            expires_at=now + ttl_seconds,
            owner_id=holder,
            owner_role=role,
            fence_token=token,
            handoff_state="reserved",
            session_id=None,
            workspace_id=None,
            confirmed_workspace_id=None,
            updated_at=now,
        )
        if row:
            await conn.execute(update(owners).where(owners.c.id == row["id"]).values(**values))
        else:
            await conn.execute(
                insert(owners).values(
                    id=str(uuid.uuid4()),
                    repository_id=target.repository_id,
                    ref=target.branch,
                    created_at=now,
                    **values,
                )
            )
        return Fence(target=target, owner_id=holder, token=token)

    async def renew(self, fence: Fence, *, ttl_seconds=DEFAULT_TTL_SECONDS, conn=None) -> float:
        self._ttl(ttl_seconds)

        async def renew_on(owned):
            row = await self.lock_on(owned, fence.target)
            self.require_current(row, fence)
            expiry = max(row["expires_at"], self.clock() + ttl_seconds)
            await owned.execute(
                update(owners)
                .where(owners.c.id == row["id"])
                .values(expires_at=expiry, updated_at=self.clock())
            )
            return expiry

        async with asyncio.timeout(CRITICAL_SECTION_SECONDS):
            if conn is not None:
                return await renew_on(conn)
            async with self.db.immediate() as owned:
                return await renew_on(owned)

    async def release(self, fence: Fence, *, conn=None) -> bool:
        """Release only this identity, even after expiry; never erase the fence."""

        async def release_on(owned):
            row = await self.lock_on(owned, fence.target)
            if not managed(row) or row["holder"] != fence.owner_id or row["fence"] != fence.token:
                return False
            await owned.execute(
                update(owners)
                .where(owners.c.id == row["id"])
                .values(
                    holder=None,
                    expires_at=self.clock(),
                    handoff_state="released",
                    updated_at=self.clock(),
                )
            )
            return True

        async with asyncio.timeout(CRITICAL_SECTION_SECONDS):
            if conn is not None:
                return await release_on(conn)
            async with self.db.immediate() as owned:
                return await release_on(owned)

    async def fenced_push(
        self,
        fence: Fence,
        *,
        git: GitManager,
        checkout_path: str,
        repository: GitHubRepositoryBinding,
        tip_oid: str,
        expected_old_oid: str,
    ) -> str:
        """Push the captured expected-old OID with authenticated ambiguous read-back.

        Expiry bounds the transport as well as the DB check. A lost response is
        reconciled by GitManager against the actual remote; a moved remote requires
        rebuilding. This does not refresh expected_old_oid or fence identity.
        """
        async with self.exclusion(fence) as row:
            remaining = min(CRITICAL_SECTION_SECONDS, row["expires_at"] - self.clock())
            deadline = asyncio.get_running_loop().time() + remaining
            async with asyncio.timeout(remaining):
                branch = canonical_target(fence.target).branch
                try:
                    await git.apush_repository_oid(
                        checkout_path,
                        repository=repository,
                        tip_oid=tip_oid,
                        branch=branch,
                        expected_old_oid=expected_old_oid,
                        authority_deadline=deadline,
                    )
                    return tip_oid
                except GitError:
                    observed = await git.als_remote_ref(
                        checkout_path,
                        branch,
                        repository_url=f"https://github.com/{repository.full_name}.git",
                    )
                    if observed.state is not RemoteRefState.ERROR:
                        if observed.oid == tip_oid:
                            return tip_oid
                        if (observed.oid or "0" * 40) != expected_old_oid:
                            raise RemoteMoved(observed.oid) from None
                    raise


async def session_leases_on(conn, session_id: str, *, task_id: str | None = None) -> list[dict]:
    """Read adapter bindings, never workspace/stop proofs or a project lease."""
    statement = (
        select(owners)
        .where(
            owners.c.fence.is_not(None),
            owners.c.holder.is_not(None),
            owners.c.session_id == session_id,
        )
        .order_by(owners.c.repository_id, owners.c.ref)
    )
    if task_id is not None:
        statement = statement.where(owners.c.holder == task_id)
    return [dict(row) for row in (await conn.execute(statement)).mappings()]


async def renew_session_leases_on(db, conn, session_id: str, observed_at: float) -> None:
    """Heartbeat renews only a current live claim, and cannot revive an expiry."""
    session = (
        (await conn.execute(select(sessions).where(sessions.c.id == session_id).with_for_update()))
        .mappings()
        .one_or_none()
    )
    if (
        session is None
        or not session["task_id"]
        or session["state"] not in {"starting", "running", "draining"}
    ):
        return
    task = (
        (await conn.execute(select(tasks).where(tasks.c.id == session["task_id"])))
        .mappings()
        .one_or_none()
    )
    if (
        task is None
        or task["assigned_agent_id"] != session["agent_id"]
        or (session["lifecycle"] == "pool" and session["last_claim_epoch"] != task["claim_epoch"])
        or task["status"] not in {"ASSIGNED", "IN_PROGRESS"}
    ):
        return
    lock = BranchLock(db)
    for row in await session_leases_on(conn, session_id, task_id=task["id"]):
        fence = Fence(
            target=BranchKey(repository_id=row["repository_id"], branch=row["ref"]),
            owner_id=row["holder"],
            token=row["fence"],
        )
        try:
            # Old transcript observations do not grant runway starting now.
            ttl = observed_at + DEFAULT_TTL_SECONDS - lock.clock()
            if ttl > 0:
                await lock.renew(fence, ttl_seconds=ttl, conn=conn)
        except StaleFence:
            pass


async def release_session_leases_on(db, conn, session_id: str, task_id: str) -> None:
    """Release every lease this session holds for *task_id* in one statement.

    Equivalent to ``BranchLock.release`` per row (holder and fence are the
    row's own), without a read: the claim release path stays one statement.
    The fence is retained, as in ``release``.
    """
    now = BranchLock(db).clock()
    await conn.execute(
        update(owners)
        .where(
            owners.c.fence.is_not(None),
            owners.c.session_id == session_id,
            owners.c.holder == task_id,
        )
        .values(holder=None, expires_at=now, handoff_state="released", updated_at=now)
    )
