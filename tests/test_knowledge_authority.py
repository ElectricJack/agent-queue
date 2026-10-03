"""Exact review bindings, revocation and global/source confidentiality."""

import hashlib
from uuid import uuid4

import pytest
from sqlalchemy import insert, update

from src.commands.principal import TRUSTED_LOCAL
from src.database.tables import doc_review_revisions, doc_reviews, record_source_artifacts
from src.knowledge.authority import review_binding
from src.knowledge.service import KnowledgeService
from src.records.auth import GLOBAL_SCOPE
from src.records.models import RecordError
from tests.record_helpers import knowledge_config, seed_project, snapshot, worker_principal

LOCAL = dict(principal=TRUSTED_LOCAL, project_id="p")
GLOBAL = dict(principal=TRUSTED_LOCAL, project_id=GLOBAL_SCOPE)


@pytest.fixture
async def service(reuse_database):
    db = await reuse_database()
    await seed_project(db)
    return KnowledgeService(db, knowledge_config(global_enabled=True))


async def verified(service, *, scope=LOCAL):
    original = await service.create(
        snapshot=snapshot(category="policy"), idempotency_key="create", **scope
    )
    aid = uuid4()
    async with service.db.immediate() as conn:
        await conn.execute(
            insert(record_source_artifacts).values(
                artifact_id=aid,
                scope_key="global" if scope is GLOBAL else "project:p",
                content_sha256="f" * 64,
                byte_size=5,
                media_type="text/plain",
                storage_key=f"record-artifacts/{aid}",
            )
        )
    evidence = [dict(source_id="retained", kind="artifact", artifact_id=str(aid), sha256="f" * 64)]
    result = await service.verify(
        identity=f"record:{original['record_id']}",
        if_revision=original["revision_id"],
        evidence=evidence,
        reason="Compared retained evidence",
        idempotency_key="verify",
        **scope,
    )
    return original, result


async def approved(service, result, content=None):
    body = content if content is not None else review_binding(result, result)
    digest = hashlib.sha256(body.encode()).hexdigest()
    async with service.db.immediate() as conn:
        await conn.execute(
            insert(doc_reviews).values(
                id="rev-policy",
                project_id="p",
                title="Policy",
                kind="plan",
                state="approved",
                current_revision=1,
                vault_path="reviews/policy.md",
                created_at=1,
                updated_at=1,
            )
        )
        await conn.execute(
            insert(doc_review_revisions).values(
                review_id="rev-policy",
                revision=1,
                content=body,
                content_sha256=digest,
                submitted_by="human:test",
                submitted_at=1,
            )
        )
    return dict(review_id="rev-policy", revision=1, sha256=digest)


def grant_args(result, proof=None, key="grant"):
    return dict(
        identity=f"record:{result['record_id']}",
        if_revision=result["revision_id"],
        idempotency_key=key,
        reason="Adopt procedure",
        review=proof,
        **LOCAL,
    )


async def test_review_must_explicitly_bind_record_revision_hash(service):
    _, result = await verified(service)
    with pytest.raises(RecordError, match="knowledge.review_required"):
        await service.authority_grant(**grant_args(result))
    proof = await approved(service, result, "An approved general design is insufficient.")
    with pytest.raises(RecordError, match="knowledge.review_binding_invalid"):
        await service.authority_grant(**grant_args(result, proof))


async def test_authority_revocation_is_checked_on_reads_and_exact_history(service):
    original, result = await verified(service)
    proof = await approved(service, result)
    await service.authority_grant(**grant_args(result, proof))
    current = await service.show(identity=f"record:{result['record_id']}", **LOCAL)
    assert current["authority"]["kind"] == "policy"
    previous = await service.show(
        identity=f"record:{result['record_id']}", revision_id=original["revision_id"], **LOCAL
    )
    assert previous["authority"] is None
    async with service.db.immediate() as conn:
        await conn.execute(
            update(doc_reviews).where(doc_reviews.c.id == "rev-policy").values(state="rejected")
        )
    current = await service.show(identity=f"record:{result['record_id']}", **LOCAL)
    assert current["authority"] is None
    assert current["snapshot"]["verification"] == "verified"


