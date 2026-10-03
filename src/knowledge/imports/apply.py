"""Explicit snapshot-only imports with bounded, resumable transactions.

All mutations are composed by knowledge_import in CommandHandler. Artifacts
are immutable confined files; manifest/selection/base tokens/backup evidence
are pinned before the first revision. Receipts, head advancement and source
cutover commit together. No live legacy read or provider call occurs here.
"""

from __future__ import annotations

import asyncio
import base64
import os
import stat
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.commands.principal import PrincipalKind
from src.database.tables import record_import_items, record_import_runs, record_legacy_mappings
from src.database.tables import record_source_artifacts
from src.knowledge.imports.compatibility import source_lock
from src.knowledge.imports.manifest import canonical_json, sha256, verify_manifest
from src.knowledge.models import normalize_snapshot
from src.knowledge.service import KnowledgeService
from src.records.identity import knowledge_identity
from src.records.auth import GLOBAL_SCOPE
from src.records.models import RecordError, uuid_value

IDENTITY_FIELDS = ("source_installation_id", "source_kind", "source_scope", "source_key")
OUTCOMES = ("created", "revised", "reused", "excluded", "quarantined", "failed")


def source_hash_key(identity):
    """Stable key for a caller-pinned hash of one complete mapping identity."""
    return sha256(canonical_json(tuple(identity)))


