"""Permanent per-source writer fences, independent of rollout flags.

Adapters must call this before legacy writes/watchers, and route guarded writes
through CommandHandler. Import and notes use the same transaction lock so a
legacy write cannot race ownership cutover. No external plugin is initialized.
"""

from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import select, text

from src.database.tables import record_legacy_mappings
from src.knowledge.deprecation import record_usage_on
from src.records.models import RecordError


async def source_lock(conn, identity):
    key = int.from_bytes(
        hashlib.sha256(str(tuple(identity)).encode()).digest()[:8], "big", signed=True
    )
    await conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def migrated_result(mapping):
    return {
        "success": False,
        "error_code": "memory.migrated_read_only",
        "error": "Source is managed; use knowledge update with an expected revision and key",
        "record_id": str(mapping["record_id"]),
        "revision_id": str(mapping["latest_revision_id"]),
        "canonical_command": "knowledge_update",
    }


class CompatibilityFence:
    def __init__(self, db):
        self.db = db

    async def lookup_on(
        self, conn, *, source_installation_id, source_kind, source_scope, source_key
    ):
        identity = (source_installation_id, source_kind, source_scope, source_key)
        await source_lock(conn, identity)
        return (
            (
                await conn.execute(
                    select(record_legacy_mappings).where(
                        *[
                            record_legacy_mappings.c[key] == value
                            for key, value in zip(
                                (
                                    "source_installation_id",
                                    "source_kind",
                                    "source_scope",
                                    "source_key",
                                ),
                                identity,
                                strict=True,
                            )
                        ]
                    )
                )
            )
            .mappings()
            .first()
        )

    async def read(self, execute, **identity):
        async with self.db.immediate() as conn:
            mapping = await self.lookup_on(conn, **identity)
            if mapping and mapping["ownership"] == "managed":
                await record_usage_on(
                    conn, scope_key=identity["source_scope"], operation="read",
                    outcome="canonical_read",
                )
        if not mapping or mapping["ownership"] != "managed":
            return None
        project = identity["source_scope"]
        args = {"identity": f"record:{mapping['record_id']}"}
        args.update(
            {"global_scope": True}
            if project == "global"
            else {"project_id": project.removeprefix("project:")}
        )
        result = await execute("knowledge_show", args)
        if result.get("success"):
            result = {
                **result,
                "deprecation": {"code": "memory.migrated", "canonical_command": "knowledge_show"},
            }
        return result

    async def write(
        self,
        execute,
        *,
        if_revision=None,
        idempotency_key=None,
        patch=None,
        claim_epoch=None,
        **identity,
    ):
        async with self.db.immediate() as conn:
            mapping = await self.lookup_on(conn, **identity)
            if mapping and mapping["ownership"] == "managed":
                await record_usage_on(
                    conn, scope_key=identity["source_scope"], operation="write",
                    outcome=("guarded_write" if if_revision and idempotency_key
                             and isinstance(patch, dict) else "refused_write"),
                )
        if not mapping or mapping["ownership"] != "managed":
            return None
        if not if_revision or not idempotency_key or not isinstance(patch, dict):
            return migrated_result(mapping)
        # Callers cannot smuggle another record/scope/token into the translation.
        from src.knowledge.models import EDIT_FIELDS

        if set(patch) - EDIT_FIELDS:
            raise RecordError("record.invalid_input")
        args = {
            **patch,
            "identity": f"record:{mapping['record_id']}",
            "if_revision": if_revision,
            "idempotency_key": idempotency_key,
            "claim_epoch": claim_epoch,
        }
        scope = identity["source_scope"]
        args.update(
            {"global_scope": True}
            if scope == "global"
            else {"project_id": scope.removeprefix("project:")}
        )
        return await execute("knowledge_update", args)

    @asynccontextmanager
    async def note_path(self, project_id, path, *, operation=None):
        """Hold the same source lock around legacy filesystem side effects.

        Physical paths are retained in mapping decision evidence at cutover.
        Keep the lexical path fenced after deletion; also fence symlink targets.
        Scope labels cannot bypass physical ownership. Canonical reads still
        authorize the caller's requested project through CommandHandler.
        """
        path = str(Path(path).parent.resolve() / Path(path).name)
        paths = {path}
        if Path(path).is_symlink():
            paths.add(str(Path(path).resolve()))
        async with self.db.immediate() as conn:
            # Path locks also cover a first-time mapping not yet visible in SQL.
            for target in sorted(paths):
                await source_lock(conn, ("path", target))
            mapping = (
                (
                    await conn.execute(
                        select(record_legacy_mappings).where(
                            record_legacy_mappings.c.ownership == "managed",
                            record_legacy_mappings.c.source_kind == "path",
                            record_legacy_mappings.c.source_key.in_(paths),
                        )
                    )
                )
                .mappings()
                .first()
            )
            if mapping and operation:
                await record_usage_on(
                    conn, scope_key=mapping["source_scope"], operation=operation,
                    outcome="canonical_read" if operation in {"read", "list"} else "refused_write",
                )
            yield mapping


def managed_export_path(path):
    """Legacy scanners/watchers must exclude the core export namespace."""
    return ("knowledge", "records") in tuple(zip(Path(path).parts, Path(path).parts[1:]))
