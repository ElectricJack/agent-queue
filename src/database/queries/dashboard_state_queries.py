"""Persistence for revisioned dashboard state documents."""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import delete, exists, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.database.tables import dashboard_state_documents, projects


class DashboardStateQueriesMixin:
    """Generic document queries; namespace policy stays in the service layer."""

    async def get_dashboard_document(
        self,
        *,
        scope: str,
        owner_id: str,
        namespace: str,
        subject: str,
        conn=None,
    ) -> dict | None:
        stmt = select(dashboard_state_documents).where(
            dashboard_state_documents.c.scope == scope,
            dashboard_state_documents.c.owner_id == owner_id,
            dashboard_state_documents.c.namespace == namespace,
            dashboard_state_documents.c.subject == subject,
        )
        if conn is not None:
            row = (await conn.execute(stmt)).mappings().one_or_none()
            return dict(row) if row is not None else None
        async with self._engine.begin() as owned:
            row = (await owned.execute(stmt)).mappings().one_or_none()
            return dict(row) if row is not None else None

    async def list_dashboard_documents(self, *, owner_id: str) -> list[dict]:
        stmt = (
            select(dashboard_state_documents)
            .where(
                or_(
                    (
                        (dashboard_state_documents.c.scope == "workspace")
                        & (dashboard_state_documents.c.owner_id == "")
                    ),
                    (
                        (dashboard_state_documents.c.scope == "user")
                        & (dashboard_state_documents.c.owner_id == owner_id)
                    ),
                )
            )
            .order_by(
                dashboard_state_documents.c.namespace,
                dashboard_state_documents.c.subject,
            )
        )
        async with self._engine.begin() as conn:
            rows = (await conn.execute(stmt)).mappings().all()
            return [dict(row) for row in rows]

    async def write_dashboard_document(
        self,
        *,
        scope: str,
        owner_id: str,
        namespace: str,
        subject: str,
        value: dict | None,
        base_revision: int | None,
        now: float,
    ) -> tuple[dict | None, dict | None]:
        """Write/reset one document, returning a current row on CAS loss."""
        table = dashboard_state_documents
        key = (
            (table.c.scope == scope)
            & (table.c.owner_id == owner_id)
            & (table.c.namespace == namespace)
            & (table.c.subject == subject)
        )

        async with self._engine.begin() as conn:
            if base_revision is not None and base_revision > 0:
                result = await conn.execute(
                    update(table)
                    .where(key, table.c.revision == base_revision)
                    .values(
                        revision=table.c.revision + 1,
                        value=value,
                        updated_at=now,
                    )
                    .returning(table)
                )
            else:
                statement = pg_insert(table).values(
                    scope=scope,
                    owner_id=owner_id,
                    namespace=namespace,
                    subject=subject,
                    revision=1,
                    value=value,
                    created_at=now,
                    updated_at=now,
                )
                update_where = None if base_revision is None else table.c.revision == 0
                statement = statement.on_conflict_do_update(
                    index_elements=["scope", "owner_id", "namespace", "subject"],
                    set_={
                        "revision": table.c.revision + 1,
                        "value": value,
                        "updated_at": now,
                    },
                    where=update_where,
                )
                result = await conn.execute(statement.returning(table))

            row = result.mappings().one_or_none()
            if row is not None:
                return dict(row), None
            current = (await conn.execute(select(table).where(key))).mappings().one_or_none()
            return None, dict(current) if current is not None else None

    async def list_orphan_dashboard_documents(
        self, *, project_namespaces: Iterable[str]
    ) -> list[dict]:
        names = tuple(project_namespaces)
        if not names:
            return []
        stmt = (
            select(dashboard_state_documents)
            .where(
                dashboard_state_documents.c.namespace.in_(names),
                ~exists(
                    select(projects.c.id).where(
                        projects.c.id == dashboard_state_documents.c.subject
                    )
                ),
            )
            .order_by(
                dashboard_state_documents.c.namespace,
                dashboard_state_documents.c.subject,
                dashboard_state_documents.c.owner_id,
            )
        )
        async with self._engine.begin() as conn:
            return [dict(row) for row in (await conn.execute(stmt)).mappings().all()]

    async def delete_orphan_dashboard_documents(self, *, project_namespaces: Iterable[str]) -> int:
        names = tuple(project_namespaces)
        if not names:
            return 0
        async with self._engine.begin() as conn:
            result = await conn.execute(
                delete(dashboard_state_documents).where(
                    dashboard_state_documents.c.namespace.in_(names),
                    ~exists(
                        select(projects.c.id).where(
                            projects.c.id == dashboard_state_documents.c.subject
                        )
                    ),
                )
            )
            return int(result.rowcount or 0)

    async def delete_dashboard_documents_for_project(self, project_id: str, *, conn) -> int:
        result = await conn.execute(
            delete(dashboard_state_documents).where(
                dashboard_state_documents.c.subject == project_id,
            )
        )
        return int(result.rowcount or 0)
