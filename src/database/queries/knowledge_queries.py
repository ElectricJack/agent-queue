"""Knowledge persistence primitives on caller-owned transactions.

No authorization, canonicalization or provider work happens here. K02's service
must authorize and validate before using these methods. Exact reads never fall
back to the current revision. Payload changes are forbidden by database guards.
"""

from uuid import UUID

from sqlalchemy import insert, select, update

from src.database.tables import knowledge_records, knowledge_revision_payloads, knowledge_revisions


class KnowledgeQueryMixin:
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
        stmt = select(knowledge_revisions).where(knowledge_revisions.c.record_id == record_id)
        if before_sequence is not None:
            stmt = stmt.where(knowledge_revisions.c.sequence < before_sequence)
        rows = await conn.execute(stmt.order_by(knowledge_revisions.c.sequence.desc()).limit(limit))
        return [dict(row) for row in rows.mappings()]
