"""Guarded record operations for CommandHandler composition.

Public ``*_on`` methods use a caller-owned transaction and never commit. Their
errors must escape that transaction (or its savepoint). Convenience methods own
one transaction, including bounded deadlock retries. No external I/O occurs here.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import random
from datetime import UTC, datetime
from dataclasses import replace
from uuid import UUID, uuid4

from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from src.database.queries.record_queries import RecordDomainUnavailable
from src.database.tables import (
    doc_review_revisions,
    doc_reviews,
    knowledge_revisions,
    knowledge_global_shares,
    knowledge_authority_grants,
    record_link_heads,
    record_link_versions,
    record_outbox,
    record_source_artifacts,
    records,
    task_record_link_state,
    task_session_attempts,
)
from src.knowledge.legacy import check_legacy_writer
from src.knowledge.models import LINK_TYPES, content_hash, normalize_snapshot, validate_metadata
from src.records.auth import GLOBAL_SCOPE, RecordAccess, authorize_on
from src.records.models import RecordError, RecordIdentity, uuid_value


class RecordService:
    def __init__(self, db, config, *, active_legacy_scopes=None):
        self.db = db
        self.config = config
        self._request_slots = asyncio.Semaphore(2)
        # The command owner supplies a registry-backed, local-state-only probe.
        self.active_legacy_scopes = active_legacy_scopes or (lambda: frozenset())

    async def _transaction(self, callback):
        # Keep ordinary knowledge traffic bounded independently of the task
        # pool. Composed *_on callers own their connection and request budget.
        try:
            async with asyncio.timeout(5):
                async with self._request_slots:
                    return await self._retry_transaction(callback)
        except TimeoutError as exc:
            raise RecordError("record.retryable") from exc

    async def _retry_transaction(self, callback):
        for attempt in range(3):
            try:
                async with self.db.immediate() as conn:
                    return await callback(conn)
            except DBAPIError as exc:
                state = getattr(exc.orig, "sqlstate", None)
                if state in {"40P01", "40001"}:
                    if attempt == 2:
                        raise RecordError("record.retryable") from exc
                    await asyncio.sleep(random.uniform(0.01, 0.04) * (attempt + 1))
                elif isinstance(exc, IntegrityError):
                    raise RecordError("record.integrity_conflict") from exc
                else:
                    raise

    async def _access(
        self, conn, principal, operation, project_id, *, write=False, claim_epoch=None
    ):
        if write:
            from src.knowledge.redaction import KNOWLEDGE_ERASURE_LOCK

            lock = (
                "pg_advisory_xact_lock"
                if operation == "knowledge_redact"
                else "pg_advisory_xact_lock_shared"
            )
            await conn.execute(text(f"SELECT {lock}(:key)"), {"key": KNOWLEDGE_ERASURE_LOCK})
        # Conflicting activation fails closed without blocking task operation.
        if self.config.enabled:
            check_legacy_writer(self.config, project_id, self.active_legacy_scopes())
        return await authorize_on(
            conn,
            self.config,
            principal,
            operation,
            project_id,
            write=write,
            claim_epoch=claim_epoch,
        )

    async def resolve_on(self, identity: str, access: RecordAccess, *, conn) -> dict:
        ident = RecordIdentity.parse(identity)
        if ident.kind == "task":
            if not access.task_visible(ident.value):
                raise RecordError("record.not_found")
            domain = await self.db.get_task_record_domain_on(ident.value, conn=conn)
            if domain is not None:
                if domain["project_id"] != access.project_id:
                    raise RecordError("record.not_found")
                try:
                    record = await self.db.ensure_task_record_on(
                        ident.value, actor_id=access.actor_key, conn=conn
                    )
                except RecordDomainUnavailable:
                    raise RecordError("record.domain_unavailable") from None
                except LookupError:
                    raise RecordError("record.not_found") from None
            else:
                record = await self.db.get_record_on(task_id=ident.value, conn=conn)
        else:
            key = "record_id" if ident.kind == "record" else "knowledge_alias"
            record = await self.db.get_record_on(**{key: ident.value}, conn=conn)
        from src.knowledge.sharing import visible_scope

        if not record or not await conn.scalar(
            select(records.c.record_id).where(
                records.c.record_id == record["record_id"],
                visible_scope(access, records),
            )
        ):
            raise RecordError("record.not_found")
        if record["kind"] == "task":
            if not access.task_visible(record["task_id"]):
                raise RecordError("record.not_found")
            domain = await self.db.get_task_record_domain_on(record["task_id"], conn=conn)
            if not domain:
                raise RecordError("record.domain_unavailable")
            if domain["project_id"] != access.project_id:
                raise RecordError("record.not_found")
        return record

    async def _revision(self, record, revision_id=None, *, conn):
        if record["kind"] != "knowledge":
            raise RecordError("record.invalid_input", "Knowledge revision required")
        revision = await self.db.get_knowledge_revision_on(
            record["record_id"],
            revision_id=uuid_value(revision_id, "revision_id") if revision_id is not None else None,
            conn=conn,
        )
        if not revision:
            raise RecordError("record.revision_unavailable")
        if revision["snapshot"] is None:
            raise RecordError("record.revision_redacted")
        return revision

    async def _source_visible(self, source, access, *, conn, writing=False):
        """Recheck retained evidence without following caller URLs or paths."""
        kind = source["kind"]
        source_scope = access.evidence_scope or access.scope_key
        if source_scope == "global" and kind != "artifact":
            raise RecordError(
                "record.source_unavailable", "Global evidence requires a global artifact"
            )
        if kind == "task":
            if not access.task_visible(source["task_id"]):
                raise RecordError("record.not_found")
            task = await self.db.get_task_record_domain_on(source["task_id"], conn=conn)
            if not task or task["project_id"] != access.project_id:
                raise RecordError("record.not_found")
            if source.get("attempt_id") or source.get("session_id"):
                stmt = select(task_session_attempts.c.id).where(
                    task_session_attempts.c.task_id == source["task_id"],
                    task_session_attempts.c.project_id == access.project_id,
                )
                for key, column in (("attempt_id", "id"), ("session_id", "session_id")):
                    if source.get(key):
                        stmt = stmt.where(task_session_attempts.c[column] == source[key])
                if not await conn.scalar(stmt.limit(1)):
                    raise RecordError("record.source_unavailable")
        elif kind == "review":
            row = (
                (
                    await conn.execute(
                        select(
                            doc_review_revisions.c.content,
                            doc_review_revisions.c.content_sha256,
                        )
                        .join(doc_reviews, doc_reviews.c.id == doc_review_revisions.c.review_id)
                        .where(
                            doc_reviews.c.project_id == access.project_id,
                            doc_reviews.c.id == source["review_id"],
                            doc_review_revisions.c.revision == source["revision"],
                        )
                    )
                )
                .mappings()
                .first()
            )
            if not row:
                raise RecordError("record.not_found")
            if (
                row["content_sha256"] != source["sha256"]
                or hashlib.sha256(row["content"].encode()).hexdigest() != source["sha256"]
            ):
                raise RecordError("record.source_unavailable")
        elif kind == "legacy" and writing:
            raise RecordError("knowledge.operation_unavailable", "Legacy import requires K07")
        if source.get("artifact_id"):
            artifact = (
                (
                    await conn.execute(
                        select(record_source_artifacts).where(
                            record_source_artifacts.c.artifact_id == UUID(source["artifact_id"]),
                            record_source_artifacts.c.scope_key == source_scope,
                            record_source_artifacts.c.redacted_at.is_(None),
                        )
                    )
                )
                .mappings()
                .first()
            )
            if not artifact:
                raise RecordError("record.not_found")
            if artifact["media_type"] == "application/vnd.aq.import-manifest":
                raise RecordError("record.source_unavailable", "Import manifests are operator-only")
            if source.get("sha256") and artifact["content_sha256"] != source["sha256"]:
                raise RecordError("record.source_unavailable")

    async def _validate_snapshot_access(self, snapshot, access, *, conn):
        for source in snapshot["sources"]:
            await self._source_visible(source, access, conn=conn, writing=True)
        if snapshot["summary_of_revision"]:
            owner = await conn.scalar(
                select(knowledge_revisions.c.record_id).where(
                    knowledge_revisions.c.revision_id == UUID(snapshot["summary_of_revision"])
                )
            )
            if owner is None:
                raise RecordError("record.source_unavailable")
            record = await self.resolve_on(f"record:{owner}", access, conn=conn)
            await self._revision(record, snapshot["summary_of_revision"], conn=conn)
        for link in snapshot["outgoing_links"]:
            target = await self.resolve_on(f"record:{link['target_record_id']}", access, conn=conn)
            if link["target_revision_id"]:
                await self._revision(target, link["target_revision_id"], conn=conn)

    async def _safe_snapshot(self, snapshot, access, *, conn, scope_key=None):
        if scope_key is not None:
            access = replace(access, evidence_scope=scope_key)
        value = copy.deepcopy(snapshot)
        value["outgoing_links"] = [
            self._link_snapshot(link)
            for link in await self._visible_links(value["outgoing_links"], access, conn=conn)
        ]
        value["sources"] = []
        for source in snapshot["sources"]:
            try:
                await self._source_visible(source, access, conn=conn)
            except RecordError:
                continue
            value["sources"].append(source)
        for key in ("successor_record_id", "summary_of_revision"):
            if not value[key]:
                continue
            try:
                owner = value[key]
                if key == "summary_of_revision":
                    owner = await conn.scalar(
                        select(knowledge_revisions.c.record_id).where(
                            knowledge_revisions.c.revision_id == UUID(owner)
                        )
                    )
                await self.resolve_on(f"record:{owner}", access, conn=conn)
            except RecordError:
                value[key] = None
        return value

    async def _visible_links(self, links, access, *, conn):
        visible = []
        for link in links:
            try:
                target = await self.resolve_on(
                    f"record:{link['target_record_id']}", access, conn=conn
                )
            except RecordError:
                continue
            value = dict(link)
            value["availability"] = "available"
            if target["kind"] == "knowledge":
                try:
                    revision = await self._revision(target, link["target_revision_id"], conn=conn)
                    value["resolved_revision_id"] = str(revision["revision_id"])
                except RecordError as exc:
                    value["availability"] = exc.code
                    if exc.code == "record.revision_redacted":
                        value["metadata"] = {}
            visible.append(value)
        return visible

    async def show(self, **kwargs):
        return await self._transaction(lambda conn: self.show_on(conn=conn, **kwargs))

    async def show_on(
        self, identity, *, principal, project_id, revision_id=None, include_edges=False, conn
    ):
        access = await self._access(conn, principal, "record_show", project_id)
        record = await self.resolve_on(identity, access, conn=conn)
        result = {
            "success": True,
            "outcome": "read",
            "record_id": str(record["record_id"]),
            "kind": record["kind"],
        }
        if record["kind"] == "task":
            if revision_id is not None:
                raise RecordError("record.invalid_input", "Tasks have no knowledge revision")
            result["task"] = await self.db.get_task_record_domain_on(record["task_id"], conn=conn)
            state = (
                (
                    await conn.execute(
                        select(task_record_link_state).where(
                            task_record_link_state.c.record_id == record["record_id"]
                        )
                    )
                )
                .mappings()
                .one()
            )
            result["link_token"] = str(state["link_token"])
        else:
            revision = await self._revision(record, revision_id, conn=conn)
            from src.knowledge.authority import authority_on

            result.update(
                authority=await authority_on(
                    conn, record, revision, review_required=self.config.authority_review_required
                ),
                knowledge_alias=record["knowledge_alias"],
                revision_id=str(revision["revision_id"]),
                sequence=revision["sequence"],
                content_sha256=revision["content_sha256"],
                hash_version=revision["hash_version"],
                snapshot=await self._safe_snapshot(
                    revision["snapshot"], access, conn=conn, scope_key=record["scope_key"]
                ),
            )
        if include_edges:
            from src.records.graph import record_edges_on

            result["edges"] = await record_edges_on(
                self, record, access, revision_id=result.get("revision_id"), conn=conn
            )
        return result

    async def search(self, **kwargs):
        from src.records.search import search_on

        return await self._transaction(lambda conn: search_on(self, conn=conn, **kwargs))

    async def list_links(self, **kwargs):
        return await self._transaction(lambda conn: self.list_links_on(conn=conn, **kwargs))

    async def list_links_on(self, identity, *, principal, project_id, revision_id=None, conn):
        access = await self._access(conn, principal, "link_list", project_id)
        record = await self.resolve_on(identity, access, conn=conn)
        if record["kind"] == "knowledge":
            revision = await self._revision(record, revision_id, conn=conn)
            links = revision["snapshot"]["outgoing_links"]
        else:
            if revision_id is not None:
                raise RecordError("record.invalid_input")
            links = [
                self._link_snapshot(link)
                for link in await self.db.current_record_links_on(record["record_id"], conn=conn)
                if not link["removed"]
            ]
        return {
            "success": True,
            "outcome": "read",
            "links": await self._visible_links(links, access, conn=conn),
        }

    async def _receipt(self, conn, access, operation, key, arguments):
        if not isinstance(key, str) or not 1 <= len(key) <= 128:
            raise RecordError("record.invalid_input", "idempotency_key must have 1–128 characters")
        scope = await self.db.ensure_record_scope_on(
            project_id=None if access.project_id is GLOBAL_SCOPE else access.project_id, conn=conn
        )
        try:
            digest = content_hash(arguments)
        except (ValueError, TypeError, UnicodeError) as exc:
            raise RecordError("record.invalid_input", str(exc)) from exc
        receipt = await self.db.begin_record_request_on(
            scope_key=scope,
            actor_key=access.actor_key,
            operation=operation,
            idempotency_key=key,
            request_sha256=digest,
            conn=conn,
        )
        if receipt["request_sha256"] != digest:
            raise RecordError("record.idempotency_conflict")
        if receipt["result"]:
            access = await self._access(
                conn,
                access.principal,
                operation,
                access.project_id,
                write=True,
                claim_epoch=access.claim_epoch,
            )
            result = receipt["result"]
            if result.get("proposal_id"):
                from src.knowledge.proposals import reauthorize_proposal

                await reauthorize_proposal(self, conn, result["proposal_id"], access)
            if not result.get("record_id"):
                return receipt, {**result, "outcome": "replayed"}
            record = await self.resolve_on(f"record:{result['record_id']}", access, conn=conn)
            if (
                record["kind"] == "knowledge"
                and result.get("revision_id")
                and not result.get("redaction_id")
            ):
                revision = await self._revision(record, result["revision_id"], conn=conn)
                await self._validate_snapshot_access(revision["snapshot"], access, conn=conn)
            if "authority" in result:
                from src.knowledge.authority import authority_on

                result = {
                    **result,
                    "authority": await authority_on(
                        conn,
                        record,
                        revision,
                        review_required=self.config.authority_review_required,
                    ),
                }
            return receipt, {**result, "outcome": "replayed"}
        return receipt, None

    async def _lock_source(self, record, *, conn):
        return await self.db.get_record_on(record_id=record["record_id"], lock=True, conn=conn)

    @staticmethod
    def _precondition(expected, current, field="if_revision"):
        if expected is None:
            raise RecordError("record.precondition_required", f"{field} is required")
        if uuid_value(expected, field) != current:
            raise RecordError("record.revision_conflict", current_token=str(current))

    @staticmethod
    def _optional_token(value, field):
        return str(uuid_value(value, field)) if value is not None else None

    @staticmethod
    def _link_snapshot(link):
        value = {
            key: link[key]
            for key in (
                "link_id",
                "version",
                "link_type",
                "target_record_id",
                "target_revision_id",
                "metadata",
            )
        }
        for key in ("link_id", "target_record_id", "target_revision_id"):
            if value[key] is not None:
                value[key] = str(value[key])
        return value

    @staticmethod
    def _normalize_operations(operations):
        if not isinstance(operations, list) or not 1 <= len(operations) <= 100:
            raise RecordError("record.invalid_input", "Supply 1–100 link operations")
        result = copy.deepcopy(operations)
        seen = set()
        for op in result:
            if not isinstance(op, dict) or op.get("action") not in {"add", "remove", "update"}:
                raise RecordError("record.invalid_input", "Invalid link action")
            allowed = (
                {"action", "link_id"}
                if op["action"] == "remove"
                else {"action", "link_id", "target", "target_revision_id", "link_type", "metadata"}
            )
            if set(op) - allowed:
                raise RecordError("record.invalid_input", "Unknown link fields")
            if op["action"] != "remove":
                if op.get("link_type") not in LINK_TYPES:
                    raise RecordError("record.execution_link_forbidden")
                ident = RecordIdentity.parse(op.get("target"))
                op["target"] = f"{ident.kind}:{ident.value}"
                if op.get("target_revision_id") is not None:
                    op["target_revision_id"] = str(
                        uuid_value(op["target_revision_id"], "target pin")
                    )
                else:
                    op["target_revision_id"] = None
                try:
                    op["metadata"] = validate_metadata(op.get("metadata", {}))
                except ValueError as exc:
                    raise RecordError("record.invalid_input", str(exc)) from exc
            if op["action"] == "add":
                if "link_id" in op:
                    raise RecordError("record.invalid_input", "New link IDs are server allocated")
            else:
                op["link_id"] = str(uuid_value(op.get("link_id"), "link_id"))
                if op["link_id"] in seen:
                    raise RecordError("record.invalid_input", "One operation per link per mutation")
                seen.add(op["link_id"])
        return result

    async def _plan_links(self, record, operations, access, *, conn):
        rows = await self.db.current_record_links_on(
            record["record_id"],
            conn=conn,
            link_ids=[UUID(op["link_id"]) for op in operations if op.get("link_id")],
        )
        current = {str(row["link_id"]): row for row in rows}
        changes = []
        for op in operations:
            previous = current.get(op.get("link_id"))
            if op["action"] != "add" and previous is None:
                raise RecordError("record.not_found")
            if previous:
                await self.resolve_on(f"record:{previous['target_record_id']}", access, conn=conn)
            if op["action"] == "remove":
                if previous["removed"]:
                    continue
                value = {**self._link_snapshot(previous), "removed": True}
            else:
                target = await self.resolve_on(op["target"], access, conn=conn)
                if record["scope_key"] == "global" and target["scope_key"] != "global":
                    raise RecordError("knowledge.cross_project_forbidden")
                if op["link_type"] == "supersedes" and record["scope_key"] != target["scope_key"]:
                    raise RecordError("record.invalid_link")
                pin = op["target_revision_id"]
                if target["kind"] == "knowledge" or pin:
                    await self._revision(target, pin, conn=conn)
                link_type = op["link_type"]
                if (
                    record["record_id"] == target["record_id"]
                    or (
                        link_type in {"motivated_by", "produces"}
                        and (record["kind"], target["kind"]) != ("task", "knowledge")
                    )
                    or (
                        link_type in {"supports", "contradicts", "supersedes"}
                        and (record["kind"], target["kind"]) != ("knowledge", "knowledge")
                    )
                ):
                    raise RecordError("record.invalid_link")
                value = dict(
                    link_id=op.get("link_id", str(uuid4())),
                    version=1,
                    link_type=link_type,
                    target_record_id=str(target["record_id"]),
                    target_revision_id=pin,
                    metadata=op["metadata"],
                    removed=False,
                )
                if previous:
                    value["version"] = previous["version"]
                    if not previous["removed"] and self._link_snapshot(previous) == {
                        key: val for key, val in value.items() if key != "removed"
                    }:
                        continue
            if previous:
                value["version"] = previous["version"] + 1
            current[value["link_id"]] = value
            changes.append(value)
        active = [self._link_snapshot(row) for row in current.values() if not row["removed"]]
        if len(active) > 1000:
            raise RecordError("record.invalid_input", "At most 1000 active outgoing links")
        tuples = {
            (link["link_type"], link["target_record_id"], link["target_revision_id"])
            for link in active
        }
        if len(tuples) != len(active):
            raise RecordError("record.duplicate_link")
        return sorted(active, key=lambda link: link["link_id"]), changes

    async def _write_links(self, record, changes, access, revision_id, now, *, conn):
        for link in sorted(changes, key=lambda link: link["link_id"]):
            lid = UUID(link["link_id"])
            if link["version"] == 1:
                await conn.execute(
                    insert(record_link_heads).values(
                        link_id=lid,
                        source_record_id=record["record_id"],
                        owner_scope_key=record["scope_key"],
                        current_version=1,
                    )
                )
            else:
                await conn.execute(
                    update(record_link_heads)
                    .where(
                        record_link_heads.c.link_id == lid,
                        record_link_heads.c.source_record_id == record["record_id"],
                    )
                    .values(current_version=link["version"])
                )
            await conn.execute(
                insert(record_link_versions).values(
                    **{
                        **link,
                        "link_id": lid,
                        "target_record_id": UUID(link["target_record_id"]),
                        "target_revision_id": UUID(link["target_revision_id"])
                        if link["target_revision_id"]
                        else None,
                    },
                    actor_id=access.actor_key,
                    source_revision_id=revision_id,
                    created_at=now,
                )
            )

    async def _outbox(self, record, result, access, operation, now, *, conn):
        await conn.execute(
            insert(record_outbox).values(
                event_id=uuid4(),
                scope_key=record["scope_key"],
                aggregate_record_id=record["record_id"],
                revision_id=UUID(result["revision_id"]) if result.get("revision_id") else None,
                event_type=operation,
                destination="audit",
                dedup_key=f"audit:{record['record_id']}:{result.get('revision_id', result.get('link_token'))}",
                payload={**result, "actor": access.actor_key, "operation": operation},
                available_at=now,
            )
        )
        if self.config.export.enabled and result.get("revision_id"):
            await conn.execute(
                insert(record_outbox).values(
                    event_id=uuid4(),
                    scope_key=record["scope_key"],
                    aggregate_record_id=record["record_id"],
                    revision_id=UUID(result["revision_id"]),
                    event_type=operation,
                    destination="export",
                    dedup_key=f"export:{record['record_id']}:{result['revision_id']}",
                    payload={**result, "actor": access.actor_key, "operation": operation},
                    available_at=now,
                )
            )

    async def _append_snapshot(
        self,
        record,
        current,
        snapshot,
        access,
        operation,
        *,
        conn,
        changes=(),
        force_revision=False,
    ):
        if current and operation != "knowledge_verify":
            snapshot = {
                **snapshot,
                "verification": "unverified",
                "last_verified_at": None,
                "last_verified_by": None,
            }
        snapshot = normalize_snapshot(snapshot)
        await self._validate_snapshot_access(snapshot, access, conn=conn)
        digest = content_hash(snapshot)
        from src.database.tables import knowledge_redaction_targets

        if await conn.scalar(
            select(knowledge_redaction_targets.c.revision_id)
            .join(
                knowledge_revisions,
                knowledge_revisions.c.revision_id == knowledge_redaction_targets.c.revision_id,
            )
            .join(records, records.c.record_id == knowledge_revisions.c.record_id)
            .where(
                knowledge_redaction_targets.c.content_sha256 == digest,
                records.c.scope_key == record["scope_key"],
            )
            .limit(1)
        ):
            raise RecordError("record.revision_redacted")
        if current and digest == current["content_sha256"] and not force_revision:
            return self._knowledge_result(record, current, "unchanged")
        if record["scope_key"] == "global":
            from src.knowledge.sharing import validate_share_targets

            recipients = (
                (
                    await conn.execute(
                        select(knowledge_global_shares.c.project_id).where(
                            knowledge_global_shares.c.record_id == record["record_id"],
                            knowledge_global_shares.c.revoked_at.is_(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
            await validate_share_targets(conn, snapshot, recipients)
        now, revision_id = datetime.now(UTC), uuid4()
        await conn.execute(
            update(knowledge_authority_grants)
            .where(
                knowledge_authority_grants.c.record_id == record["record_id"],
                knowledge_authority_grants.c.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        sequence = current["sequence"] + 1 if current else 1
        if current is None:
            await self.db.insert_knowledge_record_on(
                dict(
                    record_id=record["record_id"],
                    current_revision_id=revision_id,
                    current_sequence=sequence,
                    updated_at=now,
                ),
                conn=conn,
            )
        revision = dict(
            record_id=record["record_id"],
            revision_id=revision_id,
            sequence=sequence,
            parent_revision_id=current["revision_id"] if current else None,
            actor_id=access.actor_key,
            created_at=now,
            change_kind=operation,
            content_sha256=digest,
            hash_version=1,
        )
        await self.db.append_knowledge_revision_on(revision, snapshot, conn=conn)
        if current:
            advanced = await self.db.advance_knowledge_head_on(
                record["record_id"],
                expected_revision=current["revision_id"],
                revision_id=revision_id,
                sequence=sequence,
                updated_at=now,
                conn=conn,
            )
            if not advanced:
                raise RecordError("record.revision_conflict")
        await self._write_links(record, changes, access, revision_id, now, conn=conn)
        await self.db.project_knowledge_search_on(
            record["record_id"], revision_id, record["scope_key"], snapshot, now, conn=conn
        )
        await conn.execute(
            update(records).where(records.c.record_id == record["record_id"]).values(updated_at=now)
        )
        result = self._knowledge_result(record, revision, "updated" if current else "created")
        await self._outbox(record, result, access, operation, now, conn=conn)
        return result

    def _knowledge_result(self, record, revision, outcome):
        return dict(
            success=True,
            outcome=outcome,
            record_id=str(record["record_id"]),
            knowledge_alias=record["knowledge_alias"],
            revision_id=str(revision["revision_id"]),
            sequence=revision["sequence"],
            content_sha256=revision["content_sha256"],
            export_state="pending" if self.config.export.enabled else "disabled",
            index_state="disabled",
        )

    async def mutate_links(self, **kwargs):
        return await self._transaction(lambda conn: self.mutate_links_on(conn=conn, **kwargs))

    async def mutate_links_on(
        self,
        identity,
        operations,
        *,
        principal,
        project_id,
        idempotency_key,
        if_revision=None,
        if_link_token=None,
        claim_epoch=None,
        conn,
    ):
        operations = self._normalize_operations(operations)
        if_revision = self._optional_token(if_revision, "if_revision")
        if_link_token = self._optional_token(if_link_token, "if_link_token")
        operation = (
            "link_remove" if all(op["action"] == "remove" for op in operations) else "link_create"
        )
        access = await self._access(
            conn, principal, operation, project_id, write=True, claim_epoch=claim_epoch
        )
        # A mixed batch needs both grants, not just its most permissive operation.
        if operation == "link_create" and any(op["action"] == "remove" for op in operations):
            await self._access(
                conn, principal, "link_remove", project_id, write=True, claim_epoch=claim_epoch
            )
        record = await self.resolve_on(identity, access, conn=conn)
        receipt, replay = await self._receipt(
            conn,
            access,
            operation,
            idempotency_key,
            dict(
                record_id=str(record["record_id"]),
                operations=operations,
                if_revision=str(if_revision) if if_revision is not None else None,
                if_link_token=str(if_link_token) if if_link_token is not None else None,
            ),
        )
        if replay:
            # Reauthorize targets even for task link receipts (which have no snapshot).
            previous_links = {
                str(row["link_id"]): row
                for row in await self.db.current_record_links_on(
                    record["record_id"],
                    conn=conn,
                    link_ids=[UUID(op["link_id"]) for op in operations if op.get("link_id")],
                )
            }
            for op in operations:
                if op.get("target"):
                    target = await self.resolve_on(op["target"], access, conn=conn)
                    if target["kind"] == "knowledge" or op["target_revision_id"]:
                        await self._revision(target, op["target_revision_id"], conn=conn)
                if op.get("link_id"):
                    previous = previous_links.get(op["link_id"])
                    if previous:
                        await self.resolve_on(
                            f"record:{previous['target_record_id']}", access, conn=conn
                        )
            return replay
        record = await self._lock_source(record, conn=conn)
        access = await self._access(
            conn, principal, operation, project_id, write=True, claim_epoch=claim_epoch
        )
        record = await self.resolve_on(f"record:{record['record_id']}", access, conn=conn)
        if record["kind"] == "knowledge":
            current = await self._revision(record, conn=conn)
            access.editable(record, current["snapshot"])
            self._precondition(if_revision, current["revision_id"])
            if current["snapshot"]["lifecycle"] != "active":
                raise RecordError("record.forbidden", "Restore before editing retired knowledge")
        else:
            access.editable(record)
            state = (
                (
                    await conn.execute(
                        select(task_record_link_state)
                        .where(task_record_link_state.c.record_id == record["record_id"])
                        .with_for_update()
                    )
                )
                .mappings()
                .one()
            )
            self._precondition(if_link_token, state["link_token"], "if_link_token")
        active, changes = await self._plan_links(record, operations, access, conn=conn)
        if record["kind"] == "knowledge":
            snapshot = {**current["snapshot"], "outgoing_links": active}
            if changes and snapshot["summary_of_revision"]:
                snapshot.update(summary=None, summary_of_revision=None)
            result = await self._append_snapshot(
                record, current, snapshot, access, operation, changes=changes, conn=conn
            )
        else:
            now = datetime.now(UTC)
            token = uuid4() if changes else state["link_token"]
            result = dict(
                success=True,
                outcome="updated" if changes else "unchanged",
                record_id=str(record["record_id"]),
                link_token=str(token),
                link_sequence=state["link_sequence"] + bool(changes),
            )
            if changes:
                await self._write_links(record, changes, access, None, now, conn=conn)
                await conn.execute(
                    update(task_record_link_state)
                    .where(task_record_link_state.c.record_id == record["record_id"])
                    .values(link_token=token, link_sequence=result["link_sequence"], updated_at=now)
                )
                await conn.execute(
                    update(records)
                    .where(records.c.record_id == record["record_id"])
                    .values(updated_at=now)
                )
                await self._outbox(record, result, access, operation, now, conn=conn)
        access = await self._access(
            conn, principal, operation, project_id, write=True, claim_epoch=claim_epoch
        )
        await self.resolve_on(f"record:{record['record_id']}", access, conn=conn)
        await self.db.finish_record_request_on(receipt, result, conn=conn)
        return result
