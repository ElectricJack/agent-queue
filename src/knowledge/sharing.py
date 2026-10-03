"""Explicit global shares; no cross-project grants or implicit public scope."""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import exists, insert, select, update

from src.database.tables import (
    knowledge_global_shares,
    knowledge_revision_payloads,
    knowledge_revisions,
    projects,
)
from src.records.auth import GLOBAL_SCOPE
from src.records.models import RecordError


def visible_scope(access, table):
    own = table.c.scope_key == access.scope_key
    if not access.global_enabled or access.project_id is GLOBAL_SCOPE:
        return own
    return own | (
        (table.c.scope_key == "global")
        & exists().where(
            knowledge_global_shares.c.record_id == table.c.record_id,
            knowledge_global_shares.c.project_id == access.project_id,
            knowledge_global_shares.c.revoked_at.is_(None),
        )
    )


class SharingMixin:
    async def share(self, **kwargs):
        return await self._transaction(lambda conn: self.share_on(conn=conn, **kwargs))

    async def share_on(
        self,
        identity,
        *,
        principal,
        project_id,
        target_project_id,
        if_revision=None,
        idempotency_key,
        reason,
        revoke=False,
        dry_run=True,
        conn,
    ):
        if_revision = self._optional_token(if_revision, "if_revision")
        access = await self._access(conn, principal, "knowledge_share", project_id, write=True)
        if not access.supervisor or project_id is not GLOBAL_SCOPE:
            raise RecordError("knowledge.cross_project_forbidden")
        self._reason(reason)
        if not await conn.scalar(select(projects.c.id).where(projects.c.id == target_project_id)):
            raise RecordError("record.not_found")
        record = await self.resolve_on(identity, access, conn=conn)
        if record["kind"] != "knowledge" or record["scope_key"] != "global":
            raise RecordError("knowledge.cross_project_forbidden")
        receipt, replay = await self._receipt(
            conn,
            access,
            "knowledge_share",
            idempotency_key,
            dict(
                record_id=str(record["record_id"]),
                target_project_id=target_project_id,
                if_revision=if_revision,
                revoke=revoke,
                reason=reason,
                dry_run=dry_run,
            ),
        )
        if replay:
            return replay
        await self._lock_source(record, conn=conn)
        current = await self._revision(record, conn=conn)
        self._precondition(if_revision, current["revision_id"])
        if not revoke:
            # Global snapshots accept only global retained sources. Recheck all
            # served history and every target's recipient grant at sharing time.
            rows = (
                await conn.execute(
                    select(knowledge_revision_payloads.c.snapshot)
                    .join(
                        knowledge_revisions,
                        knowledge_revisions.c.revision_id
                        == knowledge_revision_payloads.c.revision_id,
                    )
                    .where(
                        knowledge_revisions.c.record_id == record["record_id"],
                        knowledge_revision_payloads.c.snapshot.is_not(None),
                    )
                )
            ).scalars()
            for snapshot in rows:
                await self._validate_snapshot_access(snapshot, access, conn=conn)
                await validate_share_targets(conn, snapshot, [target_project_id])
        now = datetime.now(UTC)
        if not dry_run:
            active = knowledge_global_shares.c
            await conn.execute(
                update(knowledge_global_shares)
                .where(
                    active.record_id == record["record_id"],
                    active.project_id == target_project_id,
                    active.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )
            if not revoke:
                await conn.execute(
                    insert(knowledge_global_shares).values(
                        grant_id=uuid4(),
                        record_id=record["record_id"],
                        project_id=target_project_id,
                        actor_id=access.actor_key,
                    )
                )
        result = {
            **self._knowledge_result(record, current, "unchanged" if dry_run else "updated"),
            "dry_run": dry_run,
            "shared": not revoke,
        }
        await self.db.finish_record_request_on(receipt, result, conn=conn)
        return result


async def validate_share_targets(conn, snapshot, recipients):
    for link in snapshot["outgoing_links"]:
        for project in recipients:
            from src.records.models import uuid_value

            if not await conn.scalar(
                select(knowledge_global_shares.c.grant_id).where(
                    knowledge_global_shares.c.record_id
                    == uuid_value(link["target_record_id"], "target"),
                    knowledge_global_shares.c.project_id == project,
                    knowledge_global_shares.c.revoked_at.is_(None),
                )
            ):
                raise RecordError(
                    "record.source_unavailable", "Target is not shared with recipient"
                )