class ImportService(KnowledgeService):
    def __init__(self, db, config, vault_root):
        super().__init__(db, config)
        self.vault_root = vault_root

    async def _gate(self, conn, principal, project_id):
        if principal is None or principal.kind != PrincipalKind.LOCAL:
            raise RecordError("knowledge_import.forbidden", "Requires the local operator")
        if not self.config.import_apply.enabled:
            raise RecordError("knowledge_import.disabled", "Enable knowledge.import_apply")
        return await self._access(conn, principal, "knowledge_import", project_id, write=True)

    async def _artifact(self, conn, access, content, *, private_manifest=False):
        digest = sha256(content)
        aid = uuid5(NAMESPACE_URL, f"aq-artifact:{access.scope_key}:{digest}")
        await source_lock(conn, ("artifact", str(aid)))
        row = (
            (
                await conn.execute(
                    select(record_source_artifacts).where(
                        record_source_artifacts.c.scope_key == access.scope_key,
                        record_source_artifacts.c.content_sha256 == digest,
                    )
                )
            )
            .mappings()
            .first()
        )
        if row and row["redacted_at"]:
            raise RecordError("record.revision_redacted")
        storage_key = row["storage_key"] if row else f"record-artifacts/{aid}"
        await asyncio.to_thread(self._artifact_io, storage_key, content)
        if not row:
            await conn.execute(
                insert(record_source_artifacts).values(
                    artifact_id=aid,
                    scope_key=access.scope_key,
                    content_sha256=digest,
                    byte_size=len(content),
                    media_type="application/vnd.aq.import-manifest"
                    if private_manifest
                    else "application/octet-stream",
                    storage_key=storage_key,
                )
            )
        return {
            "source_id": digest,
            "kind": "artifact",
            "artifact_id": str(row["artifact_id"] if row else aid),
            "sha256": digest,
        }

    def _artifact_io(self, key, content=None):
        from src.records.export import _directory

        parts = Path(key).parts
        if len(parts) != 2 or parts[0] != "record-artifacts":
            raise RecordError("record.artifact_path")
        uuid_value(parts[1], "artifact_id")
        fd = _directory(self.vault_root, parts[:-1], create=content is not None)
        try:
            if content is not None:
                temporary = f".import-{uuid4()}"
                out = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o400,
                    dir_fd=fd,
                )
                try:
                    with os.fdopen(out, "wb") as stream:
                        stream.write(content)
                        stream.flush()
                        os.fsync(stream.fileno())
                    try:
                        os.link(
                            temporary,
                            parts[-1],
                            src_dir_fd=fd,
                            dst_dir_fd=fd,
                            follow_symlinks=False,
                        )
                    except FileExistsError:
                        pass
                    os.fsync(fd)
                finally:
                    os.unlink(temporary, dir_fd=fd)
            source = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            with os.fdopen(source, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise RecordError("record.artifact_path")
                raw = stream.read(70 * 1024 * 1024 + 1)
            if content is not None and raw != content:
                raise RecordError("record.hash_divergence")
            return raw
        finally:
            os.close(fd)

    async def apply(
        self,
        *,
        principal,
        project_id,
        manifest_content_base64,
        manifest_sha256,
        selected_item_ids,
        idempotency_key,
        backup_receipt,
        expected_revisions=None,
        expected_source_hashes=None,
        limit=100,
    ):
        if principal is None or principal.kind != PrincipalKind.LOCAL:
            raise RecordError("knowledge_import.forbidden")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise RecordError("knowledge_import.invalid_input", "Batch limit must be 1-100")
        try:
            raw = base64.b64decode(manifest_content_base64, validate=True)
            if len(raw) > 70 * 1024 * 1024:
                raise ValueError("manifest too large")
            document = verify_manifest(raw, manifest_sha256)
            selected = sorted(set(selected_item_ids))
            if not selected or len(selected) != len(selected_item_ids):
                raise ValueError("Select unique item IDs explicitly")
            if set(selected) - {item["item_key"] for item in document["items"]}:
                raise ValueError("Selected item is absent from sealed manifest")
            if not isinstance(backup_receipt, str) or not backup_receipt.strip():
                raise ValueError("Explicit backup receipt required")
            if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128:
                raise ValueError("Import key must contain 1-128 characters")
            if expected_revisions is not None:
                if not isinstance(expected_revisions, dict):
                    raise ValueError("Expected revisions must be an object")
                for token in expected_revisions.values():
                    uuid_value(token, "expected_revision")
            if expected_source_hashes is not None:
                if not isinstance(expected_source_hashes, dict):
                    raise ValueError("Expected source hashes must be an object")
                for key, digest in expected_source_hashes.items():
                    if (
                        len(key) != 64
                        or len(digest) != 64
                        or any(c not in "0123456789abcdef" for c in key + digest)
                    ):
                        raise ValueError("Source keys and old hashes must be SHA-256")
        except (ValueError, TypeError, KeyError) as exc:
            raise RecordError("knowledge_import.invalid_input", str(exc)) from exc
        run_id = uuid5(NAMESPACE_URL, f"aq-import:{project_id}:{idempotency_key}")
        selection = {
            "selected": selected,
            "expected_revisions": expected_revisions or {},
            "expected_source_hashes": expected_source_hashes or {},
            "backup_receipt": backup_receipt,
            "manifest_sha256": manifest_sha256,
        }

        async def prepare(conn):
            access = await self._gate(conn, principal, project_id)
            await self.db.ensure_record_scope_on(
                project_id=None if project_id is GLOBAL_SCOPE else project_id,
                conn=conn,
            )
            await source_lock(conn, ("run", str(run_id)))
            row = (
                (
                    await conn.execute(
                        select(record_import_runs)
                        .where(
                            record_import_runs.c.run_id == run_id,
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if row:
                if row["scope_key"] != access.scope_key or row["cursor"]["selection"] != selection:
                    raise RecordError("record.idempotency_conflict")
                return
            # Source labels confer no authority. Every selected candidate must
            # belong to the explicitly authorized target scope.
            for item in document["items"]:
                if item["item_key"] in selected and item["disposition"] == "candidate":
                    if any(s["source_scope"] != access.scope_key for s in item["sources"]):
                        raise RecordError("record.forbidden", "Import scope mismatch")
            artifact = await self._artifact(conn, access, raw, private_manifest=True)
            await conn.execute(
                insert(record_import_runs).values(
                    run_id=run_id,
                    source_installation_id=document["source_installation_id"],
                    snapshot_id=document["snapshot_id"],
                    manifest_sha256=manifest_sha256,
                    scope_key=access.scope_key,
                    state="prepared",
                    cursor={"selection": selection, "manifest": artifact, "offset": 0},
                    report={"items": {}},
                )
            )

        await self._transaction(prepare)
        return await self.resume(
            run_id=str(run_id),
            principal=principal,
            project_id=project_id,
            manifest_sha256=manifest_sha256,
            limit=limit,
        )

    async def resume(
        self, *, run_id, principal, project_id, manifest_sha256, limit=100, cancel=False
    ):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise RecordError("knowledge_import.invalid_input", "Batch limit must be 1-100")
        run_id = uuid_value(run_id, "run_id")

        async def batch(conn):
            access = await self._gate(conn, principal, project_id)
            run = (
                (
                    await conn.execute(
                        select(record_import_runs)
                        .where(
                            record_import_runs.c.run_id == run_id,
                            record_import_runs.c.scope_key == access.scope_key,
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if not run:
                raise RecordError("record.not_found")
            if run["manifest_sha256"] != manifest_sha256:
                raise RecordError("knowledge_import.manifest_changed")
            cursor, report = dict(run["cursor"]), dict(run["report"])
            report["items"] = dict(report["items"])
            manifest = cursor["manifest"]
            artifact = (
                (
                    await conn.execute(
                        select(record_source_artifacts).where(
                            record_source_artifacts.c.artifact_id == UUID(manifest["artifact_id"]),
                            record_source_artifacts.c.scope_key == access.scope_key,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if not artifact or artifact["redacted_at"]:
                raise RecordError("record.revision_redacted")
            raw = await asyncio.to_thread(self._artifact_io, artifact["storage_key"])
            document = verify_manifest(raw, manifest_sha256)
            selection = cursor["selection"]
            items = {i["item_key"]: i for i in document["items"]}
            pending = [key for key in selection["selected"] if key not in report["items"]]
            replay = not pending
            if not cancel:
                for key in pending[:limit]:
                    item = items[key]
                    try:
                        async with conn.begin_nested():
                            result = await self._apply_item(
                                conn,
                                access,
                                document,
                                item,
                                selection["expected_revisions"],
                                selection["expected_source_hashes"],
                            )
                    except RecordError as exc:
                        result = {"outcome": "failed", "error_code": exc.code}
                    await conn.execute(
                        insert(record_import_items).values(
                            run_id=run_id,
                            item_key=key,
                            source_sha256=item["source_sha256"],
                            disposition=item["disposition"],
                            record_id=result.get("record_id"),
                            revision_id=result.get("revision_id"),
                            error_code=result.get("error_code"),
                        )
                    )
                    report["items"][key] = result
            counts = Counter(value["outcome"] for value in report["items"].values())
            counts = {name: counts[name] for name in OUTCOMES}
            processed = sum(counts.values())
            total = len(selection["selected"])
            state = (
                "cancelled"
                if cancel
                else (
                    "applying"
                    if processed < total
                    else "failed"
                    if counts["failed"]
                    else "succeeded"
                )
            )
            report["counts"] = {
                "selected": total,
                "processed": processed,
                "pending": total - processed,
                **counts,
            }
            cursor["offset"] = processed
            await conn.execute(
                update(record_import_runs)
                .where(
                    record_import_runs.c.run_id == run_id,
                )
                .values(
                    state=state,
                    cursor=cursor,
                    report=report,
                    finished_at=datetime.now(UTC) if state in {"succeeded", "failed"} else None,
                )
            )
            receipt_counts = report["counts"]
            response_items = [{"item_key": key, **value} for key, value in report["items"].items()]
            if replay:
                response_items = [
                    {**i, "outcome": "reused"}
                    if i["outcome"] in {"created", "revised", "reused"}
                    else i
                    for i in response_items
                ]
                receipt_counts = {
                    **receipt_counts,
                    "created": 0,
                    "revised": 0,
                    "reused": sum(counts[k] for k in ("created", "revised", "reused")),
                }
            return {
                "success": True,
                "outcome": "replayed" if replay else "applied",
                "run_id": str(run_id),
                "state": state,
                "replay": replay,
                "manifest_sha256": manifest_sha256,
                "counts": receipt_counts,
                "items": response_items,
            }

        return await self._transaction(batch)

    async def _apply_item(self, conn, access, document, item, expected, expected_hashes):
        disposition = item["disposition"]
        if disposition != "candidate":
            for source in sorted(item["sources"], key=canonical_json):
                identity = (
                    document["source_installation_id"],
                    source["source_kind"],
                    source["source_scope"],
                    source["source_key"],
                )
                await source_lock(conn, identity)
                values = dict(zip(IDENTITY_FIELDS, identity, strict=True))
                values.update(
                    ownership="quarantined" if disposition == "unavailable" else disposition,
                    decision_reason=", ".join(item["issues"]) or disposition,
                )
                await conn.execute(
                    pg_insert(record_legacy_mappings).values(**values).on_conflict_do_nothing()
                )
            return {
                "outcome": "failed" if disposition == "unavailable" else disposition,
                "error_code": "knowledge_import.source_unavailable"
                if disposition == "unavailable"
                else None,
            }
        mappings = []
        identities = []
        # Lock paths before identities, in deterministic order. Notes holds
        # these through filesystem changes, preventing cutover races.
        paths = sorted(
            {
                str(Path(s["metadata"]["real_root"]) / s["metadata"]["relative_path"])
                for s in item["sources"]
                if "relative_path" in s["metadata"]
            }
        )
        for path in paths:
            await source_lock(conn, ("path", str(Path(path).parent.resolve() / Path(path).name)))
        for source in sorted(
            item["sources"], key=lambda s: (s["source_kind"], s["source_scope"], s["source_key"])
        ):
            identity = (
                document["source_installation_id"],
                source["source_kind"],
                source["source_scope"],
                source["source_key"],
            )
            await source_lock(conn, identity)
            where = [
                record_legacy_mappings.c[k] == v
                for k, v in zip(IDENTITY_FIELDS, identity, strict=True)
            ]
            mapping = (
                (await conn.execute(select(record_legacy_mappings).where(*where).with_for_update()))
                .mappings()
                .first()
            )
            if mapping:
                mappings.append(mapping)
            identities.append((identity, source, sha256(canonical_json(source))))
        owners = {m["record_id"] for m in mappings if m["record_id"]}
        # The same physical note cannot acquire a second owner under another
        # installation/source label. All path owners share the path lock.
        for path in paths:
            path = str(Path(path).parent.resolve() / Path(path).name)
            path_owners = set(
                (
                    await conn.execute(
                        select(record_legacy_mappings.c.record_id).where(
                            record_legacy_mappings.c.source_kind == "path",
                            record_legacy_mappings.c.source_key == path,
                            record_legacy_mappings.c.ownership == "managed",
                        )
                    )
                ).scalars()
            )
            if path_owners - owners:
                raise RecordError("knowledge_import.mapping_conflict")
        if len(owners) > 1:
            raise RecordError("knowledge_import.mapping_conflict")
        record = current = None
        if owners:
            record = await self.db.get_record_on(record_id=next(iter(owners)), conn=conn)
            if not record or record["scope_key"] != access.scope_key:
                raise RecordError("record.forbidden")
            record = await self._lock_source(record, conn=conn)
            current = await self._revision(record, conn=conn)
            old_hashes = {
                source_hash_key(tuple(m[k] for k in IDENTITY_FIELDS)): m["latest_source_sha256"]
                for m in mappings
                if m["record_id"]
            }
            if expected_hashes:
                if any(
                    expected_hashes[key] != digest
                    for key, digest in old_hashes.items()
                    if key in expected_hashes
                ):
                    raise RecordError("knowledge_import.source_conflict")
            unchanged = len(mappings) == len(identities) and all(
                m["latest_source_sha256"] == digest
                for (ident, _, digest) in identities
                for m in mappings
                if tuple(m[k] for k in IDENTITY_FIELDS) == ident
            )
            if unchanged:
                return {
                    "outcome": "reused",
                    "record_id": str(record["record_id"]),
                    "revision_id": str(current["revision_id"]),
                    "source_hashes": old_hashes,
                }
            base = expected.get(item["item_key"])
            if not base or set(old_hashes) - expected_hashes.keys():
                raise RecordError("record.precondition_required")
            if uuid_value(base, "expected_revision") != current["revision_id"]:
                raise RecordError("record.revision_conflict")
        elif any(m["ownership"] != "legacy" for m in mappings):
            raise RecordError("knowledge_import.mapping_conflict")
        artifacts = {a["sha256"]: a for a in document["artifacts"]}
        evidence = {}
        for _, source, _ in identities:
            digest = source["artifact_sha256"]
            if digest:
                # A vector export may include other projects. Its exact bytes
                # stay in the operator-only manifest, never as record evidence.
                # Retain this source's complete rows/metadata independently.
                raw = (
                    canonical_json(source)
                    if source["source_kind"] == "vector"
                    else base64.b64decode(artifacts[digest]["bytes_base64"], validate=True)
                )
                digest = sha256(raw)
                evidence[digest] = await self._artifact(conn, access, raw)
        snapshots = self._snapshots(item, document, list(evidence.values()))
        if record is None:
            record_id, alias = knowledge_identity()
            record = dict(
                record_id=record_id,
                kind="knowledge",
                scope_key=access.scope_key,
                knowledge_alias=alias,
                created_by=access.actor_key,
            )
            await self.db.insert_record_on(record, conn=conn)
        for snapshot in snapshots:
            result = await self._append_snapshot(
                record, current, snapshot, access, "knowledge_import", conn=conn
            )
            current = await self._revision(record, conn=conn)
        for identity, source, digest in identities:
            metadata = source["metadata"]
            reason = "Explicit sealed snapshot selection"
            if "relative_path" in metadata:
                path = Path(metadata["real_root"]) / metadata["relative_path"]
                reason = f"path:{path.parent.resolve() / path.name}"
            values = dict(zip(IDENTITY_FIELDS, identity, strict=True))
            values.update(
                record_id=record["record_id"],
                latest_source_sha256=digest,
                latest_revision_id=current["revision_id"],
                ownership="managed",
                decision_reason=reason,
                updated_at=datetime.now(UTC),
            )
            await conn.execute(
                pg_insert(record_legacy_mappings)
                .values(**values)
                .on_conflict_do_update(index_elements=list(IDENTITY_FIELDS), set_=values)
            )
            if reason.startswith("path:"):
                # Permanent physical alias: relocating a snapshot never releases
                # a previously managed path to a second write authority.
                path_values = {
                    **values,
                    "source_kind": "path",
                    "source_key": reason[5:],
                    "latest_source_sha256": source["artifact_sha256"],
                }
                await conn.execute(
                    pg_insert(record_legacy_mappings)
                    .values(**path_values)
                    .on_conflict_do_update(index_elements=list(IDENTITY_FIELDS), set_=path_values)
                )
        return {
            "outcome": "revised" if owners else "created",
            "record_id": result["record_id"],
            "revision_id": result["revision_id"],
            "source_hashes": {
                source_hash_key(identity): digest for identity, _, digest in identities
            },
        }

    def _snapshots(self, item, document, evidence):
        rows = [row for source in item["sources"] for row in source["metadata"].get("rows", [])]
        versions = [row for row in rows if row.get("entry_type") in {"kv", "temporal"}]
        if versions:
            versions.sort(key=lambda r: (r.get("valid_from") or "", canonical_json(r)))
        else:
            versions = [None]
        result = []
        for version in versions:
            body = item["original"] if version is None else version.get("value")
            if not isinstance(body, str):
                raise RecordError("knowledge_import.invalid_input", "Retained original required")
            title = item["sources"][0]["source_key"][-240:]
            result.append(
                normalize_snapshot(
                    {
                        "title": title,
                        "body": body,
                        "category": "fact" if version else "note",
                        "summary": item["summary"] if version is None else None,
                        "valid_from": version.get("valid_from") if version else None,
                        "valid_until": version.get("valid_until") if version else None,
                        "sources": evidence,
                        "metadata": {
                            "import.snapshot_id": document["snapshot_id"],
                            "import.item_key": item["item_key"],
                            "import.source_sha256": item["source_sha256"],
                            "import.source_timestamp": document["snapshot_timestamp"],
                        },
                    }
                )
            )
        return result
