"""Persistence for immutable V2 artifacts and explicit activation records."""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from src.database.tables import (
    integration_outbox,
    playbook_activations,
    playbook_artifacts,
    playbook_pending_events,
    playbook_v2_runs,
)
from src.playbooks.artifact_ref import ArtifactRef
from src.playbooks.artifact_tombstone import restore_any

logger = logging.getLogger(__name__)

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


#: The artifact columns :meth:`list_playbook_activations_with_artifacts` adds
#: to each activation row.  ``playbook_id``, ``scope`` and ``scope_identifier``
#: are deliberately absent: they exist on both tables, and the activation's
#: values are the authority for where the playbook is installed.
_ACTIVATION_ARTIFACT_COLUMNS = (
    "artifact_sha256",
    "schema_generation",
    "version",
    "source_digest",
    "contract_fingerprint",
    "profile_fingerprint",
    "compiler_build",
    "path",
    "size_bytes",
    "validation",
    "compiled_at",
)


#: Bound parameters per ``IN`` clause.  SQLite's historical limit is 999 and
#: PostgreSQL's is 65535; a few hundred keeps one statement comfortably inside
#: both while still collapsing a large directory scan into a handful of reads.
_SHA_BATCH = 400


class ArtifactNotFound(ValueError):
    """An activation cannot point at an artifact row which does not exist."""


def _advisory_key(artifact_sha256: str) -> int:
    """A stable signed 64-bit PostgreSQL advisory-lock key for one artifact hash.

    ``pg_advisory_xact_lock`` takes a ``bigint``, so the first sixteen hex
    digits of the digest are folded into the signed range.  Collisions between
    two unrelated hashes are possible and harmless: the worst case is that two
    artifacts serialise against each other for the length of one transaction.
    """
    digest = artifact_sha256.removeprefix("sha256:")
    value = int(digest[:16], 16)
    return value - (1 << 64) if value >= (1 << 63) else value


