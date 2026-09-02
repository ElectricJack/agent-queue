"""Persistence for immutable V2 artifacts and explicit activation records."""

from __future__ import annotations

import time
from uuid import uuid4

from sqlalchemy import insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.database.tables import playbook_activations, playbook_artifacts
from src.playbooks.artifact_ref import ArtifactRef

#: Everything but the surrogate key: a re-activation of the same
#: ``(playbook_id, scope, scope_identifier)`` rewrites the record in place and
#: keeps the ``activation_id`` anything else may already reference.
_ACTIVATION_UPDATE_COLUMNS = (
    "active_artifact_sha256",
    "enabled",
    "health",
    "reasons",
    "activated_at",
    "activated_by",
    "updated_at",
)


class ArtifactNotFound(ValueError):
    """An activation cannot point at an artifact row which does not exist."""


class PlaybookArtifactQueryMixin:
    async def upsert_playbook_artifact(
        self,
        ref: ArtifactRef,
        *,
        scope: str,
        scope_identifier: str = "",
        profile_fingerprint: str = "",
        path: str,
        size_bytes: int,
        validation: str = "{}",
    ) -> None:
        values = {
            **ref.as_dict(),
            "scope": scope,
            "scope_identifier": scope_identifier,
            "profile_fingerprint": profile_fingerprint,
            "path": path,
            "size_bytes": size_bytes,
            "validation": validation,
            "created_at": time.time(),
        }
        async with self.immediate() as conn:
            existing = await conn.execute(
                select(playbook_artifacts.c.artifact_sha256).where(
                    playbook_artifacts.c.artifact_sha256 == ref.artifact_sha256
                )
            )
            if existing.scalar_one_or_none() is None:
                await conn.execute(insert(playbook_artifacts).values(**values))

    async def get_playbook_artifact(self, artifact_sha256: str) -> ArtifactRef | None:
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(
                    select(playbook_artifacts).where(
                        playbook_artifacts.c.artifact_sha256 == artifact_sha256
                    )
                )
            ).mappings().fetchone()
        return ArtifactRef.from_row(row) if row else None

    async def set_playbook_activation(
        self,
        *,
        playbook_id: str,
        scope: str,
        scope_identifier: str,
        artifact_sha256: str | None,
        enabled: bool,
        activated_by: str | None,
        health: str,
        reasons: str,
    ) -> None:
        async with self.immediate() as conn:
            if artifact_sha256 is not None:
                found = await conn.execute(
                    select(playbook_artifacts.c.artifact_sha256).where(
                        playbook_artifacts.c.artifact_sha256 == artifact_sha256
                    )
                )
                if found.scalar_one_or_none() is None:
                    raise ArtifactNotFound(artifact_sha256)
            now = time.time()
            values = {
                "activation_id": str(uuid4()),
                "playbook_id": playbook_id,
                "scope": scope,
                "scope_identifier": scope_identifier,
                "active_artifact_sha256": artifact_sha256,
                "enabled": enabled,
                "health": health,
                "reasons": reasons,
                "activated_at": now if artifact_sha256 else None,
                "activated_by": activated_by,
                "updated_at": now,
            }
            # An upsert, never insert-then-recover-from-IntegrityError: on
            # PostgreSQL a constraint violation aborts the surrounding
            # transaction, so a recovery UPDATE on the same connection dies
            # with "current transaction is aborted" instead of updating.
            dialect = conn.dialect.name
            if dialect in ("postgresql", "sqlite"):
                insert_fn = pg_insert if dialect == "postgresql" else sqlite_insert
                statement = insert_fn(playbook_activations).values(**values)
                await conn.execute(
                    statement.on_conflict_do_update(
                        index_elements=["playbook_id", "scope", "scope_identifier"],
                        set_={
                            column: statement.excluded[column]
                            for column in _ACTIVATION_UPDATE_COLUMNS
                        },
                    )
                )
                return
            # Generic fallback for any other dialect: update first, insert
            # only when the row is genuinely absent.
            result = await conn.execute(
                update(playbook_activations)
                .where(
                    (playbook_activations.c.playbook_id == playbook_id)
                    & (playbook_activations.c.scope == scope)
                    & (playbook_activations.c.scope_identifier == scope_identifier)
                )
                .values(**{column: values[column] for column in _ACTIVATION_UPDATE_COLUMNS})
            )
            if result.rowcount == 0:
                await conn.execute(insert(playbook_activations).values(**values))
