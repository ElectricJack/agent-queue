"""Persistence for immutable V2 artifacts and explicit activation records."""

from __future__ import annotations

import time
from collections.abc import Sequence
from uuid import uuid4

from sqlalchemy import delete, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.database.tables import (
    playbook_activations,
    playbook_artifacts,
    playbook_v2_runs,
)
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


#: Bound parameters per ``IN`` clause.  SQLite's historical limit is 999 and
#: PostgreSQL's is 65535; a few hundred keeps one statement comfortably inside
#: both while still collapsing a large directory scan into a handful of reads.
_SHA_BATCH = 400


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

    # -- retention and integrity (child plan §12.1, §12.2) -------------------

    async def list_playbook_activations(
        self, *, enabled_only: bool = False
    ) -> list[dict]:
        """Every activation row, newest first, as plain mappings.

        Read-only and unfiltered by scope on purpose: its two callers are the
        retention sweep's health refresh and the ``playbooks.artifact_integrity``
        doctor check, and both are box-wide sweeps rather than operator lookups.
        """
        stmt = select(playbook_activations).order_by(playbook_activations.c.updated_at.desc())
        if enabled_only:
            stmt = stmt.where(playbook_activations.c.enabled.is_(True))
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).mappings().fetchall()
        return [dict(row) for row in rows]

    async def mark_activation_unavailable(self, activation_id: str) -> None:
        """Persist ``unavailable`` health on one activation (§11 clause (b)).

        A targeted UPDATE rather than a ``set_playbook_activation`` round-trip:
        that call asserts the artifact *row* exists before writing, which is
        exactly the assertion a missing artifact fails, and it would also
        rewrite ``activated_by``/``activated_at`` with the sweeper's identity.
        Health is the only column a sweep is allowed to move.
        """
        async with self.immediate() as conn:
            await conn.execute(
                update(playbook_activations)
                .where(playbook_activations.c.activation_id == activation_id)
                .values(health="unavailable", updated_at=time.time())
            )

    async def get_playbook_artifact_path(self, artifact_sha256: str) -> str | None:
        """The stored file path for one artifact, or ``None`` when the row is gone."""
        async with self._engine.connect() as conn:
            row = await conn.execute(
                select(playbook_artifacts.c.path).where(
                    playbook_artifacts.c.artifact_sha256 == artifact_sha256
                )
            )
        return row.scalar_one_or_none()

    async def filter_referenced_artifact_shas(self, shas: Sequence[str]) -> set[str]:
        """Which of ``shas`` are still named by a row anywhere in V2 storage.

        Three tables, not one.  The artifact row is the ordinary reference,
        but the caller is the orphan-file half of the sweep (§12.1) and it is
        about to unlink files, so it must not depend on the ``RESTRICT``
        foreign keys actually being enforced — SQLite only enforces them when
        the connection asks (§7.4).  An activation or a run naming a hash
        whose artifact row has somehow gone therefore still protects the file:
        the answer is deliberately a superset of "has an artifact row".

        Chunked because the candidate list comes from a directory scan and is
        not bounded by anything the schema controls, while both backends cap
        bound parameters per statement.
        """
        wanted = [sha for sha in dict.fromkeys(shas) if sha]
        if not wanted:
            return set()
        referenced: set[str] = set()
        async with self._engine.connect() as conn:
            for start in range(0, len(wanted), _SHA_BATCH):
                batch = wanted[start : start + _SHA_BATCH]
                for column in (
                    playbook_artifacts.c.artifact_sha256,
                    playbook_activations.c.active_artifact_sha256,
                    playbook_v2_runs.c.artifact_sha256,
                ):
                    found = await conn.execute(select(column).where(column.in_(batch)))
                    referenced.update(sha for sha in found.scalars().all() if sha)
        return referenced

    async def collect_playbook_artifacts(
        self, before: float, *, min_versions: int = 10, limit: int = 1000
    ) -> list[tuple[str, str]]:
        """Delete collectable artifact rows and return their ``(sha, path)`` pairs.

        §12.1's three protections, all of them explicit queries rather than
        foreign keys (§7.4) — the FKs are ``RESTRICT``, which would turn a
        mistake here into an ``IntegrityError`` rather than data loss, but
        relying on that would make the sweep's outcome depend on whether
        SQLite happens to be enforcing FKs on this connection:

        1. referenced by an activation (``active_artifact_sha256``);
        2. referenced by any retained run (``playbook_v2_runs.artifact_sha256``);
        3. among the newest ``min_versions`` artifacts of its own playbook,
           ranked by ``version`` then ``created_at`` so a re-used version
           number cannot make the window ambiguous.

        Only the rows are deleted here.  The caller unlinks the files
        afterwards, in that order deliberately: a crash between the two
        leaves an unreferenced file that the next sweep removes, whereas the
        reverse order would leave a row pointing at a missing file and read
        as ``unavailable`` health on a playbook that was fine.
        """
        async with self.immediate() as conn:
            activated = select(playbook_activations.c.active_artifact_sha256).where(
                playbook_activations.c.active_artifact_sha256.is_not(None)
            )
            pinned_by_run = select(playbook_v2_runs.c.artifact_sha256)
            candidates = (
                (
                    await conn.execute(
                        select(
                            playbook_artifacts.c.artifact_sha256,
                            playbook_artifacts.c.playbook_id,
                            playbook_artifacts.c.path,
                        )
                        .where(
                            playbook_artifacts.c.created_at < before,
                            playbook_artifacts.c.artifact_sha256.not_in(
                                activated.scalar_subquery()
                            ),
                            playbook_artifacts.c.artifact_sha256.not_in(
                                pinned_by_run.scalar_subquery()
                            ),
                        )
                        .order_by(playbook_artifacts.c.created_at)
                        .limit(limit)
                    )
                )
                .mappings()
                .fetchall()
            )
            if not candidates:
                return []
            protected: set[str] = set()
            for playbook_id in {row["playbook_id"] for row in candidates}:
                newest = (
                    (
                        await conn.execute(
                            select(playbook_artifacts.c.artifact_sha256)
                            .where(playbook_artifacts.c.playbook_id == playbook_id)
                            .order_by(
                                playbook_artifacts.c.version.desc(),
                                playbook_artifacts.c.created_at.desc(),
                            )
                            .limit(max(min_versions, 0))
                        )
                    )
                    .scalars()
                    .all()
                )
                protected.update(newest)
            doomed = [
                (row["artifact_sha256"], row["path"])
                for row in candidates
                if row["artifact_sha256"] not in protected
            ]
            if not doomed:
                return []
            await conn.execute(
                delete(playbook_artifacts).where(
                    playbook_artifacts.c.artifact_sha256.in_([sha for sha, _ in doomed])
                )
            )
        return doomed