async def test_guarded_edit_resets_verification_and_revokes_grant(service):
    _, result = await verified(service)
    proof = await approved(service, result)
    await service.authority_grant(**grant_args(result, proof))
    worker = await worker_principal(service.db)
    with pytest.raises(RecordError, match="Protected revisions"):
        await service.update(
            identity=f"record:{result['record_id']}",
            patch={"body": "overwrite"},
            if_revision=result["revision_id"],
            idempotency_key="worker",
            principal=worker,
            project_id="p",
            claim_epoch=1,
        )
    edited = await service.update(
        identity=f"record:{result['record_id']}",
        patch={"body": "correction"},
        if_revision=result["revision_id"],
        idempotency_key="edit",
        **LOCAL,
    )
    current = await service.show(identity=f"record:{result['record_id']}", **LOCAL)
    assert current["revision_id"] == edited["revision_id"]
    assert current["authority"] is None
    assert current["snapshot"]["verification"] == "unverified"
    assert current["snapshot"]["last_verified_at"] is None


async def test_explicit_authority_without_review_when_policy_allows(service):
    service.config.authority_review_required = False
    _, result = await verified(service)
    await service.authority_grant(**grant_args(result))
    assert (await service.show(identity=f"record:{result['record_id']}", **LOCAL))["authority"]
    await service.authority_revoke(
        **{k: v for k, v in grant_args(result, key="revoke").items() if k != "review"}
    )
    assert (await service.show(identity=f"record:{result['record_id']}", **LOCAL))[
        "authority"
    ] is None


async def test_dispute_requires_privilege_and_named_evidence(service):
    _, result = await verified(service)
    worker = await worker_principal(service.db, grants=["knowledge_verify"])
    args = dict(
        identity=f"record:{result['record_id']}",
        if_revision=result["revision_id"],
        reason="Conflicting evidence",
        evidence=[dict(source_id="task", kind="task", task_id="task-worker")],
        verification="disputed",
        idempotency_key="dispute",
        **LOCAL,
    )
    with pytest.raises(RecordError, match="record.forbidden"):
        await service.verify(**{**args, "principal": worker})
    disputed = await service.verify(**args)
    assert disputed["sequence"] == 3
    assert (await service.search(**LOCAL))["items"] == []
    assert len((await service.search(include_disputed=True, **LOCAL))["items"]) == 1


async def test_global_share_revoke_search_history_and_link_read_authorization(service):
    worker = await worker_principal(service.db)
    record = await service.create(
        snapshot=snapshot(title="Shared procedure"), idempotency_key="global", **GLOBAL
    )
    identity = f"record:{record['record_id']}"
    with pytest.raises(RecordError, match="record.not_found"):
        await service.show(identity=identity, principal=worker, project_id="p")
    assert (await service.search(principal=worker, project_id="p"))["items"] == []
    args = dict(
        identity=identity,
        target_project_id="p",
        if_revision=record["revision_id"],
        reason="Publish global procedure",
        dry_run=False,
        idempotency_key="share",
        **GLOBAL,
    )
    await service.share(**args)
    assert len((await service.search(principal=worker, project_id="p"))["items"]) == 1
    assert (await service.show(identity=identity, principal=worker, project_id="p"))["snapshot"][
        "title"
    ] == "Shared procedure"
    with pytest.raises(RecordError, match="record.forbidden"):
        await service.update(
            identity=identity,
            patch={"body": "project edit"},
            if_revision=record["revision_id"],
            idempotency_key="edit",
            principal=worker,
            project_id="p",
            claim_epoch=1,
        )
    await service.share(**{**args, "revoke": True, "idempotency_key": "revoke"})
    for read in (service.show, service.history):
        with pytest.raises(RecordError, match="record.not_found"):
            await read(identity=identity, principal=worker, project_id="p")
    assert (await service.search(principal=worker, project_id="p"))["items"] == []


async def test_project_supervisor_cannot_use_global_or_cross_project_grants(service):
    supervisor = await worker_principal(
        service.db, elevated=True, grants=["knowledge_create", "knowledge_share"]
    )
    with pytest.raises(RecordError, match="record.not_found"):
        await service.create(
            snapshot=snapshot(),
            principal=supervisor,
            project_id=GLOBAL_SCOPE,
            idempotency_key="global",
        )
    record = await service.create(snapshot=snapshot(), idempotency_key="local", **LOCAL)
    with pytest.raises(RecordError, match="knowledge.cross_project_forbidden"):
        await service.share(
            identity=f"record:{record['record_id']}",
            target_project_id="q",
            if_revision=record["revision_id"],
            reason="Cross project",
            idempotency_key="share",
            principal=supervisor,
            project_id="p",
        )


