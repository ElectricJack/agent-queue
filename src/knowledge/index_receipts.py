"""Derived index receipts and the core-owned chunk manifest (K12).

A semantic index is rebuildable derived data. These receipts are its audit
trail, never its authority: every served revision is read again through
``KnowledgeService``, and a receipt cannot create, hide or replace a revision.

The chunk manifest is validated here rather than in a provider, because a
manifest that carried text would be an uncontrolled excerpt channel. A provider
declares a contiguous, text-free partition of the exact revision body and core
binds the digest to ``record_id``/``revision_id``/``content_sha256``/
``hash_version``. Overlapping or lossy windows are refused: they cannot be
verified without excerpts.

Writes require core enablement for the scope, ``knowledge.semantic.enabled``,
the memory master flag and a registered, non-deprecated provider. A stale
acknowledgment can never move a checkpoint backwards, and redaction is one-way:
a redacted revision can be erased but never re-indexed.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.database.tables import (
    knowledge_records,
    knowledge_revision_payloads,
    knowledge_revisions,
    record_index_state,
    records,
)
from src.records.models import RecordError, uuid_value

#: Provider receipts live in this table only.
INDEX_RECEIPT_TABLE_NAMES = ("record_index_state",)

MAX_CHUNKS = 512
MAX_CHUNK_LABEL_BYTES = 128

MANIFEST_FIELDS = frozenset(
    {"record_id", "revision_id", "content_sha256", "hash_version", "chunks"}
)
CHUNK_FIELDS = frozenset({"chunk_id", "ordinal", "char_start", "char_end"})


def canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def manifest_digest(manifest: dict) -> str:
    """The core-computed digest a provider may never supply itself."""

    return hashlib.sha256(canonical_json(manifest).encode("utf-8")).hexdigest()


def _label(value) -> str:
    if not isinstance(value, str) or value != value.strip() or not value.isprintable():
        raise RecordError("record.invalid_input", "Invalid chunk label")
    if len(value.encode("utf-8")) > MAX_CHUNK_LABEL_BYTES:
        raise RecordError("record.invalid_input", "Chunk label is too long")
    return value


def _offset(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RecordError("record.invalid_input", "Chunk offsets must be integers")
    return value


def validate_manifest(manifest, *, record_id, revision_id, content_sha256, body_length) -> dict:
    """Validate a provider manifest and return its canonical form.

    Raises ``record.invalid_input`` for extra fields, text, duplicates,
    unordered ordinals, gaps, overlaps or a partition that does not cover the
    exact revision body.
    """

    if not isinstance(manifest, dict) or set(manifest) != MANIFEST_FIELDS:
        raise RecordError("record.invalid_input", "Manifest fields are not exact")
    if str(uuid_value(manifest["record_id"], "record_id")) != str(record_id):
        raise RecordError("record.invalid_input", "Manifest names another record")
    if str(uuid_value(manifest["revision_id"], "revision_id")) != str(revision_id):
        raise RecordError("record.invalid_input", "Manifest names another revision")
    if manifest["content_sha256"] != content_sha256:
        raise RecordError("record.invalid_input", "Manifest is bound to other bytes")
    if manifest["hash_version"] != 1:
        raise RecordError("record.invalid_input", "Unsupported manifest hash version")
    chunks = manifest["chunks"]
    if not isinstance(chunks, list):
        raise RecordError("record.invalid_input", "Manifest chunks must be a list")
    if len(chunks) > MAX_CHUNKS or (chunks and body_length == 0):
        raise RecordError("record.invalid_input", "Manifest chunk count is not usable")
    if not chunks and body_length:
        raise RecordError("record.invalid_input", "Manifest omits the revision body")
    normalized, seen, cursor = [], set(), 0
    for ordinal, chunk in enumerate(chunks):
        if not isinstance(chunk, dict) or set(chunk) != CHUNK_FIELDS:
            raise RecordError("record.invalid_input", "Chunk fields are not exact")
        chunk_id = _label(chunk["chunk_id"])
        if chunk_id in seen:
            raise RecordError("record.invalid_input", "Duplicate chunk identity")
        seen.add(chunk_id)
        if chunk["ordinal"] != ordinal:
            raise RecordError("record.invalid_input", "Chunk ordinals must be contiguous")
        start, end = _offset(chunk["char_start"]), _offset(chunk["char_end"])
        if start != cursor or end <= start or end > body_length:
            raise RecordError("record.invalid_input", "Chunks must partition the exact body")
        cursor = end
        normalized.append(
            {"char_end": end, "char_start": start, "chunk_id": chunk_id, "ordinal": ordinal}
        )
    if cursor != body_length:
        raise RecordError("record.invalid_input", "Chunks must partition the exact body")
    return {
        "chunks": normalized,
        "content_sha256": content_sha256,
        "hash_version": 1,
        "record_id": str(record_id),
        "revision_id": str(revision_id),
    }


class DerivedIndexReceipts:
    """Core-owned receipts for one optional provider's derived index.

    ``registry`` is a :class:`src.knowledge.registration.RetrievalProviderRegistry`
    (or anything with ``require``); it is consulted locally, never initialized.
    ``service`` supplies authorization and the exact revision read.
    """

    def __init__(self, service, registry, memory_config, *, clock=None):
        self.service = service
        self.registry = registry
        self.memory_config = memory_config
        self.clock = clock or (lambda: datetime.now(UTC))

    def _now(self):
        return self.clock()

    def _enabled(self, access) -> None:
        cfg = self.service.config
        if not (
            cfg.enabled is True
            and cfg.semantic.enabled is True
            and self.memory_config.enabled is True
        ):
            raise RecordError("knowledge.disabled", "Optional semantic index is not enabled")

    async def hydrate_index_payload(
        self, *, record_id, revision_id, principal, project_id
    ) -> dict:
        """Server-authorized payload for chunking one exact revision.

        The values come from the ordinary authorized read path, never from a
        caller-named collection, path or record: no arbitrary legacy store is
        queried and no other record's bytes are reachable from here.
        """

        async def read(conn):
            self._enabled(await self.service._access(conn, principal, "knowledge_show", project_id))
            shown = await self.service.show_on(
                f"record:{record_id}",
                revision_id=str(revision_id),
                principal=principal,
                project_id=project_id,
                conn=conn,
            )
            body = shown["snapshot"]["body"]
            return {
                "record_id": shown["record_id"],
                "revision_id": shown["revision_id"],
                "sequence": shown["sequence"],
                "content_sha256": shown["content_sha256"],
                "hash_version": shown["hash_version"],
                "scope_key": shown["scope_key"],
                "title": shown["snapshot"]["title"],
                "body": body,
                "body_length": len(body),
            }

        return await self.service._transaction(read)

    async def acknowledge(
        self, *, provider_id, manifest, principal, project_id, record_id=None, revision_id=None
    ) -> dict:
        """Record one provider's exact-revision index receipt.

        The provider's own digest is ignored; core computes it. A manifest for a
        superseded revision is still accepted, because reindexing history is
        derived work. A redacted revision cannot be acknowledged at all: the
        authorized read it needs has already been permanently withdrawn, and only
        :meth:`acknowledge_erasure` remains open for it.
        """

        entry = self.registry.require(provider_id)

        async def write(conn):
            access = await self.service._access(
                conn, principal, "knowledge_show", project_id
            )
            self._enabled(access)
            source = manifest if isinstance(manifest, dict) else {}
            shown = await self.service.show_on(
                f"record:{record_id or source.get('record_id')}",
                revision_id=str(revision_id if revision_id is not None else source.get("revision_id")),
                principal=principal,
                project_id=project_id,
                conn=conn,
            )
            record_uuid = uuid_value(shown["record_id"], "record_id")
            revision_uuid = uuid_value(shown["revision_id"], "revision_id")
            canonical = validate_manifest(
                manifest,
                record_id=record_uuid,
                revision_id=revision_uuid,
                content_sha256=shown["content_sha256"],
                body_length=len(shown["snapshot"]["body"]),
            )
            digest = manifest_digest(canonical)
            now = self._now()
            existing = (
                (
                    await conn.execute(
                        select(record_index_state)
                        .where(
                            record_index_state.c.provider_id == entry.provider_id,
                            record_index_state.c.record_id == record_uuid,
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if existing is not None and existing["sequence"] >= shown["sequence"]:
                raise RecordError("knowledge.index_stale", "Stale index acknowledgment")
            await conn.execute(
                pg_insert(record_index_state)
                .values(
                    provider_id=entry.provider_id,
                    record_id=record_uuid,
                    revision_id=revision_uuid,
                    sequence=shown["sequence"],
                    chunk_manifest_sha256=digest,
                    indexed_at=now,
                    redacted_at=None,
                )
                .on_conflict_do_update(
                    constraint="pk_record_index_state",
                    set_={
                        "revision_id": revision_uuid,
                        "sequence": shown["sequence"],
                        "chunk_manifest_sha256": digest,
                        "indexed_at": now,
                        "redacted_at": None,
                    },
                    where=record_index_state.c.sequence < shown["sequence"],
                )
            )
            return {
                "success": True,
                "provider_id": entry.provider_id,
                "record_id": str(record_uuid),
                "revision_id": str(revision_uuid),
                "sequence": shown["sequence"],
                "chunk_manifest_sha256": digest,
                "chunk_count": len(canonical["chunks"]),
            }

        return await self.service._transaction(write)

    async def acknowledge_erasure(
        self, *, provider_id, record_id, revision_id, principal, project_id
    ) -> dict:
        """Record that a provider dropped the derived chunks of a redacted revision."""

        entry = self.registry.require(provider_id)

        async def write(conn):
            access = await self.service._access(
                conn, principal, "knowledge_show", project_id
            )
            self._enabled(access)
            record_uuid = uuid_value(record_id, "record_id")
            revision_uuid = uuid_value(revision_id, "revision_id")
            redacted = await conn.scalar(
                select(knowledge_revisions.c.revision_id)
                .select_from(
                    knowledge_revisions.join(
                        knowledge_revision_payloads,
                        knowledge_revision_payloads.c.revision_id
                        == knowledge_revisions.c.revision_id,
                    )
                )
                .where(
                    knowledge_revisions.c.revision_id == revision_uuid,
                    knowledge_revisions.c.record_id == record_uuid,
                    knowledge_revision_payloads.c.snapshot.is_(None),
                )
            )
            if redacted is None:
                raise RecordError("knowledge.index_not_redacted")
            now = self._now()
            result = await conn.execute(
                update(record_index_state)
                .where(
                    record_index_state.c.provider_id == entry.provider_id,
                    record_index_state.c.record_id == record_uuid,
                )
                .values(redacted_at=now)
                .returning(record_index_state.c.record_id)
            )
            acknowledged = result.first() is not None
            return {
                "success": True,
                "provider_id": entry.provider_id,
                "record_id": str(record_uuid),
                "revision_id": str(revision_uuid),
                "state": "erased" if acknowledged else "never_indexed",
            }

        return await self.service._transaction(write)

    async def lag(self, *, provider_id, limit=100) -> dict:
        """Authorized-scope revisions with no current receipt for this provider.

        Derived lag never blocks a read: the lexical path serves an unindexed
        revision today and the provider is free to catch up.
        """

        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise RecordError("record.invalid_input", "limit must be between 1 and 100")
        entry = self.registry.require(provider_id)
        async with self.service.db.immediate() as conn:
            rows = (
                (
                    await conn.execute(
                        select(
                            records.c.record_id,
                            knowledge_records.c.current_revision_id,
                            records.c.scope_key,
                        )
                        .select_from(
                            records.join(
                                knowledge_records,
                                knowledge_records.c.record_id == records.c.record_id,
                            )
                            .outerjoin(
                                record_index_state,
                                (record_index_state.c.provider_id == entry.provider_id)
                                & (record_index_state.c.record_id == records.c.record_id),
                            )
                            .join(
                                knowledge_revision_payloads,
                                knowledge_revision_payloads.c.revision_id
                                == knowledge_records.c.current_revision_id,
                            )
                        )
                        .where(
                            records.c.kind == "knowledge",
                            knowledge_revision_payloads.c.snapshot.is_not(None),
                            or_(
                                record_index_state.c.record_id.is_(None),
                                record_index_state.c.revision_id
                                != knowledge_records.c.current_revision_id,
                            ),
                        )
                        .order_by(records.c.record_id)
                        .limit(limit)
                    )
                )
                .mappings()
                .all()
            )
        return {
            "success": True,
            "provider_id": entry.provider_id,
            "pending": [
                {
                    "record_id": str(row["record_id"]),
                    "revision_id": str(row["current_revision_id"]),
                    "scope_key": row["scope_key"],
                }
                for row in rows
            ],
            "truncated": len(rows) == limit,
        }

    async def pending_erasures(self, *, provider_id) -> dict:
        """Redacted revisions whose derived chunks are still acknowledged as indexed."""

        entry = self.registry.require(provider_id)
        async with self.service.db.immediate() as conn:
            rows = (
                (
                    await conn.execute(
                        select(record_index_state.c.record_id, record_index_state.c.revision_id)
                        .select_from(
                            record_index_state.join(
                                knowledge_revision_payloads,
                                knowledge_revision_payloads.c.revision_id
                                == record_index_state.c.revision_id,
                            )
                        )
                        .where(
                            record_index_state.c.provider_id == entry.provider_id,
                            record_index_state.c.redacted_at.is_(None),
                            knowledge_revision_payloads.c.snapshot.is_(None),
                        )
                        .order_by(record_index_state.c.record_id)
                        .limit(100)
                    )
                )
                .mappings()
                .all()
            )
        return {
            "success": True,
            "provider_id": entry.provider_id,
            "pending": [
                {"record_id": str(row["record_id"]), "revision_id": str(row["revision_id"])}
                for row in rows
            ],
            "truncated": len(rows) == 100,
        }


async def index_state_for_provider(db, provider_id: str) -> list[dict]:
    """Direct receipt read for doctor/diagnostics; never used to serve a read."""

    async with db.immediate() as conn:
        rows = (
            (
                await conn.execute(
                    select(record_index_state)
                    .where(record_index_state.c.provider_id == provider_id)
                    .order_by(record_index_state.c.record_id)
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


async def indexed_provider_ids(db) -> tuple[str, ...]:
    """Provider ids that hold at least one receipt.

    Read from the receipts themselves, so doctor never needs the plugin registry
    and never initializes an optional provider.
    """

    async with db.immediate() as conn:
        rows = await conn.execute(select(record_index_state.c.provider_id).distinct())
    return tuple(sorted({row[0] for row in rows}))


async def provider_lag_counts(db, provider_ids) -> dict:
    """Per-provider receipt lag and unacknowledged erasures, for ``doctor``.

    Empty when no provider is registered: an absent optional provider is not a
    finding, because nothing derived exists to be behind.
    """

    counts = {}
    if not provider_ids:
        return counts
    async with db.immediate() as conn:
        for provider_id in provider_ids:
            pending = await conn.scalar(
                select(func.count())
                .select_from(
                    knowledge_records.outerjoin(
                        record_index_state,
                        (record_index_state.c.provider_id == provider_id)
                        & (record_index_state.c.record_id == knowledge_records.c.record_id),
                    ).join(
                        knowledge_revision_payloads,
                        knowledge_revision_payloads.c.revision_id
                        == knowledge_records.c.current_revision_id,
                    )
                )
                .where(
                    knowledge_revision_payloads.c.snapshot.is_not(None),
                    or_(
                        record_index_state.c.record_id.is_(None),
                        record_index_state.c.revision_id
                        != knowledge_records.c.current_revision_id,
                    ),
                )
            )
            erasures = await conn.scalar(
                select(func.count())
                .select_from(
                    record_index_state.join(
                        knowledge_revision_payloads,
                        knowledge_revision_payloads.c.revision_id
                        == record_index_state.c.revision_id,
                    )
                )
                .where(
                    record_index_state.c.provider_id == provider_id,
                    record_index_state.c.redacted_at.is_(None),
                    knowledge_revision_payloads.c.snapshot.is_(None),
                )
            )
            counts[provider_id] = {
                "lag": int(pending or 0),
                "erasure_pending": int(erasures or 0),
            }
    return counts
