"""Exact-base correction proposals; acceptance is one guarded transaction."""

from copy import deepcopy
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import insert, select, update

from src.database.tables import knowledge_proposals
from src.knowledge.models import content_hash, normalize_snapshot
from src.records.identity import knowledge_identity
from src.records.models import RecordError, uuid_value


def proposal_hash(record_id, base_revision_id, snapshot):
    return content_hash(
        dict(
            record_id=str(record_id) if record_id else None,
            base_revision_id=str(base_revision_id) if base_revision_id else None,
            snapshot=snapshot,
        )
    )


async def reauthorize_proposal(service, conn, proposal_id, access, *, lock=False):
    stmt = select(knowledge_proposals).where(
        knowledge_proposals.c.proposal_id == uuid_value(proposal_id, "proposal_id"),
        knowledge_proposals.c.scope_key == access.scope_key,
    )
    if lock:
        stmt = stmt.with_for_update()
    row = (await conn.execute(stmt)).mappings().first()
    if not row or (not access.supervisor and row["actor_id"] != access.actor_key):
        raise RecordError("record.not_found")
    if row["redacted_at"]:
        raise RecordError("record.revision_redacted")
    if row["record_id"]:
        record = await service.resolve_on(f"record:{row['record_id']}", access, conn=conn)
        await service._revision(record, row["base_revision_id"], conn=conn)
    await service._validate_snapshot_access(row["proposed_snapshot"], access, conn=conn)
    if row["resulting_record_id"]:
        record = await service.resolve_on(f"record:{row['resulting_record_id']}", access, conn=conn)
        await service._revision(record, row["resulting_revision_id"], conn=conn)
    return dict(row)


