"""Permanent redaction tombstones and durable purge work.

Redaction alone takes an exclusive knowledge-write advisory lock. Ordinary
knowledge writes take its shared side; task scheduling never uses this lock.
This closes the new-derived-copy race while the dependency closure is scrubbed.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import delete, insert, select, text, update

from src.commands.principal import PrincipalKind
from src.database.tables import (
    knowledge_authority_grants,
    knowledge_proposals,
    knowledge_redactions,
    knowledge_redaction_targets,
    knowledge_revision_payloads,
    knowledge_revisions,
    knowledge_search,
    record_outbox,
    record_source_artifacts,
    record_export_state,
    record_import_runs,
    record_import_items,
)
from src.records.models import RecordError, uuid_value

KNOWLEDGE_ERASURE_LOCK = 710523048019


def depends_on(snapshot, revision_ids, record_ids, artifact_ids, link_ids):
    if not snapshot:
        return False
    return (
        snapshot.get("summary_of_revision") in revision_ids
        or any(s.get("artifact_id") in artifact_ids for s in snapshot["sources"])
        or any(
            v["target_record_id"] in record_ids or v["link_id"] in link_ids
            for v in snapshot["outgoing_links"]
        )
    )


class RedactionMixin:
    async def redact(self, **kwargs):
        return await self._transaction(lambda conn: self.redact_on(conn=conn, **kwargs))

    async def redact_on(
        self,
        identity,
        *,
        principal,
        project_id,
        idempotency_key,
        if_revision=None,
        revision_id=None,
        reason_code,
        dry_run=True,
        conn,
    ):
        if_revision = self._optional_token(if_revision, "if_revision")
        if principal is None or principal.kind != PrincipalKind.LOCAL:
            raise RecordError("record.forbidden")
        access = await self._access(conn, principal, "knowledge_redact", project_id, write=True)
        if reason_code not in {"sensitive", "privacy", "operator_erasure"}:
            raise RecordError("record.invalid_input")
        record = await self.resolve_on(identity, access, conn=conn)
        receipt, replay = await self._receipt(
            conn,
            access,
            "knowledge_redact",
            idempotency_key,
            dict(
                record_id=str(record["record_id"]),
                if_revision=if_revision,
                revision_id=revision_id,
                reason_code=reason_code,
                dry_run=dry_run,
            ),
        )
        if replay:
            return replay
        await self._lock_source(record, conn=conn)
        current = await self._revision(record, conn=conn)
        self._precondition(if_revision, current["revision_id"])
        selected = uuid_value(revision_id, "revision_id") if revision_id else None
        if selected:
            await self._revision(record, selected, conn=conn)
        # No user content leaves this transaction. A bounded closure covers
        # derived summaries, citations, link metadata and shared artifacts.
        rows = [
            dict(row)
            for row in (
                await conn.execute(
                    select(
                        knowledge_revisions,
                        knowledge_revision_payloads.c.snapshot,
                    )
                    .join(
                        knowledge_revision_payloads,
                        knowledge_revision_payloads.c.revision_id
                        == knowledge_revisions.c.revision_id,
                    )
                    .where(knowledge_revision_payloads.c.snapshot.is_not(None))
                )
            ).mappings()
        ]
        affected = {
            r["revision_id"]: r
            for r in rows
            if r["record_id"] == record["record_id"]
            and (selected is None or r["revision_id"] == selected)
        }
        artifacts, owned_links = set(), set()
        while True:
            rids = {str(r["record_id"]) for r in affected.values()}
            revs = {str(r) for r in affected}
            for row in affected.values():
                artifacts.update(
                    s["artifact_id"] for s in row["snapshot"]["sources"] if s.get("artifact_id")
                )
                owned_links.update(v["link_id"] for v in row["snapshot"]["outgoing_links"])
            additions = {
                r["revision_id"]: r
                for r in rows
                if r["revision_id"] not in affected
                and depends_on(r["snapshot"], revs, rids, artifacts, owned_links)
            }
            if not additions:
                break
            affected.update(additions)
        if len(affected) > 10000:
            raise RecordError("knowledge.redaction_scope_too_large")
        affected_records = {r["record_id"] for r in affected.values()}
        # Sealed import manifests are operator evidence, never record sources.
        # Scrub their retained copies whenever any imported record is erased;
        # this permanently fences both same-run resume and fresh import replay.
        import_runs = (await conn.execute(select(record_import_runs).where(
            record_import_runs.c.run_id.in_(select(record_import_items.c.run_id).where(
                record_import_items.c.record_id.in_(affected_records),
            )),
        ))).mappings().all()
        for run in import_runs:
            artifacts.add(run["cursor"]["manifest"]["artifact_id"])
        for rid in sorted(affected_records):
            await self.db.get_record_on(record_id=rid, lock=True, conn=conn)
        result = dict(
            success=True,
            outcome="unchanged" if dry_run else "redacted",
            record_id=str(record["record_id"]),
            revision_id=str(current["revision_id"]),
            dry_run=dry_run,
            affected_revisions=len(affected),
            affected_records=len(affected_records),
        )
        if dry_run:
            await self.db.finish_record_request_on(receipt, result, conn=conn)
            return result
        now, redaction_id = datetime.now(UTC), uuid4()
        cleanup = dict(export="pending", artifact="pending", index="pending")
        await conn.execute(
            insert(knowledge_redactions).values(
                redaction_id=redaction_id,
                record_id=record["record_id"],
                revision_id=selected,
                actor_id=access.actor_key,
                reason_code=reason_code,
                cleanup_state=cleanup,
            )
        )
        for rid, row in affected.items():
            await conn.execute(
                insert(knowledge_redaction_targets).values(
                    revision_id=rid,
                    redaction_id=redaction_id,
                    content_sha256=row["content_sha256"],
                )
            )
        proposals = (
            (
                await conn.execute(
                    select(knowledge_proposals).where(
                        knowledge_proposals.c.redacted_at.is_(None),
                    )
                )
            )
            .mappings()
            .all()
        )
        for proposal in proposals:
            if (
                proposal["record_id"] in affected_records
                or proposal["resulting_record_id"] in affected_records
                or depends_on(proposal["proposed_snapshot"], revs, rids, artifacts, owned_links)
            ):
                await conn.execute(
                    update(knowledge_proposals)
                    .where(
                        knowledge_proposals.c.proposal_id == proposal["proposal_id"],
                    )
                    .values(proposed_snapshot=None, source_descriptors=[], redacted_at=now)
                )
        if artifacts:
            await conn.execute(
                update(record_source_artifacts)
                .where(
                    record_source_artifacts.c.artifact_id.in_([UUID(a) for a in artifacts]),
                )
                .values(redacted_at=now)
            )
        await conn.execute(
            delete(knowledge_search).where(
                knowledge_search.c.revision_id.in_(affected),
            )
        )
        await conn.execute(
            update(knowledge_authority_grants)
            .where(
                knowledge_authority_grants.c.record_id.in_(affected_records),
            )
            .values(revoked_at=now, reason="redacted")
        )
        # Invalidate live exporter leases as well as ordinary replay. A worker
        # already holding the file lock is followed by the purge under that lock.
        await conn.execute(
            update(record_outbox)
            .where(
                record_outbox.c.aggregate_record_id.in_(affected_records),
                record_outbox.c.destination != "purge",
            )
            .values(
                delivered_at=now,
                lease_token=None,
                lease_until=None,
                payload={},
                last_error_code="record.revision_redacted",
            )
        )
        await conn.execute(text("SELECT knowledge_erase_v1(:rid)"), {"rid": redaction_id})
        for rid in sorted(affected_records):
            target = await self.db.get_record_on(record_id=rid, conn=conn)
            await conn.execute(
                insert(record_outbox).values(
                    event_id=uuid4(),
                    scope_key=target["scope_key"],
                    aggregate_record_id=rid,
                    event_type="knowledge_redact",
                    destination="purge",
                    dedup_key=f"purge:{redaction_id}:{rid}",
                    payload=dict(redaction_id=str(redaction_id), artifact_ids=sorted(artifacts)),
                    available_at=now,
                )
            )
        result.update(redaction_id=str(redaction_id), cleanup_state=cleanup)
        await self.db.finish_record_request_on(receipt, result, conn=conn)
        return result


async def purge_event(db, exporter, event):
    """Purge only core-managed files. Missing optional indexes are already empty."""
    from src.records.export import ManagedFile, _directory, _io
    import os

    redaction_id = uuid_value(event["payload"].get("redaction_id"), "redaction_id")
    async with db.immediate() as conn:
        if not await conn.scalar(
            select(knowledge_redactions.c.redaction_id).where(
                knowledge_redactions.c.redaction_id == redaction_id,
            )
        ):
            raise RecordError("record.not_found")
        record = await db.get_record_on(record_id=event["aggregate_record_id"], conn=conn)
        artifacts = (
            (
                await conn.execute(
                    select(record_source_artifacts).where(
                        record_source_artifacts.c.artifact_id.in_(
                            [UUID(v) for v in event["payload"].get("artifact_ids", [])]
                        ),
                        record_source_artifacts.c.redacted_at.is_not(None),
                    )
                )
            )
            .mappings()
            .all()
        )
    if exporter is None:
        raise RecordError("record.destination_unavailable")
    managed = await _io(
        ManagedFile, exporter.vault_root, record["scope_key"], record["knowledge_alias"]
    )
    try:
        await _io(managed.purge)
    finally:
        await _io(managed.close)

    def purge_artifact(storage_key):
        # K05 reserves this confined core artifact namespace for future import.
        from pathlib import PurePosixPath

        parts = PurePosixPath(storage_key).parts
        if (
            not parts
            or parts[0] != "record-artifacts"
            or any(p in {"", ".", ".."} or "\\" in p for p in parts)
        ):
            raise RecordError("record.artifact_path")
        try:
            fd = _directory(exporter.vault_root, parts[:-1])
        except FileNotFoundError:
            return
        try:
            try:
                os.unlink(parts[-1], dir_fd=fd)
                os.fsync(fd)
            except FileNotFoundError:
                pass
        finally:
            os.close(fd)

    for artifact in artifacts:
        await _io(purge_artifact, artifact["storage_key"])
    async with db.immediate() as conn:
        await conn.execute(
            delete(record_export_state).where(
                record_export_state.c.record_id == record["record_id"],
            )
        )
    return dict(state="purged", redaction_id=str(redaction_id))
