"""Operator acknowledgement rows for the Playbook V2 migration.

Package 6 (``docs/superpowers/plans/2026-09-01-playbook-v2-migration-artifacts.md``
§6).  One row is one written waiver: an operator declaring that a playbook
cannot be migrated to V2 and that the fleet may cut over without it.

Every waiver is keyed by ``(playbook_id, scope, scope_identifier,
source_sha256)``.  Editing the authoring Markdown changes the hash, the ack
stops matching, and the playbook returns to ``question_required`` — an operator
cannot acknowledge a playbook once and have the waiver survive a rewrite.
"""

from __future__ import annotations

import time

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.database.tables import playbook_migration_acks

#: Minimum length of an acknowledgement's justification.  Enforced here, at the
#: command boundary, and by a table check constraint: an empty waiver is not a
#: waiver, and this is the one mechanism in Package 6 capable of moving the
#: fleet past a real problem.
MIN_ACK_REASON_LENGTH = 12


class PlaybookMigrationQueryMixin:
    """Read and write ``playbook_migration_acks``."""

    async def upsert_playbook_migration_ack(
        self,
        *,
        playbook_id: str,
        scope: str,
        scope_identifier: str = "",
        source_sha256: str,
        reason: str,
        acknowledged_by: str,
        acknowledged_at: float | None = None,
    ) -> dict:
        """Record or replace one waiver.

        ``acknowledged_by`` is the caller's server-derived principal identity.
        This method never reads it from a request body — see
        ``src/commands/playbook_migration_commands.py``.
        """
        if len(reason.strip()) < MIN_ACK_REASON_LENGTH:
            raise ValueError(
                f"an acknowledgement reason must be at least {MIN_ACK_REASON_LENGTH} characters"
            )
        values = {
            "playbook_id": playbook_id,
            "scope": scope,
            "scope_identifier": scope_identifier or "",
            "source_sha256": source_sha256,
            "reason": reason.strip(),
            "acknowledged_by": acknowledged_by,
            "acknowledged_at": time.time() if acknowledged_at is None else acknowledged_at,
        }
        async with self._engine.begin() as conn:
            insert_fn = pg_insert if conn.dialect.name == "postgresql" else sqlite_insert
            statement = insert_fn(playbook_migration_acks).values(**values)
            await conn.execute(
                statement.on_conflict_do_update(
                    index_elements=[
                        playbook_migration_acks.c.playbook_id,
                        playbook_migration_acks.c.scope,
                        playbook_migration_acks.c.scope_identifier,
                    ],
                    set_={
                        field: getattr(statement.excluded, field)
                        for field in (
                            "source_sha256",
                            "reason",
                            "acknowledged_by",
                            "acknowledged_at",
                        )
                    },
                )
            )
        return values

    async def delete_playbook_migration_ack(
        self, *, playbook_id: str, scope: str, scope_identifier: str = ""
    ) -> bool:
        """Remove one waiver; the entry returns to its computed disposition."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                delete(playbook_migration_acks).where(
                    playbook_migration_acks.c.playbook_id == playbook_id,
                    playbook_migration_acks.c.scope == scope,
                    playbook_migration_acks.c.scope_identifier == (scope_identifier or ""),
                )
            )
        return bool(result.rowcount)

    async def list_playbook_migration_acks(self) -> list[dict]:
        """Every waiver, sorted for a stable report."""
        stmt = select(playbook_migration_acks).order_by(
            playbook_migration_acks.c.scope,
            playbook_migration_acks.c.scope_identifier,
            playbook_migration_acks.c.playbook_id,
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).mappings().fetchall()
        return [dict(row) for row in rows]

    # The inventory's ``ack_repo`` protocol (§3.2) names this ``list_acks``;
    # the database class keeps the long, table-qualified name every other
    # query mixin uses.
    async def list_acks(self) -> list[dict]:
        return await self.list_playbook_migration_acks()
