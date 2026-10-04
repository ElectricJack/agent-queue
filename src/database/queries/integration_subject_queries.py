"""Persistence for integration subjects and their append-only journal.

Rows only: :mod:`src.integration.subjects` owns the typed model and the
never-blocked schedule, and the reconciler owns every transition.  Each ``_on``
helper runs inside the caller's transaction; the others open their own.

Concurrency.  A visit reads a subject at ``version`` *v* and writes back with
:meth:`~IntegrationSubjectQueriesMixin.update_integration_subject_on`, which
succeeds only while the row is still at *v*.  Events never bump the version:
:meth:`~IntegrationSubjectQueriesMixin.wake_integration_subjects` pulls the due
time forward and stamps ``wake_requested_at``, and a visit's write that started
before that stamp keeps the subject due now.  A lost event therefore costs
latency, never progress, and an event never invalidates a visit in flight.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import Float, and_, case, func, literal, null, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from src.database.tables import integration_subject_journal, integration_subjects

#: Columns a visit may write.  Identity, the pinned policy, the version and
#: the timestamps are maintained here, never by the caller.
_MUTABLE_COLUMNS = frozenset(
    {
        "engine",
        "phase",
        "batch_id",
        "target_ref",
        "head_sha",
        "base_sha",
        "generation",
        "next_due_at",
        "due_set_at",
        "max_wait_seconds",
        "wait_reason",
        "gate_id",
        "refusal_streak",
        "last_visit_at",
        "closed_reason",
        "writer_status",
        "writer_task_id",
        "writer_fence_token",
        "writer_session_id",
        "writer_claimed_at",
        "writer_last_push_at",
        "writer_stop_proof",
        "budget_ordinal",
        "budget_class",
        "budget_started_at",
        "budget_deadline_at",
        "budget_attempts",
        "budget_attempt_limit",
    }
)


def _require_limit(limit: int) -> None:
    if limit <= 0:
        raise ValueError("integration subject page limit must be positive")


class IntegrationSubjectQueriesMixin:
    """Subjects: idempotent creation, keyset due pages, versioned writes, wakes."""

    # -- subjects ---------------------------------------------------------

    async def ensure_integration_subject_on(
        self, conn: AsyncConnection, values: dict[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        """Insert a subject unless ``(project_id, kind, subject_key)`` exists.

        Returns ``(row, created)``.  An existing row is returned unchanged:
        creation never overwrites a subject's phase, schedule or pinned policy.
        """
        inserted = (
            (
                await conn.execute(
                    pg_insert(integration_subjects)
                    .values(**values)
                    .on_conflict_do_nothing(constraint="uq_integration_subjects_key")
                    .returning(integration_subjects)
                )
            )
            .mappings()
            .first()
        )
        if inserted is not None:
            return dict(inserted), True
        row = (
            (
                await conn.execute(
                    select(integration_subjects).where(
                        integration_subjects.c.project_id == values["project_id"],
                        integration_subjects.c.kind == values["kind"],
                        integration_subjects.c.subject_key == values["subject_key"],
                    )
                )
            )
            .mappings()
            .one()
        )
        return dict(row), False

    async def ensure_integration_subject(
        self, values: dict[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        async with self._engine.begin() as conn:
            return await self.ensure_integration_subject_on(conn, values)

    async def get_integration_subject(self, subject_id: str) -> dict[str, Any] | None:
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(integration_subjects).where(integration_subjects.c.id == subject_id)
                    )
                )
                .mappings()
                .first()
            )
        return dict(row) if row is not None else None

    async def list_integration_subjects(
        self,
        *,
        project_id: str | None = None,
        subject_ids: Iterable[str] = (),
        task_ids: Iterable[str] = (),
        roots_only: bool = False,
        include_done: bool = False,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Subjects for the operator surfaces, soonest due first.

        ``project_id`` narrows to one project, ``subject_ids`` to named
        subjects, ``task_ids`` to subjects bound to or written by those tasks
        and ``roots_only`` to root subjects (no task of their own); nothing
        given lists every project.  Finished subjects are omitted unless
        ``include_done``.  Read-only: status, explain and doctor show what the
        reconciler holds and never write through this.
        """
        _require_limit(limit)
        due_at = integration_subjects.c.next_due_at
        statement = (
            select(integration_subjects)
            .order_by(due_at.asc().nulls_last(), integration_subjects.c.id)
            .limit(limit)
        )
        if project_id is not None:
            statement = statement.where(integration_subjects.c.project_id == project_id)
        wanted = sorted({value for value in subject_ids if value})
        if wanted:
            statement = statement.where(integration_subjects.c.id.in_(wanted))
        tasks = sorted({value for value in task_ids if value})
        if tasks:
            statement = statement.where(
                or_(
                    integration_subjects.c.task_id.in_(tasks),
                    integration_subjects.c.writer_task_id.in_(tasks),
                )
            )
        if roots_only:
            statement = statement.where(integration_subjects.c.task_id.is_(None))
        if not include_done:
            statement = statement.where(integration_subjects.c.phase != "done")
        async with self._engine.connect() as conn:
            rows = (await conn.execute(statement)).mappings().all()
        return [dict(row) for row in rows]

    async def get_integration_subject_by_key(
        self, *, project_id: str, kind: str, subject_key: str
    ) -> dict[str, Any] | None:
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(integration_subjects).where(
                            integration_subjects.c.project_id == project_id,
                            integration_subjects.c.kind == kind,
                            integration_subjects.c.subject_key == subject_key,
                        )
                    )
                )
                .mappings()
                .first()
            )
        return dict(row) if row is not None else None

    async def lock_integration_subject_on(
        self, conn: AsyncConnection, subject_id: str
    ) -> dict[str, Any] | None:
        row = (
            (
                await conn.execute(
                    select(integration_subjects)
                    .where(integration_subjects.c.id == subject_id)
                    .with_for_update()
                )
            )
            .mappings()
            .first()
        )
        return dict(row) if row is not None else None

    async def due_integration_subject_page(
        self,
        *,
        now: float,
        after: tuple[float, str] | None,
        limit: int,
        kinds: Sequence[str] | None = None,
        engine: str | None = None,
    ) -> list[dict[str, Any]]:
        """Live subjects due at ``now``, oldest due first, keyset-paged by ``(due, id)``."""
        _require_limit(limit)
        due_at = integration_subjects.c.next_due_at
        subject_id = integration_subjects.c.id
        statement = (
            select(integration_subjects)
            .where(
                integration_subjects.c.phase != "done",
                due_at.is_not(None),
                due_at <= now,
            )
            .order_by(due_at, subject_id)
            .limit(limit)
        )
        if kinds is not None:
            statement = statement.where(integration_subjects.c.kind.in_(list(kinds)))
        if engine is not None:
            statement = statement.where(integration_subjects.c.engine == engine)
        if after is not None:
            statement = statement.where(
                or_(due_at > after[0], and_(due_at == after[0], subject_id > after[1]))
            )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(statement)).mappings().all()
        return [dict(row) for row in rows]

    async def update_integration_subject_on(
        self,
        conn: AsyncConnection,
        *,
        subject_id: str,
        expected_version: int,
        values: dict[str, Any],
        now: float,
        visit_started_at: float | None = None,
    ) -> dict[str, Any] | None:
        """Write a visit's result if the subject is still at ``expected_version``.

        Returns the updated row, or ``None`` when another writer moved the
        subject first (the caller re-observes).  With ``visit_started_at``, a
        wake stamped at or after it keeps a live subject due ``now``.
        """
        unknown = set(values) - _MUTABLE_COLUMNS
        if unknown:
            raise ValueError(f"not writable by a visit: {sorted(unknown)}")
        assignments: dict[str, Any] = dict(values)
        closing = values.get("phase") == "done"
        if visit_started_at is not None and "next_due_at" in values and not closing:
            wake = integration_subjects.c.wake_requested_at
            requested_due = values["next_due_at"]
            assignments["next_due_at"] = case(
                (and_(wake.is_not(None), wake >= visit_started_at), literal(now, Float)),
                else_=null() if requested_due is None else literal(requested_due, Float),
            )
        assignments["version"] = integration_subjects.c.version + 1
        assignments["updated_at"] = now
        row = (
            (
                await conn.execute(
                    update(integration_subjects)
                    .where(
                        integration_subjects.c.id == subject_id,
                        integration_subjects.c.version == expected_version,
                    )
                    .values(**assignments)
                    .returning(integration_subjects)
                )
            )
            .mappings()
            .first()
        )
        return dict(row) if row is not None else None

    async def wake_integration_subjects(
        self,
        *,
        now: float,
        subject_ids: Iterable[str] = (),
        task_ids: Iterable[str] = (),
        writer_task_ids: Iterable[str] = (),
        batch_ids: Iterable[str] = (),
        gate_ids: Iterable[str] = (),
        project_ids: Iterable[str] = (),
    ) -> int:
        """Make every matching live subject due ``now``; returns how many.

        This is all an event does (§3.3): the due time moves earlier, never
        later, and the version is untouched.
        """
        conditions = []
        for column, ids in (
            (integration_subjects.c.id, subject_ids),
            (integration_subjects.c.task_id, task_ids),
            (integration_subjects.c.writer_task_id, writer_task_ids),
            (integration_subjects.c.batch_id, batch_ids),
            (integration_subjects.c.gate_id, gate_ids),
            (integration_subjects.c.project_id, project_ids),
        ):
            wanted = sorted({value for value in ids if value})
            if wanted:
                conditions.append(column.in_(wanted))
        if not conditions:
            return 0
        due_at = integration_subjects.c.next_due_at
        async with self._engine.begin() as conn:
            result = await conn.execute(
                update(integration_subjects)
                .where(integration_subjects.c.phase != "done", or_(*conditions))
                .values(
                    next_due_at=case(
                        (or_(due_at.is_(None), due_at > now), literal(now, Float)),
                        else_=due_at,
                    ),
                    wake_requested_at=func.greatest(
                        func.coalesce(
                            integration_subjects.c.wake_requested_at, literal(now, Float)
                        ),
                        literal(now, Float),
                    ),
                )
            )
        return int(result.rowcount or 0)

    # -- journal ----------------------------------------------------------

    async def append_integration_subject_journal_on(
        self, conn: AsyncConnection, values: dict[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        """Append one journal entry, replay-safe on ``(subject_id, idempotency_key)``.

        Returns ``(row, created)``; a replay returns the original entry.  The
        subject's ``last_journal_seq`` follows the newest entry without
        bumping its version.
        """
        inserted = (
            (
                await conn.execute(
                    pg_insert(integration_subject_journal)
                    .values(**values)
                    .on_conflict_do_nothing(constraint="uq_integration_subject_journal_idempotency")
                    .returning(integration_subject_journal)
                )
            )
            .mappings()
            .first()
        )
        if inserted is None:
            row = (
                (
                    await conn.execute(
                        select(integration_subject_journal).where(
                            integration_subject_journal.c.subject_id == values["subject_id"],
                            integration_subject_journal.c.idempotency_key
                            == values["idempotency_key"],
                        )
                    )
                )
                .mappings()
                .one()
            )
            return dict(row), False
        await conn.execute(
            update(integration_subjects)
            .where(integration_subjects.c.id == inserted["subject_id"])
            .values(
                last_journal_seq=func.greatest(
                    func.coalesce(integration_subjects.c.last_journal_seq, 0),
                    inserted["seq"],
                )
            )
        )
        return dict(inserted), True

    async def append_integration_subject_journal(
        self, values: dict[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        async with self._engine.begin() as conn:
            return await self.append_integration_subject_journal_on(conn, values)

    async def list_integration_subject_journal(
        self,
        subject_id: str,
        *,
        after_seq: int | None = None,
        limit: int = 100,
        entry_kinds: Sequence[str] | None = None,
        newest_first: bool = False,
    ) -> list[dict[str, Any]]:
        """A subject's journal in append order, keyset-paged by ``seq``.

        ``newest_first`` reads the latest ``limit`` entries, newest first, for
        ``aq integration explain``; it cannot be combined with ``after_seq``.
        """
        _require_limit(limit)
        if newest_first and after_seq is not None:
            raise ValueError("newest_first reads the latest entries; after_seq pages forward")
        seq = integration_subject_journal.c.seq
        statement = (
            select(integration_subject_journal)
            .where(integration_subject_journal.c.subject_id == subject_id)
            .order_by(seq.desc() if newest_first else seq)
            .limit(limit)
        )
        if after_seq is not None:
            statement = statement.where(integration_subject_journal.c.seq > after_seq)
        if entry_kinds is not None:
            statement = statement.where(
                integration_subject_journal.c.entry_kind.in_(list(entry_kinds))
            )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(statement)).mappings().all()
        return [dict(row) for row in rows]
