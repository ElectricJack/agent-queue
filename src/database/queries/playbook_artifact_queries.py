"""Persistence for immutable V2 artifacts and explicit activation records."""

from __future__ import annotations

import time
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.database.tables import playbook_activations, playbook_artifacts
from src.playbooks.artifact_ref import ArtifactRef


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
        """Insert immutable artifact identity or refresh its mutable storage metadata.

        The content-addressed identity fields are retained once the artifact
        exists.  The local path, byte size, validation result, and profile
        fingerprint can change when the same canonical bytes are rediscovered
        or revalidated, so those fields are refreshed on subsequent calls.
        """
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
            insert_fn = pg_insert if conn.dialect.name == "postgresql" else sqlite_insert
            statement = insert_fn(playbook_artifacts).values(**values)
            await conn.execute(
                statement.on_conflict_do_update(
                    index_elements=[playbook_artifacts.c.artifact_sha256],
                    set_={
                        field: getattr(statement.excluded, field)
                        for field in ("profile_fingerprint", "path", "size_bytes", "validation")
                    },
                )
            )

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
            insert_fn = pg_insert if conn.dialect.name == "postgresql" else sqlite_insert
            statement = insert_fn(playbook_activations).values(**values)
            await conn.execute(
                statement.on_conflict_do_update(
                    index_elements=[
                        playbook_activations.c.playbook_id,
                        playbook_activations.c.scope,
                        playbook_activations.c.scope_identifier,
                    ],
                    set_={
                        field: getattr(statement.excluded, field)
                        for field in values
                        if field != "activation_id"
                    },
                )
            )
