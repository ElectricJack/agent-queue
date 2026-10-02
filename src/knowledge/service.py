"""Project-scoped knowledge commands. Protected/global operations ship in K05."""

from copy import deepcopy

from sqlalchemy import select

from src.database.tables import record_link_heads, record_link_versions
from src.knowledge.models import normalize_snapshot
from src.records.identity import knowledge_identity
from src.records.models import RecordError, uuid_value
from src.records.service import RecordService

EDIT_FIELDS = frozenset(
    {
        "title",
        "body",
        "category",
        "tags",
        "summary",
        "summary_of_revision",
        "valid_from",
        "valid_until",
        "recheck_at",
        "sources",
        "metadata",
        "change_reason",
    }
)


class KnowledgeService(RecordService):
    async def create(self, **kwargs):
        return await self._transaction(lambda conn: self.create_on(conn=conn, **kwargs))

    async def create_on(
        self, snapshot, *, principal, project_id, idempotency_key, claim_epoch=None, conn
    ):
        snapshot = normalize_snapshot(snapshot)
        if (
            snapshot["verification"] != "unverified"
            or snapshot["lifecycle"] != "active"
            or snapshot["successor_record_id"]
            or snapshot["retirement_reason"]
            or snapshot["outgoing_links"]
        ):
            raise RecordError(
                "knowledge.operation_unavailable", "Create an active unverified finding"
            )
        access = await self._access(
            conn, principal, "knowledge_create", project_id, write=True, claim_epoch=claim_epoch
        )
        await self._validate_snapshot_access(snapshot, access, conn=conn)
        receipt, replay = await self._receipt(
            conn, access, "knowledge_create", idempotency_key, snapshot
        )
        if replay:
            return replay
        access = await self._access(
            conn, principal, "knowledge_create", project_id, write=True, claim_epoch=claim_epoch
        )
        record_id, alias = knowledge_identity()
        record = dict(
            record_id=record_id,
            kind="knowledge",
            scope_key=receipt["scope_key"],
            knowledge_alias=alias,
            created_by=access.actor_key,
        )
        await self.db.insert_record_on(record, conn=conn)
        result = await self._append_snapshot(
            record, None, snapshot, access, "knowledge_create", conn=conn
        )
        await self._access(
            conn, principal, "knowledge_create", project_id, write=True, claim_epoch=claim_epoch
        )
        await self.db.finish_record_request_on(receipt, result, conn=conn)
        return result

    async def show_on(self, identity, *, principal, project_id, revision_id=None, conn):
        access = await self._access(conn, principal, "knowledge_show", project_id)
        record = await self.resolve_on(identity, access, conn=conn)
        revision = await self._revision(record, revision_id, conn=conn)
        return dict(
            success=True,
            outcome="read",
            record_id=str(record["record_id"]),
            kind="knowledge",
            knowledge_alias=record["knowledge_alias"],
            revision_id=str(revision["revision_id"]),
            sequence=revision["sequence"],
            content_sha256=revision["content_sha256"],
            hash_version=1,
            snapshot=await self._safe_snapshot(revision["snapshot"], access, conn=conn),
        )

    async def history(self, **kwargs):
        return await self._transaction(lambda conn: self.history_on(conn=conn, **kwargs))

    async def history_on(
        self, identity, *, principal, project_id, before_sequence=None, limit=25, conn
    ):
        access = await self._access(conn, principal, "knowledge_history", project_id)
        record = await self.resolve_on(identity, access, conn=conn)
        if record["kind"] != "knowledge":
            raise RecordError("record.invalid_input")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise RecordError("record.invalid_input", "limit must be between 1 and 100")
        if before_sequence is not None and (
            type(before_sequence) is not int or before_sequence < 1
        ):
            raise RecordError("record.invalid_input")
        rows = await self.db.list_knowledge_history_on(
            record["record_id"], before_sequence=before_sequence, limit=limit, conn=conn
        )
        return {
            "success": True,
            "outcome": "read",
            "revisions": [
                {
                    **row,
                    "record_id": str(row["record_id"]),
                    "revision_id": str(row["revision_id"]),
                    "parent_revision_id": str(row["parent_revision_id"])
                    if row["parent_revision_id"]
                    else None,
                    "created_at": row["created_at"].isoformat(),
                }
                for row in rows
            ],
        }

    async def update(self, **kwargs):
        return await self._transaction(lambda conn: self.update_on(conn=conn, **kwargs))

    async def update_on(
        self,
        identity,
        patch,
        *,
        principal,
        project_id,
        idempotency_key,
        if_revision=None,
        claim_epoch=None,
        conn,
    ):
        if not isinstance(patch, dict) or set(patch) - EDIT_FIELDS:
            raise RecordError(
                "record.invalid_input", "Use dedicated link/retire/restore operations"
            )
        normalized = normalize_snapshot(
            {"title": "validation", "body": "", "category": "note", **patch}
        )
        patch = {key: normalized.get(key) for key in patch}
        return await self._mutate_on(
            identity,
            "knowledge_update",
            patch,
            principal=principal,
            project_id=project_id,
            idempotency_key=idempotency_key,
            if_revision=if_revision,
            claim_epoch=claim_epoch,
            conn=conn,
        )

    async def retire(self, **kwargs):
        return await self._transaction(lambda conn: self.retire_on(conn=conn, **kwargs))

    async def retire_on(
        self,
        identity,
        reason,
        *,
        principal,
        project_id,
        idempotency_key,
        if_revision=None,
        successor_record_id=None,
        claim_epoch=None,
        conn,
    ):
        if not isinstance(reason, str) or not reason.strip() or len(reason.encode()) > 4096:
            raise RecordError(
                "record.invalid_input", "Retirement requires a reason of at most 4096 bytes"
            )
        successor = (
            str(uuid_value(successor_record_id, "successor")) if successor_record_id else None
        )
        return await self._mutate_on(
            identity,
            "knowledge_retire",
            dict(retirement_reason=reason, successor_record_id=successor),
            principal=principal,
            project_id=project_id,
            idempotency_key=idempotency_key,
            if_revision=if_revision,
            claim_epoch=claim_epoch,
            conn=conn,
        )

    async def restore(self, **kwargs):
        return await self._transaction(lambda conn: self.restore_on(conn=conn, **kwargs))

    async def restore_on(
        self,
        identity,
        revision_id,
        reason,
        *,
        principal,
        project_id,
        idempotency_key,
        if_revision=None,
        claim_epoch=None,
        conn,
    ):
        if not isinstance(reason, str) or not reason.strip() or len(reason.encode()) > 4096:
            raise RecordError(
                "record.invalid_input", "Restore requires a reason of at most 4096 bytes"
            )
        return await self._mutate_on(
            identity,
            "knowledge_restore",
            dict(
                revision_id=str(uuid_value(revision_id, "revision_id")),
                reason=reason,
            ),
            principal=principal,
            project_id=project_id,
            idempotency_key=idempotency_key,
            if_revision=if_revision,
            claim_epoch=claim_epoch,
            conn=conn,
        )

    async def _mutate_on(
        self,
        identity,
        operation,
        arguments,
        *,
        principal,
        project_id,
        idempotency_key,
        if_revision,
        claim_epoch,
        conn,
    ):
        if_revision = self._optional_token(if_revision, "if_revision")
        access = await self._access(
            conn, principal, operation, project_id, write=True, claim_epoch=claim_epoch
        )
        record = await self.resolve_on(identity, access, conn=conn)
        if record["kind"] != "knowledge":
            raise RecordError("record.invalid_input")
        if operation != "knowledge_update" and not access.supervisor:
            raise RecordError("record.forbidden")
        receipt, replay = await self._receipt(
            conn,
            access,
            operation,
            idempotency_key,
            dict(
                record_id=str(record["record_id"]),
                arguments=arguments,
                if_revision=str(if_revision) if if_revision is not None else None,
            ),
        )
        if replay:
            return replay
        if operation == "knowledge_retire" and arguments["successor_record_id"]:
            successor = await self.resolve_on(
                f"record:{arguments['successor_record_id']}", access, conn=conn
            )
            if successor["kind"] != "knowledge" or successor["record_id"] == record["record_id"]:
                raise RecordError("record.invalid_link")
            # The successor owns the supersedes edge. Lock both sources in
            # UUID order so concurrent removal cannot create write skew.
            for candidate in sorted((record, successor), key=lambda row: row["record_id"]):
                await self._lock_source(candidate, conn=conn)
        else:
            record = await self._lock_source(record, conn=conn)
        access = await self._access(
            conn, principal, operation, project_id, write=True, claim_epoch=claim_epoch
        )
        current = await self._revision(record, conn=conn)
        access.editable(record, current["snapshot"])
        self._precondition(if_revision, current["revision_id"])
        snapshot, changes = deepcopy(current["snapshot"]), []
        if operation == "knowledge_update":
            if snapshot["lifecycle"] != "active":
                raise RecordError("record.forbidden", "Restore before editing retired knowledge")
            snapshot.update(arguments)
            # A generated summary cannot survive a changed original unless a
            # new explicit summary/input pair is supplied and authorized.
            original_fields = EDIT_FIELDS - {"summary", "summary_of_revision", "change_reason"}
            if (
                current["snapshot"]["summary_of_revision"]
                and any(snapshot[k] != current["snapshot"][k] for k in original_fields)
                and not {"summary", "summary_of_revision"} <= arguments.keys()
            ):
                snapshot.update(summary=None, summary_of_revision=None)
        elif operation == "knowledge_retire":
            snapshot.update(
                arguments,
                lifecycle="retired",
                verification="unverified",
                last_verified_at=None,
                last_verified_by=None,
            )
            if arguments["successor_record_id"]:
                successor = await self.resolve_on(
                    f"record:{arguments['successor_record_id']}", access, conn=conn
                )
                if successor["kind"] != "knowledge" or not await conn.scalar(
                    select(record_link_heads.c.link_id)
                    .join(
                        record_link_versions,
                        (record_link_versions.c.link_id == record_link_heads.c.link_id)
                        & (record_link_versions.c.version == record_link_heads.c.current_version),
                    )
                    .where(
                        record_link_heads.c.source_record_id == successor["record_id"],
                        record_link_versions.c.target_record_id == record["record_id"],
                        record_link_versions.c.link_type == "supersedes",
                        record_link_versions.c.removed.is_(False),
                    )
                    .limit(1)
                ):
                    raise RecordError("record.invalid_link", "Successor must supersede this record")
        else:
            historical = await self._revision(record, arguments["revision_id"], conn=conn)
            snapshot = deepcopy(historical["snapshot"])
            snapshot.update(
                lifecycle="active",
                retirement_reason=None,
                successor_record_id=None,
                verification="unverified",
                last_verified_at=None,
                last_verified_by=None,
                change_reason=arguments["reason"],
            )
            await self._validate_snapshot_access(snapshot, access, conn=conn)
            wanted = {link["link_id"]: link for link in snapshot["outgoing_links"]}
            operations = [
                {"action": "remove", "link_id": link["link_id"]}
                for link in current["snapshot"]["outgoing_links"]
                if link["link_id"] not in wanted
            ]
            operations.extend(
                dict(
                    action="update",
                    link_id=link["link_id"],
                    link_type=link["link_type"],
                    target=f"record:{link['target_record_id']}",
                    target_revision_id=link["target_revision_id"],
                    metadata=link["metadata"],
                )
                for link in wanted.values()
            )
            # Restore can copy a large unchanged set, but changes still respect
            # the 100-operation mutation bound.
            active, changes = await self._plan_links(record, operations, access, conn=conn)
            if len(changes) > 100:
                raise RecordError("record.invalid_input", "Restore changes more than 100 links")
            snapshot["outgoing_links"] = active
        result = await self._append_snapshot(
            record,
            current,
            snapshot,
            access,
            operation,
            changes=changes,
            conn=conn,
            force_revision=operation == "knowledge_restore",
        )
        await self._access(
            conn, principal, operation, project_id, write=True, claim_epoch=claim_epoch
        )
        await self.db.finish_record_request_on(receipt, result, conn=conn)
        return result

    async def search(self, **kwargs):
        return await self._transaction(lambda conn: self.search_on(conn=conn, **kwargs))

    async def search_on(
        self,
        *,
        principal,
        project_id,
        query="",
        category=None,
        include_retired=False,
        include_disputed=False,
        limit=25,
        cursor=None,
        conn,
    ):
        from src.knowledge.search import lexical_search_on

        access = await self._access(conn, principal, "knowledge_search", project_id)
        return await lexical_search_on(
            conn,
            access=access,
            query=query,
            category=category,
            include_retired=include_retired,
            include_disputed=include_disputed,
            limit=limit,
            cursor=cursor,
        )
