"""Reads and writes for provider re-routes (``docs/specs/provider-failover.md`` D15-D17).

A re-route rewrites ``tasks.profile_id`` and appends one ``task_reroutes``
row in the same transaction, under the same "no worker holds it" predicate
``update_task_routing`` uses: a claim that wins after the planner's read can
never be silently retargeted.  ``tasks.rerouted_from`` is the cheap current
projection of the history -- the profile before the first re-route that has
not been undone -- and the partial index ``idx_tasks_rerouted`` is what the
re-route trickle counts.

Nothing here decides anything.  The policy is :mod:`src.providers.reroute`.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import and_, case, func, insert, select, update

from src.database.tables import messages, sessions, task_reroutes, tasks
from src.models import TaskStatus

#: Statuses a task can be re-routed (or undone) from: queued, never running.
REROUTABLE_STATUSES = (
    TaskStatus.DEFINED.value,
    TaskStatus.READY.value,
    TaskStatus.BLOCKED.value,
    TaskStatus.PAUSED.value,
)
#: The automatic reason code; the others are an operator's.
AUTOMATIC_REASON = "provider_unavailable"
UNDO_REASON = "operator_undo"

_UNSET = object()


def _unheld():
    """The routing guard: queued, unassigned, and no live session on it."""
    active_session = (
        select(sessions.c.id)
        .where(
            sessions.c.task_id == tasks.c.id,
            sessions.c.state.in_(("starting", "running", "draining")),
        )
        .exists()
    )
    return and_(
        tasks.c.status.in_(REROUTABLE_STATUSES),
        tasks.c.assigned_agent_id.is_(None),
        ~active_session,
    )


def _reroute_values(record: Mapping[str, Any], *, now: float) -> dict[str, Any]:
    return {
        "task_id": record["task_id"],
        "project_id": record["project_id"],
        "from_profile_id": record.get("from_profile_id"),
        "to_profile_id": record.get("to_profile_id"),
        "from_provider": record.get("from_provider") or "",
        "to_provider": record.get("to_provider") or "",
        "intelligence_class": record.get("intelligence_class"),
        "reason_code": record["reason_code"],
        "provider_state": record.get("provider_state") or "",
        "provider_generation": record.get("provider_generation"),
        "batch_id": record.get("batch_id"),
        "actor": record.get("actor") or "system",
        "at": float(record.get("at") or now),
    }


class TaskRerouteQueryMixin:
    """Query mixin for ``task_reroutes``.  Expects ``self._engine``."""

    async def apply_task_reroute(
        self,
        task_id: str,
        *,
        expected_profile_id: str,
        to_profile_id: str,
        record: Mapping[str, Any],
        intelligence_class: str | None | object = _UNSET,
    ) -> int | None:
        """Move *task_id* from *expected_profile_id* to *to_profile_id*, atomically.

        Returns the new ``task_reroutes`` id, or ``None`` when the guard lost
        (the task was claimed, started, finished or re-routed meanwhile).
        ``rerouted_from`` keeps the first profile of an un-undone chain, and
        is cleared when the move returns the task to it.
        """
        now = time.time()
        values: dict[str, Any] = {
            "profile_id": to_profile_id,
            "rerouted_from": case(
                (tasks.c.rerouted_from == to_profile_id, None),
                else_=func.coalesce(tasks.c.rerouted_from, expected_profile_id),
            ),
            "updated_at": now,
        }
        if intelligence_class is not _UNSET:
            values["intelligence_class"] = intelligence_class
        async with self._engine.begin() as conn:
            result = await conn.execute(
                update(tasks)
                .where(
                    tasks.c.id == task_id,
                    tasks.c.profile_id == expected_profile_id,
                    _unheld(),
                )
                .values(**values)
            )
            if result.rowcount != 1:
                return None
            inserted = await conn.execute(
                insert(task_reroutes)
                .values(**_reroute_values({**record, "task_id": task_id}, now=now))
                .returning(task_reroutes.c.id)
            )
            await self.mark_layout_dirty(
                record["project_id"], [task_id], "task.rerouted", conn=conn
            )
            return int(inserted.scalar_one())

    async def undo_task_reroute(
        self,
        task_id: str,
        *,
        record: Mapping[str, Any],
        intelligence_class: str | None | object = _UNSET,
    ) -> dict[str, Any] | None:
        """Return *task_id* to ``rerouted_from`` and mark its moves undone.

        Returns ``{"from_profile_id", "to_profile_id", "reroute_id"}`` or
        ``None`` when the task has no un-undone re-route or a worker holds it.
        """
        now = time.time()
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(
                    select(tasks.c.profile_id, tasks.c.rerouted_from, tasks.c.project_id)
                    .where(tasks.c.id == task_id)
                    .with_for_update()
                )
            ).first()
            if row is None or not row.rerouted_from:
                return None
            values: dict[str, Any] = {
                "profile_id": row.rerouted_from,
                "rerouted_from": None,
                "updated_at": now,
            }
            if intelligence_class is not _UNSET:
                values["intelligence_class"] = intelligence_class
            result = await conn.execute(
                update(tasks)
                .where(
                    tasks.c.id == task_id,
                    tasks.c.rerouted_from == row.rerouted_from,
                    _unheld(),
                )
                .values(**values)
            )
            if result.rowcount != 1:
                return None
            await conn.execute(
                update(task_reroutes)
                .where(
                    task_reroutes.c.task_id == task_id,
                    task_reroutes.c.undone_at.is_(None),
                    task_reroutes.c.reason_code != UNDO_REASON,
                )
                .values(undone_at=now)
            )
            inserted = await conn.execute(
                insert(task_reroutes)
                .values(
                    **_reroute_values(
                        {
                            **record,
                            "task_id": task_id,
                            "project_id": row.project_id,
                            "from_profile_id": row.profile_id,
                            "to_profile_id": row.rerouted_from,
                            "reason_code": UNDO_REASON,
                        },
                        now=now,
                    )
                )
                .returning(task_reroutes.c.id)
            )
            await self.mark_layout_dirty(row.project_id, [task_id], "task.rerouted", conn=conn)
            return {
                "from_profile_id": row.profile_id,
                "to_profile_id": row.rerouted_from,
                "reroute_id": int(inserted.scalar_one()),
            }

    async def list_task_reroutes(
        self,
        *,
        task_id: str | None = None,
        task_ids: Sequence[str] | None = None,
        batch_id: str | None = None,
        project_id: str | None = None,
        since: float | None = None,
        limit: int = 200,
    ) -> list[dict]:
        """``task_reroutes`` rows newest first, filtered."""
        stmt = select(task_reroutes)
        if task_id is not None:
            stmt = stmt.where(task_reroutes.c.task_id == task_id)
        if task_ids is not None:
            ids = sorted(set(task_ids))
            if not ids:
                return []
            stmt = stmt.where(task_reroutes.c.task_id.in_(ids))
        if batch_id is not None:
            stmt = stmt.where(task_reroutes.c.batch_id == batch_id)
        if project_id is not None:
            stmt = stmt.where(task_reroutes.c.project_id == project_id)
        if since is not None:
            stmt = stmt.where(task_reroutes.c.at >= float(since))
        stmt = stmt.order_by(task_reroutes.c.at.desc(), task_reroutes.c.id.desc()).limit(
            max(1, int(limit))
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).mappings().all()
        return [dict(row) for row in rows]

    async def latest_task_reroutes(self, task_ids: Sequence[str]) -> dict[str, dict]:
        """The newest ``task_reroutes`` row per task, for *task_ids*."""
        ids = sorted(set(task_ids))
        if not ids:
            return {}
        stmt = (
            select(task_reroutes)
            .where(task_reroutes.c.task_id.in_(ids))
            .order_by(task_reroutes.c.task_id, task_reroutes.c.at.desc(), task_reroutes.c.id.desc())
            .distinct(task_reroutes.c.task_id)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).mappings().all()
        return {row["task_id"]: dict(row) for row in rows}

    async def task_reroute_stats(self, task_ids: Sequence[str]) -> dict[str, dict]:
        """Per task: automatic move count, the newest automatic move, providers left.

        What the sweep's per-task limits read (D15: ``max_auto_per_task``,
        ``task_cooldown_seconds``; D12: "skipping any provider it already
        left").  Tasks with no re-route are absent.
        """
        ids = sorted(set(task_ids))
        if not ids:
            return {}
        stmt = select(
            task_reroutes.c.task_id,
            task_reroutes.c.reason_code,
            task_reroutes.c.from_provider,
            task_reroutes.c.at,
            task_reroutes.c.undone_at,
        ).where(task_reroutes.c.task_id.in_(ids))
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        stats: dict[str, dict] = {}
        for task_id, reason, from_provider, at, undone_at in rows:
            entry = stats.setdefault(
                task_id, {"auto_count": 0, "last_auto_at": None, "left_providers": set()}
            )
            if reason == UNDO_REASON:
                continue
            if reason == AUTOMATIC_REASON:
                entry["auto_count"] += 1
                if entry["last_auto_at"] is None or at > entry["last_auto_at"]:
                    entry["last_auto_at"] = at
            if undone_at is None and from_provider:
                entry["left_providers"].add(from_provider)
        return stats

    async def count_rerouted_queued_by_profile(self) -> dict[str, int]:
        """Moved-and-not-yet-started tasks per current profile (the D15 trickle count)."""
        stmt = (
            select(tasks.c.profile_id, func.count())
            .where(
                tasks.c.rerouted_from.is_not(None),
                tasks.c.profile_id.is_not(None),
                tasks.c.status.in_(REROUTABLE_STATUSES),
                tasks.c.assigned_agent_id.is_(None),
            )
            .group_by(tasks.c.profile_id)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return {profile_id: int(count) for profile_id, count in rows}

    async def list_reroute_candidates(
        self,
        profile_ids: Sequence[str],
        *,
        task_ids: Sequence[str] | None = None,
    ) -> list[dict]:
        """Queued tasks on *profile_ids* the sweep may move, in claim order.

        ``READY`` unblocked unassigned rows, plus every ``PAUSED`` row on an
        automatic backoff (``resume_after`` set -- never an operator hold) so
        the caller can tell a provider pause (``task_metadata
        ['provider_pause']``) from a legacy one.  *task_ids* narrows to
        explicit tasks and then admits every queued status.
        """
        ids = sorted(set(profile_ids))
        if not ids and task_ids is None:
            return []
        conditions = [tasks.c.assigned_agent_id.is_(None)]
        if ids:
            conditions.append(tasks.c.profile_id.in_(ids))
        if task_ids is not None:
            wanted = sorted(set(task_ids))
            if not wanted:
                return []
            conditions.append(tasks.c.id.in_(wanted))
            conditions.append(tasks.c.status.in_(REROUTABLE_STATUSES))
        else:
            ready = and_(tasks.c.status == TaskStatus.READY.value, tasks.c.is_blocked == 0)
            paused = and_(
                tasks.c.status == TaskStatus.PAUSED.value, tasks.c.resume_after.is_not(None)
            )
            conditions.append(ready | paused)
        stmt = (
            select(tasks)
            .where(and_(*conditions))
            .order_by(tasks.c.priority.asc(), tasks.c.created_at.asc(), tasks.c.id.asc())
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).mappings().all()
        return [dict(row) for row in rows]

    async def reroute_notice_sent(self, thread_id: str, to_kind: str, to_id: str) -> bool:
        """Has a batch notice with *thread_id* already gone to ``to_kind:to_id``?

        The per-project supervisor notice is keyed by its ``thread_id``
        (``reroute:<batch>:<project>``), so one batch messages each project
        once whatever the number of sweeps (D19).
        """
        stmt = (
            select(messages.c.id)
            .where(
                messages.c.thread_id == thread_id,
                messages.c.to_kind == to_kind,
                messages.c.to_id == to_id,
            )
            .limit(1)
        )
        async with self._engine.connect() as conn:
            return (await conn.execute(stmt)).first() is not None


__all__ = [
    "AUTOMATIC_REASON",
    "REROUTABLE_STATUSES",
    "UNDO_REASON",
    "TaskRerouteQueryMixin",
]
