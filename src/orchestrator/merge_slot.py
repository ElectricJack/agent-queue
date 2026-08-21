"""Merge slot — per-project integration lease.

Thin async facade over :class:`MergeSlotQueriesMixin`.  Worktree-execution
implementation spec §5.

Semantics
---------
* :func:`acquire_merge_slot` — atomic conditional acquire.  Returns True
  iff the caller now holds the lease.  A re-acquire by the current
  holder renews it (idempotent).
* :func:`renew_merge_slot` — an alias for :func:`acquire_merge_slot` that
  only succeeds when the caller is already the holder or the lease is
  expired.  In practice they are the same SQL; the two names document
  intent at call sites (``renew`` fails-fast if you lost the lease).
* :func:`release_merge_slot` — no-op when *task_id* is not the holder.
* :func:`break_expired_merge_slots` — cascade step; clears expired leases
  and emits ``merge.lease_broken`` for each broken project.

Events are emitted through the bus rather than logged so the reflection
pipeline sees them.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def acquire_merge_slot(db, project_id: str, task_id: str, ttl: float) -> bool:
    """Try to take the project's merge slot for *task_id*.  ``rowcount==1``."""
    return await db.acquire_merge_slot_row(project_id, task_id, ttl)


async def renew_merge_slot(db, project_id: str, task_id: str, ttl: float) -> bool:
    """Extend a lease the caller already holds.  False if not the holder."""
    return await db.acquire_merge_slot_row(project_id, task_id, ttl)


async def release_merge_slot(db, project_id: str, task_id: str) -> None:
    """Idempotent release.  A non-holder release is a no-op."""
    await db.release_merge_slot_row(project_id, task_id)


async def break_expired_merge_slots(db, bus) -> int:
    """Cascade step: clear expired leases.  Returns count broken."""
    broken = await db.break_expired_merge_slot_rows()
    if bus is not None:
        for project_id in broken:
            try:
                await bus.emit(
                    "merge.lease_broken",
                    {"project_id": project_id, "reason": "expired"},
                )
            except Exception as e:
                logger.warning("merge.lease_broken emit failed for %s: %s", project_id, e)
    return len(broken)
