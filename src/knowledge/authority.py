"""Verification and revocable policy annotations bound to exact evidence."""

import hashlib
import re
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import insert, select, update

from src.database.tables import doc_review_revisions, doc_reviews, knowledge_authority_grants
from src.knowledge.models import normalize_snapshot, utc_text
from src.records.models import RecordError


def review_binding(record, revision):
    return (
        f"knowledge-authority: {record['record_id']} {revision['revision_id']} "
        f"{revision['content_sha256']}"
    )


async def approved_review(conn, record, revision, proof, *, lock=False):
    if not isinstance(proof, dict) or set(proof) != {"review_id", "revision", "sha256"}:
        raise RecordError("knowledge.review_required")
    if (
        not isinstance(proof["review_id"], str)
        or not proof["review_id"]
        or type(proof["revision"]) is not int
        or proof["revision"] < 1
        or not isinstance(proof["sha256"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", proof["sha256"])
    ):
        raise RecordError("record.invalid_input", "Invalid review evidence")
    stmt = select(doc_reviews).where(doc_reviews.c.id == proof["review_id"])
    if lock:
        stmt = stmt.with_for_update()
    review = (await conn.execute(stmt)).mappings().first()
    if not review or f"project:{review['project_id']}" != record["scope_key"]:
        raise RecordError("record.not_found")
    retained = (
        (
            await conn.execute(
                select(doc_review_revisions).where(
                    doc_review_revisions.c.review_id == proof["review_id"],
                    doc_review_revisions.c.revision == proof["revision"],
                )
            )
        )
        .mappings()
        .first()
    )
    if (
        not retained
        or review["state"] != "approved"
        or review["current_revision"] != proof["revision"]
        or retained["content_sha256"] != proof["sha256"]
        or hashlib.sha256(retained["content"].encode()).hexdigest() != proof["sha256"]
        or review_binding(record, revision) not in retained["content"].splitlines()
    ):
        raise RecordError("knowledge.review_binding_invalid")


async def authority_on(conn, record, revision, *, review_required=True):
    """Read-time truth, including independent review revocation and head changes."""
    from src.database.tables import knowledge_records

    if (
        revision["snapshot"] is None
        or revision["snapshot"]["verification"] != "verified"
        or revision["snapshot"]["lifecycle"] != "active"
    ):
        return None
    current = await conn.scalar(
        select(knowledge_records.c.current_revision_id).where(
            knowledge_records.c.record_id == record["record_id"],
        )
    )
    if current != revision["revision_id"]:
        return None
    grant = (
        (
            await conn.execute(
                select(knowledge_authority_grants).where(
                    knowledge_authority_grants.c.record_id == record["record_id"],
                    knowledge_authority_grants.c.revision_id == revision["revision_id"],
                    knowledge_authority_grants.c.revoked_at.is_(None),
                )
            )
        )
        .mappings()
        .first()
    )
    if not grant:
        return None
    if grant["review_id"]:
        try:
            await approved_review(
                conn,
                record,
                revision,
                dict(
                    review_id=grant["review_id"],
                    revision=grant["review_revision"],
                    sha256=grant["review_sha256"],
                ),
            )
        except RecordError:
            return None
    elif review_required:
        return None
    # Do not expose retained review content/reasons through an annotation.
    return dict(
        kind="policy", grant_id=str(grant["grant_id"]), revision_id=str(revision["revision_id"])
    )


class AuthorityMixin:
    @staticmethod
    def _reason(reason):
        if not isinstance(reason, str) or not reason.strip() or len(reason.encode()) > 4096:
            raise RecordError(
                "record.invalid_input", "A nonempty reason of at most 4096 bytes is required"
            )

    async def verify(self, **kwargs):
        return await self._transaction(lambda conn: self.verify_on(conn=conn, **kwargs))

    async def verify_on(
        self,
        identity,
        *,
        principal,
        project_id,
        evidence,
        reason,
        verification="verified",
        if_revision=None,
        idempotency_key,
        conn,
    ):
        if_revision = self._optional_token(if_revision, "if_revision")
        access = await self._access(conn, principal, "knowledge_verify", project_id, write=True)
        if not access.supervisor:
            raise RecordError("record.forbidden")
        self._reason(reason)
        if verification not in {"verified", "disputed"} or not evidence:
            raise RecordError("record.invalid_input", "Verification requires named evidence")
        record = await self.resolve_on(identity, access, conn=conn)
        if record["scope_key"] != access.scope_key:
            raise RecordError("record.forbidden")
        receipt, replay = await self._receipt(
            conn,
            access,
            "knowledge_verify",
            idempotency_key,
            dict(
                record_id=str(record["record_id"]),
                if_revision=if_revision,
                evidence=evidence,
                reason=reason,
                verification=verification,
            ),
        )
        if replay:
            return replay
        await self._lock_source(record, conn=conn)
        current = await self._revision(record, conn=conn)
        self._precondition(if_revision, current["revision_id"])
        if current["snapshot"]["lifecycle"] != "active":
            raise RecordError("record.forbidden")
        sources = {s["source_id"]: s for s in current["snapshot"]["sources"]}
        for source in evidence:
            if not isinstance(source, dict) or not source.get("source_id"):
                raise RecordError("record.invalid_input")
            sources[source["source_id"]] = source
        snapshot = normalize_snapshot(
            {
                **current["snapshot"],
                "sources": list(sources.values()),
                "verification": verification,
                "last_verified_at": utc_text(datetime.now(UTC)),
                "last_verified_by": access.actor_key,
                "change_reason": reason,
            }
        )
        await self._access(conn, principal, "knowledge_verify", project_id, write=True)
        result = await self._append_snapshot(
            record, current, snapshot, access, "knowledge_verify", force_revision=True, conn=conn
        )
        await self.db.finish_record_request_on(receipt, result, conn=conn)
        return result

    async def authority_grant(self, **kwargs):
        return await self._transaction(
            lambda conn: self.authority_change_on(conn=conn, revoke=False, **kwargs)
        )

    async def authority_revoke(self, **kwargs):
        return await self._transaction(
            lambda conn: self.authority_change_on(conn=conn, revoke=True, **kwargs)
        )

    async def authority_change_on(
        self,
        identity,
        *,
        principal,
        project_id,
        reason,
        if_revision=None,
        idempotency_key,
        review=None,
        revoke,
        conn,
    ):
        if_revision = self._optional_token(if_revision, "if_revision")
        operation = "knowledge_authority_revoke" if revoke else "knowledge_authority_grant"
        access = await self._access(conn, principal, operation, project_id, write=True)
        if not access.supervisor:
            raise RecordError("record.forbidden")
        self._reason(reason)
        record = await self.resolve_on(identity, access, conn=conn)
        if record["scope_key"] != access.scope_key:
            raise RecordError("record.forbidden")
        receipt, replay = await self._receipt(
            conn,
            access,
            operation,
            idempotency_key,
            dict(
                record_id=str(record["record_id"]),
                if_revision=if_revision,
                reason=reason,
                review=review,
            ),
        )
        if replay:
            return replay
        await self._lock_source(record, conn=conn)
        current = await self._revision(record, conn=conn)
        self._precondition(if_revision, current["revision_id"])
        if not revoke:
            if (
                current["snapshot"]["verification"] != "verified"
                or current["snapshot"]["lifecycle"] != "active"
            ):
                raise RecordError("knowledge.verification_required")
            if self.config.authority_review_required or review:
                await approved_review(conn, record, current, review, lock=True)
        now = datetime.now(UTC)
        await conn.execute(
            update(knowledge_authority_grants)
            .where(
                knowledge_authority_grants.c.record_id == record["record_id"],
                knowledge_authority_grants.c.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        grant_id = uuid4()
        if not revoke:
            await conn.execute(
                insert(knowledge_authority_grants).values(
                    grant_id=grant_id,
                    record_id=record["record_id"],
                    revision_id=current["revision_id"],
                    scope_key=record["scope_key"],
                    actor_id=access.actor_key,
                    reason=reason,
                    review_id=review["review_id"] if review else None,
                    review_revision=review["revision"] if review else None,
                    review_sha256=review["sha256"] if review else None,
                )
            )
        result = {
            **self._knowledge_result(record, current, "updated"),
            "authority": None if revoke else dict(kind="policy", grant_id=str(grant_id)),
        }
        await self.db.finish_record_request_on(receipt, result, conn=conn)
        return result
