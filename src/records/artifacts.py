"""Confined retained evidence shared by capture and explicit legacy imports."""

from src.knowledge.imports.apply import ImportService


class RetainedArtifacts(ImportService):
    """Use the existing immutable artifact namespace, hash lock and purge contract.

    No import operation is invoked. ``retain_on`` composes the artifact receipt
    with the caller's capture transaction; a rollback can leave only an orphan
    file, never a cursor pointing at uncommitted evidence. Reads check both the
    current tombstone and the exact bytes, rather than trusting filesystem data.
    """

    async def retain_on(self, content, *, access, conn):
        return await self._artifact(conn, access, content)

    async def read_on(self, artifact_id, *, scope_key, conn):
        import asyncio
        import hashlib

        from sqlalchemy import select

        from src.database.tables import record_source_artifacts
        from src.records.models import RecordError, uuid_value

        row = (
            (
                await conn.execute(
                    select(record_source_artifacts).where(
                        record_source_artifacts.c.artifact_id
                        == uuid_value(artifact_id, "artifact_id"),
                        record_source_artifacts.c.scope_key == scope_key,
                        record_source_artifacts.c.redacted_at.is_(None),
                    )
                )
            )
            .mappings()
            .first()
        )
        if not row or row["media_type"] == "application/vnd.aq.import-manifest":
            raise RecordError("extraction.source_unavailable")
        try:
            raw = await asyncio.to_thread(self._artifact_io, row["storage_key"])
        except (OSError, RecordError):
            raise RecordError("extraction.source_unavailable") from None
        if len(raw) != row["byte_size"] or hashlib.sha256(raw).hexdigest() != row["content_sha256"]:
            raise RecordError("extraction.source_unavailable")
        return raw, dict(row)
