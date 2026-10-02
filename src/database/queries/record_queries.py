"""Connection-owned record primitives; authorization belongs to the core service.

These helpers never commit and are not command surfaces. Task identity resolution
reads execution projections without modifying or locking task lifecycle state.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy import and_, insert, literal, or_, select, union_all, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.database.tables import (
    archived_tasks,
    projects,
    record_installation,
    record_link_heads,
    record_link_versions,
    record_requests,
    record_scopes,
    records,
    task_record_link_state,
    tasks,
)
from src.records.identity import RecordIntegrityError, task_record_id


class RecordDomainUnavailable(ValueError):
    """The permanent mapping exists, but its execution domain no longer does."""

    code = "record.domain_unavailable"


class RecordQueryMixin:
    async def begin_record_request_on(
        self, *, scope_key, actor_key, operation, idempotency_key, request_sha256, conn
    ) -> dict:
        key = dict(
            scope_key=scope_key,
            actor_key=actor_key,
            operation=operation,
            idempotency_key=idempotency_key,
        )
        await conn.execute(
            pg_insert(record_requests)
            .values(**key, request_sha256=request_sha256, result={})
            .on_conflict_do_nothing()
        )
        row = await conn.execute(
            select(record_requests)
            .where(*(record_requests.c[name] == value for name, value in key.items()))
            .with_for_update()
        )
        return dict(row.mappings().one())

    async def finish_record_request_on(self, receipt: dict, result: dict, *, conn) -> None:
        keys = ("scope_key", "actor_key", "operation", "idempotency_key")
        await conn.execute(
            update(record_requests)
            .where(*(record_requests.c[name] == receipt[name] for name in keys))
            .values(result=result)
        )

    async def current_record_links_on(self, record_id, *, conn, link_ids=()) -> list[dict]:
        rows = await conn.execute(
            select(record_link_versions)
            .join(
                record_link_heads,
                and_(
                    record_link_heads.c.link_id == record_link_versions.c.link_id,
                    record_link_heads.c.current_version == record_link_versions.c.version,
                ),
            )
            .where(
                record_link_heads.c.source_record_id == record_id,
                or_(
                    record_link_versions.c.removed.is_(False),
                    record_link_heads.c.link_id.in_(link_ids),
                ),
            )
            .order_by(record_link_heads.c.link_id)
        )
        return [dict(row) for row in rows.mappings()]

    async def get_record_installation_on(self, *, conn) -> UUID:
        return (await conn.execute(select(record_installation.c.installation_id))).scalar_one()

    async def ensure_record_scope_on(self, *, project_id: str | None, conn) -> str:
        """None explicitly selects global; callers must already authorize that scope."""
        if project_id is not None:
            if not project_id or not await conn.scalar(
                select(projects.c.id).where(projects.c.id == project_id)
            ):
                raise RecordDomainUnavailable("record scope project is unavailable")
            scope_key = f"project:{project_id}"
        else:
            scope_key = "global"
        await conn.execute(
            pg_insert(record_scopes)
            .values(
                scope_key=scope_key,
                scope_kind="global" if project_id is None else "project",
                project_id=project_id,
            )
            .on_conflict_do_nothing(index_elements=[record_scopes.c.scope_key])
        )
        return scope_key

    async def get_record_on(
        self,
        *,
        record_id: UUID | None = None,
        task_id: str | None = None,
        knowledge_alias: str | None = None,
        lock: bool = False,
        conn,
    ) -> dict | None:
        candidates = [
            (records.c.record_id, record_id),
            (records.c.task_id, task_id),
            (records.c.knowledge_alias, knowledge_alias),
        ]
        supplied = [(column, value) for column, value in candidates if value is not None]
        if len(supplied) != 1:
            raise ValueError("supply exactly one record identity")
        column, value = supplied[0]
        stmt = select(records).where(column == value)
        if lock:
            stmt = stmt.with_for_update()
        row = (await conn.execute(stmt)).mappings().first()
        return dict(row) if row else None

    async def insert_record_on(self, values: dict, *, conn) -> None:
        await conn.execute(insert(records).values(**values))

    async def get_task_record_domain_on(self, task_id: str, *, conn) -> dict | None:
        """Resolve live/archive in one snapshot; duplicates are never guessed away."""
        fields = (
            "id",
            "project_id",
            "title",
            "description",
            "status",
            "priority",
            "created_at",
            "updated_at",
        )
        stmt = union_all(
            *(
                select(
                    *(table.c[name] for name in fields), literal(archived).label("archived")
                ).where(table.c.id == task_id)
                for table, archived in ((tasks, False), (archived_tasks, True))
            )
        )
        rows = (await conn.execute(stmt)).mappings().all()
        if len(rows) > 1:
            raise RecordIntegrityError("task alias exists in both live and archive domains")
        return dict(rows[0]) if rows else None

    async def ensure_task_record_on(self, task_id: str, *, actor_id: str, conn) -> dict:
        domain = await self.get_task_record_domain_on(task_id, conn=conn)
        existing = await self.get_record_on(task_id=task_id, conn=conn)
        if domain is None:
            if existing:
                raise RecordDomainUnavailable("task was deleted; its mapping remains reserved")
            raise LookupError("record.not_found")
        scope_key = await self.ensure_record_scope_on(project_id=domain["project_id"], conn=conn)
        record_id = task_record_id(await self.get_record_installation_on(conn=conn), task_id)
        # Both alias and UUID uniqueness are checked after conflict-safe insertion.
        # A UUID collision is never a request to allocate another random mapping.
        await conn.execute(
            pg_insert(records)
            .values(
                record_id=record_id,
                kind="task",
                scope_key=scope_key,
                task_id=task_id,
                created_by=actor_id,
            )
            .on_conflict_do_nothing()
        )
        row = await self.get_record_on(record_id=record_id, conn=conn)
        if row is None or (
            row["kind"],
            row["scope_key"],
            row["task_id"],
            row["knowledge_alias"],
        ) != (
            "task",
            scope_key,
            task_id,
            None,
        ):
            raise RecordIntegrityError("task UUID/alias collides with a different identity")
        await conn.execute(
            pg_insert(task_record_link_state)
            .values(
                record_id=record_id,
                link_sequence=0,
                link_token=uuid4(),
            )
            .on_conflict_do_nothing(index_elements=[task_record_link_state.c.record_id])
        )
        return row