async def test_global_private_sources_are_refused_and_global_links_are_recipient_checked(service):
    worker = await worker_principal(service.db)
    assert worker
    with pytest.raises(RecordError, match="Global evidence"):
        await service.create(
            snapshot=snapshot(
                sources=[dict(source_id="private", kind="task", task_id="task-worker")]
            ),
            idempotency_key="private",
            **GLOBAL,
        )
    _, global_verified = await verified(service, scope=GLOBAL)
    await service.share(
        identity=f"record:{global_verified['record_id']}",
        target_project_id="p",
        if_revision=global_verified["revision_id"],
        reason="Publish retained global evidence",
        idempotency_key="share",
        dry_run=False,
        **GLOBAL,
    )
    shown = await service.show(
        identity=f"record:{global_verified['record_id']}", principal=worker, project_id="p"
    )
    assert len(shown["snapshot"]["sources"]) == 1
    hidden = await service.create(
        snapshot=snapshot(title="Private global"), idempotency_key="hidden", **GLOBAL
    )
    with pytest.raises(RecordError, match="Target is not shared"):
        await service.mutate_links(
            identity=f"record:{global_verified['record_id']}",
            operations=[
                dict(action="add", target=f"record:{hidden['record_id']}", link_type="references")
            ],
            if_revision=global_verified["revision_id"],
            idempotency_key="link",
            **GLOBAL,
        )


async def test_grant_rechecks_review_under_lock_and_replay_observes_revocation(
    service, monkeypatch
):
    import asyncio
    import src.knowledge.authority as authority_module

    _, result = await verified(service)
    proof = await approved(service, result)
    entered = asyncio.Event()
    original_check = authority_module.approved_review

    async def observed(*args, **kwargs):
        entered.set()
        return await original_check(*args, **kwargs)

    monkeypatch.setattr(authority_module, "approved_review", observed)
    async with service.db.immediate() as conn:
        from sqlalchemy import select

        await conn.execute(
            select(doc_reviews).where(doc_reviews.c.id == "rev-policy").with_for_update()
        )
        pending = asyncio.create_task(service.authority_grant(**grant_args(result, proof)))
        await asyncio.wait_for(entered.wait(), timeout=2)
        await conn.execute(
            update(doc_reviews).where(doc_reviews.c.id == "rev-policy").values(state="rejected")
        )
    with pytest.raises(RecordError, match="knowledge.review_binding_invalid"):
        await pending
    async with service.db.immediate() as conn:
        await conn.execute(
            update(doc_reviews).where(doc_reviews.c.id == "rev-policy").values(state="approved")
        )
    await service.authority_grant(**grant_args(result, proof))
    await service.authority_revoke(
        **{k: v for k, v in grant_args(result, key="revoke").items() if k != "review"}
    )
    replay = await service.authority_grant(**grant_args(result, proof))
    assert replay["outcome"] == "replayed" and replay["authority"] is None


async def test_revoked_global_share_removes_inbound_task_link_metadata_and_retry(service):
    from src.records.service import RecordService

    worker = await worker_principal(service.db)
    global_record = await service.create(snapshot=snapshot(), idempotency_key="global", **GLOBAL)
    await service.share(
        identity=f"record:{global_record['record_id']}",
        target_project_id="p",
        if_revision=global_record["revision_id"],
        reason="Share",
        dry_run=False,
        idempotency_key="share",
        **GLOBAL,
    )
    records = RecordService(service.db, service.config)
    owned = await records.show(identity="task:task-worker", principal=worker, project_id="p")
    args = dict(
        identity="task:task-worker",
        principal=worker,
        project_id="p",
        claim_epoch=1,
        if_link_token=owned["link_token"],
        idempotency_key="link",
        operations=[
            dict(
                action="add",
                target=f"record:{global_record['record_id']}",
                link_type="references",
                metadata={"private.snippet": "private global"},
            )
        ],
    )
    await records.mutate_links(**args)
    await service.share(
        identity=f"record:{global_record['record_id']}",
        target_project_id="p",
        if_revision=global_record["revision_id"],
        reason="Revoke",
        dry_run=False,
        idempotency_key="revoke",
        revoke=True,
        **GLOBAL,
    )
    links = await records.list_links(identity="task:task-worker", principal=worker, project_id="p")
    assert links["links"] == []
    with pytest.raises(RecordError, match="record.not_found"):
        await records.mutate_links(**args)