class PlaybookArtifactQueryMixin:
    @asynccontextmanager
    async def artifact_hash_lock(
        self, artifact_shas: Sequence[str]
    ) -> AsyncIterator[AsyncConnection]:
        """A write transaction that also excludes concurrent work on ``artifact_shas``.

        The retention sweep and the compile-to-store handoff both act on one
        hash from two sides — the sweep deletes the row and then removes the
        file, the handoff adopts the file and then writes the row — so the
        window between each pair is exactly the TOCTOU that leaves a live row
        pointing at a deleted file.  Both sides take this lock, which makes
        the two critical sections serialise instead of interleave.

        On SQLite ``immediate()`` is already a database-wide write lock, so
        the per-hash locks are implicit and nothing further is issued.  On
        PostgreSQL ``immediate()`` is an ordinary read-committed transaction
        and the exclusion has to be asked for: ``pg_advisory_xact_lock`` is
        taken per hash, in sorted key order so two sweeps holding overlapping
        candidate sets queue rather than deadlock, and released by the commit.
        """
        async with self.immediate() as conn:
            if conn.dialect.name == "postgresql":
                for key in sorted({_advisory_key(sha) for sha in artifact_shas if sha}):
                    await conn.execute(
                        text("SELECT pg_advisory_xact_lock(:key)"), {"key": key}
                    )
            yield conn

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
        conn: AsyncConnection | None = None,
    ) -> None:
        """Insert immutable artifact identity or refresh its mutable storage metadata.

        The content-addressed identity fields are retained once the artifact
        exists.  The local path, byte size, validation result, and profile
        fingerprint can change when the same canonical bytes are rediscovered
        or revalidated, so those fields are refreshed on subsequent calls.

        This is the moment a hash becomes *referenced*, so it is one of the two
        sides of :meth:`artifact_hash_lock` (§12.1).  Holding the lock across
        the write means a retention sweep cannot be part-way through removing
        the file for this hash while the row lands, and the file check below
        cannot be answered with a state the sweep is about to change.

        The check itself is the repair half of the sweep's two-phase deletion:
        ``ArtifactStore.put`` adopts an existing file rather than rewriting it,
        so a sweep that entombed that file between the adoption and this call
        would otherwise leave the new row pointing at nothing.  Restoring the
        tombstone puts the same bytes back under the same name.  A path with no
        file and no tombstone is left alone — an artifact whose file was never
        written is the ``file_missing`` fault ``playbooks.artifact_integrity``
        exists to report, not something this write invents an answer for.

        When *conn* is supplied, its caller already owns the transaction and
        per-hash lock.  Reviewed-artifact import uses that form to span the
        filesystem write and row upsert with one compensatable critical
        section; ordinary callers omit it and retain the original behaviour.
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
        if conn is not None:
            await self._upsert_playbook_artifact(conn, values, path=path)
            return
        async with self.artifact_hash_lock([ref.artifact_sha256]) as locked_conn:
            await self._upsert_playbook_artifact(locked_conn, values, path=path)

    @staticmethod
    async def _upsert_playbook_artifact(
        conn: AsyncConnection, values: dict, *, path: str
    ) -> None:
        """Write one artifact row on a caller-owned transaction.

        The public method normally owns the per-hash lock.  Reviewed-artifact
        import holds that same lock across ``ArtifactStore.put`` and this
        statement so a failed row write can compensate the new file before a
        concurrent retention/import operation observes it.
        """
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
        if path and restore_any(Path(path)):
            logger.warning(
                "Artifact %s was re-adopted while retention was removing it; restored %s "
                "from its tombstone",
                values["artifact_sha256"],
                path,
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

    async def get_playbook_artifact_row(
        self, artifact_sha256: str, *, conn: AsyncConnection | None = None
    ) -> dict | None:
        """The whole artifact row, or ``None`` when the hash is unknown.

        ``get_playbook_artifact`` projects the immutable identity into an
        :class:`ArtifactRef` and drops everything else.  The activation-health
        read path needs the row's *mutable* metadata too — ``path``,
        ``validation`` and the ``profile_fingerprint`` the artifact was
        compiled against — so it reads the row rather than issuing three
        single-column queries for one activation.
        """
        statement = select(playbook_artifacts).where(
            playbook_artifacts.c.artifact_sha256 == artifact_sha256
        )
        if conn is not None:
            row = (await conn.execute(statement)).mappings().fetchone()
            return dict(row) if row else None
        async with self._engine.connect() as owned_conn:
            row = (await owned_conn.execute(statement)).mappings().fetchone()
        return dict(row) if row else None

    async def list_playbook_artifacts(self, playbook_id: str, *, limit: int = 50) -> list[dict]:
        """Every stored artifact for one playbook, newest version first.

        The activation chooser's read: an operator reviewing a newly compiled
        artifact needs the *inactive* candidates, which no other read returns —
        ``list_playbook_activations`` only ever names the one active hash per
        scope.  Ordered by ``version`` (monotonic per playbook) and then by
        ``created_at``, so two artifacts that share a version — the store does
        not enforce uniqueness there — still come back newest first rather than
        in insertion order.
        """
        stmt = (
            select(playbook_artifacts)
            .where(playbook_artifacts.c.playbook_id == playbook_id)
            .order_by(
                playbook_artifacts.c.version.desc(),
                playbook_artifacts.c.created_at.desc(),
            )
            .limit(limit)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).mappings().fetchall()
        return [dict(row) for row in rows]

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

    async def list_playbook_activations_with_artifacts(
        self, *, enabled_only: bool = False
    ) -> list[dict]:
        """Every activation row joined to the artifact it activates.

        The activation table stores a *reference* — ``active_artifact_sha256``
        — and nothing else about the artifact.  Every reporting surface that
        has to answer "which bytes are live, and what were they compiled
        from?" therefore needs this join, and reading the activation row alone
        yields ``None`` for hashes that are genuinely recorded (the release
        check and the cutover report both did exactly that until §5.5's
        evidence was joined here).

        A ``LEFT`` join, deliberately: an activation whose artifact row is
        missing is the ``unavailable`` fault the health surface reports, and
        dropping it here would hide it from the reports that exist to name it.
        Such a row comes back with the activation's own columns and ``None``
        for every artifact column.

        ``playbook_id``, ``scope`` and ``scope_identifier`` exist on both
        tables and are **not** taken from the artifact: the activation's values
        are the authority for where the playbook is installed.
        """
        artifact_columns = [
            playbook_artifacts.c[name] for name in _ACTIVATION_ARTIFACT_COLUMNS
        ]
        stmt = (
            select(playbook_activations, *artifact_columns)
            .select_from(
                playbook_activations.outerjoin(
                    playbook_artifacts,
                    playbook_activations.c.active_artifact_sha256
                    == playbook_artifacts.c.artifact_sha256,
                )
            )
            .order_by(playbook_activations.c.updated_at.desc())
        )
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

    async def filter_referenced_artifact_shas(
        self, shas: Sequence[str], *, conn: AsyncConnection | None = None
    ) -> set[str]:
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

        ``conn`` lets a caller run the question inside a transaction it already
        holds.  The sweep needs exactly that: its answer is only worth acting
        on while :meth:`artifact_hash_lock` is held, and a fresh connection
        would read outside that lock and could be stale before the first file
        is touched.  Without ``conn`` this opens its own read connection, which
        is what the orphan scan wants.
        """
        wanted = [sha for sha in dict.fromkeys(shas) if sha]
        if not wanted:
            return set()
        if conn is not None:
            return await self._referenced_artifact_shas(conn, wanted)
        async with self._engine.connect() as connection:
            return await self._referenced_artifact_shas(connection, wanted)

    @staticmethod
    async def _referenced_artifact_shas(
        conn: AsyncConnection, wanted: list[str]
    ) -> set[str]:
        referenced: set[str] = set()
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

        §12.1's protections, all of them explicit queries rather than
        foreign keys (§7.4) — the FKs are ``RESTRICT``, which would turn a
        mistake here into an ``IntegrityError`` rather than data loss, but
        relying on that would make the sweep's outcome depend on whether
        SQLite happens to be enforcing FKs on this connection:

        1. referenced by an activation (``active_artifact_sha256``);
        2. referenced by any retained run (``playbook_v2_runs.artifact_sha256``);
        3. pinned by a protected pending integration destination or an
           undelivered outbox destination manifest;
        4. among the newest ``min_versions`` artifacts of its own playbook,
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
            pinned_by_pending = select(playbook_pending_events.c.artifact_sha256).where(
                playbook_pending_events.c.resolved_at.is_(None),
                playbook_pending_events.c.artifact_sha256.is_not(None),
            )
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
                            playbook_artifacts.c.artifact_sha256.not_in(
                                pinned_by_pending.scalar_subquery()
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
            manifests = (
                (
                    await conn.execute(
                        select(integration_outbox.c.destination_manifest).where(
                            integration_outbox.c.delivered_at.is_(None),
                            integration_outbox.c.destination_manifest.is_not(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
            for manifest in manifests:
                for destination in manifest or ():
                    artifact_sha256 = destination.get("artifact_sha256")
                    if artifact_sha256:
                        protected.add(artifact_sha256)
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