class ProposalMixin:
    async def propose(self, **kwargs):
        return await self._transaction(lambda conn: self.propose_on(conn=conn, **kwargs))

    async def propose_on(
        self,
        snapshot,
        *,
        principal,
        project_id,
        idempotency_key,
        identity=None,
        if_revision=None,
        link_operations=None,
        claim_epoch=None,
        conn,
    ):
        if_revision = self._optional_token(if_revision, "if_revision")
        access = await self._access(
            conn, principal, "knowledge_propose", project_id, write=True, claim_epoch=claim_epoch
        )
        proposed = normalize_snapshot(snapshot)
        if (
            proposed["verification"] != "unverified"
            or proposed["lifecycle"] != "active"
            or proposed["successor_record_id"]
            or proposed["retirement_reason"]
        ):
            raise RecordError("record.invalid_input", "Proposals contain active unverified content")
        record = await self.resolve_on(identity, access, conn=conn) if identity else None
        if record and record["scope_key"] != access.scope_key:
            raise RecordError("record.forbidden")
        if not record and (if_revision or proposed["outgoing_links"] or link_operations):
            raise RecordError("record.invalid_input", "New proposals cannot carry a base or links")
        operations = self._normalize_operations(link_operations) if link_operations else []
        receipt, replay = await self._receipt(
            conn,
            access,
            "knowledge_propose",
            idempotency_key,
            dict(
                identity=identity, snapshot=proposed, if_revision=if_revision, operations=operations
            ),
        )
        if replay:
            return replay
        base = None
        if record:
            await self._lock_source(record, conn=conn)
            current = await self._revision(record, conn=conn)
            self._precondition(if_revision, current["revision_id"])
            base = current["revision_id"]
            if proposed["outgoing_links"] != current["snapshot"]["outgoing_links"]:
                raise RecordError("record.invalid_input", "Change links with link_operations")
            if operations:
                proposed["outgoing_links"], _ = await self._plan_links(
                    record,
                    operations,
                    access,
                    conn=conn,
                )
        await self._validate_snapshot_access(proposed, access, conn=conn)
        pid = uuid4()
        digest = proposal_hash(record["record_id"] if record else None, base, proposed)
        await conn.execute(
            insert(knowledge_proposals).values(
                proposal_id=pid,
                record_id=record["record_id"] if record else None,
                base_revision_id=base,
                scope_key=receipt["scope_key"],
                proposed_snapshot=proposed,
                source_descriptors=proposed["sources"],
                content_sha256=digest,
                actor_id=access.actor_key,
            )
        )
        await self._access(
            conn, principal, "knowledge_propose", project_id, write=True, claim_epoch=claim_epoch
        )
        result = dict(
            success=True,
            outcome="created",
            proposal_id=str(pid),
            proposal_sha256=digest,
            state="pending",
        )
        await self.db.finish_record_request_on(receipt, result, conn=conn)
        return result

    async def proposal_show(self, **kwargs):
        return await self._transaction(lambda conn: self.proposal_show_on(conn=conn, **kwargs))

    async def proposal_show_on(self, proposal_id, *, principal, project_id, conn):
        access = await self._access(conn, principal, "knowledge_proposal_show", project_id)
        row = await reauthorize_proposal(self, conn, proposal_id, access)
        return dict(
            success=True,
            outcome="read",
            proposal_id=str(row["proposal_id"]),
            proposal_sha256=row["content_sha256"],
            state=row["state"],
            base_revision_id=str(row["base_revision_id"]) if row["base_revision_id"] else None,
            snapshot=await self._safe_snapshot(row["proposed_snapshot"], access, conn=conn),
        )

    async def proposal_decide(self, **kwargs):
        return await self._transaction(lambda conn: self.proposal_decide_on(conn=conn, **kwargs))

    async def proposal_decide_on(
        self,
        proposal_id,
        decision,
        *,
        principal,
        project_id,
        proposal_sha256,
        if_revision=None,
        idempotency_key,
        reason,
        conn,
    ):
        if_revision = self._optional_token(if_revision, "if_revision")
        access = await self._access(
            conn, principal, "knowledge_proposal_decide", project_id, write=True
        )
        if not access.supervisor:
            raise RecordError("record.forbidden")
        self._reason(reason)
        if decision not in {"accept", "reject"}:
            raise RecordError("record.invalid_input")
        pid = uuid_value(proposal_id, "proposal_id")
        await reauthorize_proposal(self, conn, pid, access)
        receipt, replay = await self._receipt(
            conn,
            access,
            "knowledge_proposal_decide",
            idempotency_key,
            dict(
                proposal_id=str(pid),
                decision=decision,
                proposal_sha256=proposal_sha256,
                if_revision=if_revision,
                reason=reason,
            ),
        )
        if replay:
            return replay
        row = await reauthorize_proposal(self, conn, pid, access, lock=True)
        if row["content_sha256"] != proposal_sha256:
            raise RecordError("knowledge.proposal_conflict")
        if row["base_revision_id"]:
            self._precondition(if_revision, row["base_revision_id"])
        elif if_revision is not None:
            raise RecordError("record.invalid_input")
        if row["state"] != "pending":
            if row["state"] == "accepted" and decision == "accept":
                record = await self.resolve_on(
                    f"record:{row['resulting_record_id']}", access, conn=conn
                )
                revision = await self._revision(record, row["resulting_revision_id"], conn=conn)
                result = self._knowledge_result(record, revision, "replayed")
            elif row["state"] == "rejected" and decision == "reject" or row["state"] == "stale":
                result = dict(success=True, outcome="replayed")
            else:
                raise RecordError("knowledge.proposal_decided")
            result.update(proposal_id=str(pid), state=row["state"])
            await self.db.finish_record_request_on(receipt, result, conn=conn)
            return result
        state, result = "rejected", dict(success=True, outcome="updated")
        record = current = None
        if row["record_id"]:
            record = await self.resolve_on(f"record:{row['record_id']}", access, conn=conn)
            await self._lock_source(record, conn=conn)
            current = await self._revision(record, conn=conn)
        if decision == "accept":
            state = "accepted"
            if current and current["revision_id"] != row["base_revision_id"]:
                state = "stale"
            else:
                await self._access(
                    conn, principal, "knowledge_proposal_decide", project_id, write=True
                )
                proposed = deepcopy(row["proposed_snapshot"])
                if record is None:
                    rid, alias = knowledge_identity()
                    record = dict(
                        record_id=rid,
                        kind="knowledge",
                        scope_key=access.scope_key,
                        knowledge_alias=alias,
                        created_by=row["actor_id"],
                    )
                    await self.db.insert_record_on(record, conn=conn)
                changes = []
                if current:
                    old = {v["link_id"]: v for v in current["snapshot"]["outgoing_links"]}
                    wanted = {v["link_id"]: v for v in proposed["outgoing_links"]}
                    changes.extend(
                        {**v, "version": v["version"] + 1, "removed": True}
                        for lid, v in old.items()
                        if lid not in wanted
                    )
                    changes.extend(
                        {**v, "removed": False} for lid, v in wanted.items() if v != old.get(lid)
                    )
                result = await self._append_snapshot(
                    record,
                    current,
                    proposed,
                    access,
                    "knowledge_proposal_decide",
                    changes=changes,
                    force_revision=True,
                    conn=conn,
                )
        values = dict(state=state, decided_at=datetime.now(UTC), decided_by=access.actor_key)
        if state == "accepted":
            values.update(
                resulting_record_id=UUID(result["record_id"]),
                resulting_revision_id=UUID(result["revision_id"]),
            )
        await conn.execute(
            update(knowledge_proposals)
            .where(
                knowledge_proposals.c.proposal_id == pid,
            )
            .values(**values)
        )
        result.update(proposal_id=str(pid), state=state)
        await self.db.finish_record_request_on(receipt, result, conn=conn)
        return result
