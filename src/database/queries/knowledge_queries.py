"""Knowledge persistence primitives on caller-owned transactions.

No authorization, canonicalization or provider work happens here. K02's service
must authorize and validate before using these methods. Exact reads never fall
back to the current revision. Payload changes are forbidden by database guards.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.database.tables import (
    knowledge_records,
    knowledge_revision_payloads,
    knowledge_revisions,
    knowledge_search,
)


class KnowledgeQueryMixin:
    async def project_knowledge_search_on(
        self, record_id, revision_id, scope_key, snapshot, updated_at, *, conn
    ) -> None:
        values = {
            key: snapshot[key]
            for key in (
                "title",
                "summary",
                "category",
                "lifecycle",
                "verification",
                "valid_until",
                "recheck_at",
            )
        }
        for key in ("valid_until", "recheck_at"):
            if values[key] is not None:
                values[key] = datetime.fromisoformat(values[key])
        values.update(
            record_id=record_id,
            revision_id=revision_id,
            scope_key=scope_key,
            updated_at=updated_at,
            search_vector=func.to_tsvector(
                "simple",
                snapshot["title"] + " " + (snapshot["summary"] or "") + " " + snapshot["body"],
            ),
        )
        await conn.execute(
            pg_insert(knowledge_search)
            .values(**values)
            .on_conflict_do_update(
                index_elements=[knowledge_search.c.record_id],
                set_=values,
            )
        )

    async def insert_knowledge_record_on(self, values: dict, *, conn) -> None:
        await conn.execute(insert(knowledge_records).values(**values))

    async def append_knowledge_revision_on(self, revision: dict, snapshot: dict, *, conn) -> None:
        await conn.execute(insert(knowledge_revisions).values(**revision))
        await conn.execute(
            insert(knowledge_revision_payloads).values(
                revision_id=revision["revision_id"],
                snapshot=snapshot,
            )
        )

    async def advance_knowledge_head_on(
        self,
        record_id: UUID,
        *,
        expected_revision: UUID,
        revision_id: UUID,
        sequence: int,
        updated_at,
        conn,
    ) -> bool:
        result = await conn.execute(
            update(knowledge_records)
            .where(
                knowledge_records.c.record_id == record_id,
                knowledge_records.c.current_revision_id == expected_revision,
            )
            .values(
                current_revision_id=revision_id, current_sequence=sequence, updated_at=updated_at
            )
        )
        return result.rowcount == 1

    async def get_knowledge_revision_on(
        self,
        record_id: UUID,
        *,
        revision_id: UUID | None = None,
        conn,
    ) -> dict | None:
        stmt = (
            select(
                knowledge_revisions,
                knowledge_revision_payloads.c.snapshot,
                knowledge_revision_payloads.c.redacted_at,
                knowledge_revision_payloads.c.redaction_id,
            )
            .join(
                knowledge_revision_payloads,
                knowledge_revision_payloads.c.revision_id == knowledge_revisions.c.revision_id,
            )
            .where(knowledge_revisions.c.record_id == record_id)
        )
        if revision_id is None:
            stmt = stmt.join(
                knowledge_records,
                knowledge_records.c.current_revision_id == knowledge_revisions.c.revision_id,
            )
        else:
            stmt = stmt.where(knowledge_revisions.c.revision_id == revision_id)
        row = (await conn.execute(stmt)).mappings().first()
        return dict(row) if row else None

    async def list_knowledge_history_on(
        self,
        record_id: UUID,
        *,
        before_sequence: int | None = None,
        limit: int = 25,
        conn,
    ) -> list[dict]:
        if not 1 <= limit <= 100:
            raise ValueError("history limit must be between 1 and 100")
        stmt = (
            select(knowledge_revisions, knowledge_revision_payloads.c.redacted_at)
            .join(
                knowledge_revision_payloads,
                knowledge_revision_payloads.c.revision_id == knowledge_revisions.c.revision_id,
            )
            .where(knowledge_revisions.c.record_id == record_id)
        )
        if before_sequence is not None:
            stmt = stmt.where(knowledge_revisions.c.sequence < before_sequence)
        rows = await conn.execute(stmt.order_by(knowledge_revisions.c.sequence.desc()).limit(limit))
        return [dict(row) for row in rows.mappings()]
