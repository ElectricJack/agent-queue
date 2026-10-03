"""Reference-only optional ranking with real core PostgreSQL authorization."""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import insert, select, update

from src.commands.principal import TRUSTED_LOCAL
from src.config import MemoryConfig
from src.database.tables import (
    knowledge_revisions,
    record_source_artifacts,
    records,
    sessions,
    tasks,
)
from src.knowledge.providers import KnowledgeRetrieval, RetrievalReference
from src.knowledge.service import KnowledgeService
from src.profiles.capabilities import DENY_ALL
from src.records.auth import GLOBAL_SCOPE
from src.records.models import RecordError
from tests.record_helpers import knowledge_config, seed_project, snapshot, worker_principal

LOCAL = dict(principal=TRUSTED_LOCAL, project_id="p")


@pytest.fixture
async def service(reuse_database):
    db = await reuse_database()
    await seed_project(db)
    await seed_project(db, "q")
    config = knowledge_config(global_enabled=True)
    config.semantic.enabled = True
    return KnowledgeService(db, config)


async def create(service, key="record", **changes):
    return await service.create(snapshot=snapshot(**changes), idempotency_key=key, **LOCAL)


async def retained_evidence(service):
    artifact_id = uuid4()
    async with service.db.immediate() as conn:
        await conn.execute(
            insert(record_source_artifacts).values(
                artifact_id=artifact_id,
                scope_key="project:p",
                content_sha256="a" * 64,
                byte_size=5,
                media_type="text/plain",
                storage_key=f"record-artifacts/{artifact_id}",
            )
        )
    return [
        dict(source_id="observed", kind="artifact", artifact_id=str(artifact_id), sha256="a" * 64)
    ]


def pin(record, **changes):
    return {
        **dict(
            record_id=record["record_id"],
            revision_id=record["revision_id"],
            chunk_id="chunk-1",
            score=0.8,
            provider_version="offline-v1",
        ),
        **changes,
    }


def retrieval(service, candidates):
    provider = Mock(search=AsyncMock(return_value=candidates))
    return KnowledgeRetrieval(
        service, MemoryConfig(enabled=True), provider_lookup=lambda: provider, timeout_seconds=5
    ), provider


@pytest.mark.parametrize(
    "change",
    [
        {"record_id": "task:private"},
        {"revision_id": "current"},
        {"score": float("nan")},
        {"score": float("inf")},
        {"score": True},
        {"chunk_id": ""},
        {"chunk_id": "\nsecret"},
        {"chunk_id": "é" * 65},
        {"provider_version": "x" * 129},
        {"provider_version": " "},
        {"body": "plugin supplied text"},
        {"title": "plugin supplied title"},
        {"authority": "policy"},
        {"scope_key": "global"},
        {"verification": "verified"},
    ],
)
def test_reference_rejects_text_state_and_unbounded_or_invalid_ranking(change):
    value = pin(dict(record_id=str(uuid4()), revision_id=str(uuid4())))
    with pytest.raises(ValidationError):
        RetrievalReference.model_validate({**value, **change})


async def test_hydrates_core_metadata_in_provider_order_without_duplicate_chunks(service):
    first = await create(service, "first", title="First", summary="Core summary")
    second = await create(service, "second", title="Second")
    boundary, provider = retrieval(
        service, [pin(second), pin(second), pin(second, chunk_id="another-chunk"), pin(first)]
    )
    result = await boundary.search(query="no lexical match", limit=4, semantic=True, **LOCAL)
    assert result["retrieval"]["mode"] == "semantic"
    assert [item["title"] for item in result["items"]] == ["Second", "First"]
    assert result["items"][1]["summary"] == "Core summary"
    assert result["items"][0]["content_sha256"] == second["content_sha256"]
    assert not result["items"][0]["authoritative"]
    assert all("body" not in item and "snapshot" not in item for item in result["items"])
    request = provider.search.call_args.args[0]
    assert (request.scope_key, request.limit, request.include_shared_global) == (
        "project:p",
        4,
        True,
    )


async def test_reindex_cannot_substitute_exact_historical_evidence(service):
    original = await create(service, body="Exact old evidence α\r\n")
    current = await service.update(
        identity=f"record:{original['record_id']}",
        patch={"body": "Corrected evidence"},
        if_revision=original["revision_id"],
        idempotency_key="edit",
        **LOCAL,
    )
    boundary, provider = retrieval(service, [pin(original)])
    old = await boundary.read(pin(original), **LOCAL)
    assert old["snapshot"]["body"] == "Exact old evidence α\r\n"
    assert old["content_sha256"] == original["content_sha256"]
    provider.search.assert_not_awaited()
    fallback = await boundary.search(semantic=True, **LOCAL)
    assert fallback["retrieval"]["mode"] == "lexical"
    assert fallback["items"][0]["revision_id"] == current["revision_id"]
    provider.search.return_value = [
        {**pin(current), "chunk_id": "reindexed", "provider_version": "v2"}
    ]
    refreshed = await boundary.search(semantic=True, **LOCAL)
    assert refreshed["items"][0]["revision_id"] == current["revision_id"]
    again = await boundary.read({**pin(original), "chunk_id": "different-index"}, **LOCAL)
    assert (again["revision_id"], again["content_sha256"], again["snapshot"]) == (
        old["revision_id"],
        old["content_sha256"],
        old["snapshot"],
    )


async def test_private_unknown_foreign_revision_and_forged_text_are_dropped(service):
    public = await create(service)
    other = await create(service, "other")
    private = await service.create(
        snapshot=snapshot(title="PRIVATE TITLE", body="PRIVATE BODY"),
        idempotency_key="private",
        principal=TRUSTED_LOCAL,
        project_id="q",
    )
    boundary, _ = retrieval(service, [])
    values = [
        pin(private),
        {**pin(public), "revision_id": other["revision_id"]},
        {**pin(public), "body": "PLUGIN SECRET"},
        pin(dict(record_id=str(uuid4()), revision_id=str(uuid4()))),
        pin(public),
    ]
    items = await boundary.hydrate_references(values, **LOCAL)
    assert [item["record_id"] for item in items] == [public["record_id"]]
    assert "PRIVATE" not in str(items) and "PLUGIN SECRET" not in str(items)
    with pytest.raises(RecordError, match="record.not_found"):
        await boundary.read(pin(private), **LOCAL)


@pytest.mark.parametrize("change", ["retire", "dispute", "redact"])
async def test_provider_mutations_are_rechecked_before_hydration(service, change):
    record = await create(service)
    boundary, provider = retrieval(service, [])

    async def changed(_request):
        args = dict(
            identity=f"record:{record['record_id']}",
            if_revision=record["revision_id"],
            idempotency_key=change,
            **LOCAL,
        )
        if change == "retire":
            result = await service.retire(reason="Obsolete", **args)
        elif change == "dispute":
            result = await service.verify(
                verification="disputed",
                reason="Conflicting observation",
                evidence=await retained_evidence(service),
                **args,
            )
        else:
            await service.redact(reason_code="sensitive", dry_run=False, **args)
            result = record
        return [pin(result)]

    provider.search.side_effect = changed
    result = await boundary.search(semantic=True, **LOCAL)
    assert result["items"] == []
    assert result["retrieval"]["mode"] == "lexical"
    if change == "redact":
        with pytest.raises(RecordError, match="record.revision_redacted"):
            await boundary.read(pin(record), **LOCAL)
    else:
        assert (await boundary.read(pin(record), **LOCAL))["snapshot"][
            "body"
        ] == "Retained evidence.\n"


async def test_shared_global_references_reauthorize_after_share_revocation(service):
    worker = await worker_principal(service.db)
    args = dict(principal=worker, project_id="p")
    global_args = dict(principal=TRUSTED_LOCAL, project_id=GLOBAL_SCOPE)
    record = await service.create(
        snapshot=snapshot(title="Shared global"), idempotency_key="global", **global_args
    )
    boundary, provider = retrieval(service, [pin(record)])
    assert (await boundary.search(semantic=True, **args))["items"] == []
    sharing = dict(
        identity=f"record:{record['record_id']}",
        target_project_id="p",
        if_revision=record["revision_id"],
        reason="Share evidence",
        dry_run=False,
        **global_args,
    )
    await service.share(idempotency_key="share", **sharing)
    assert (await boundary.search(semantic=True, **args))["items"][0]["record_id"] == record[
        "record_id"
    ]

    async def revoked(_request):
        await service.share(idempotency_key="revoke", revoke=True, **sharing)
        return [pin(record)]

    provider.search.side_effect = revoked
    assert (await boundary.search(semantic=True, **args))["items"] == []
    with pytest.raises(RecordError, match="record.not_found"):
        await boundary.read(pin(record), **args)


async def test_exact_read_filters_private_task_sources_and_links(service):
    worker = await worker_principal(service.db)
    other = await worker_principal(service.db, name="other")
    record = await create(
        service, sources=[dict(source_id="private-task", kind="task", task_id=other.task_id)]
    )
    record = await service.mutate_links(
        identity=f"record:{record['record_id']}",
        operations=[dict(action="add", link_type="references", target=f"task:{other.task_id}")],
        if_revision=record["revision_id"],
        idempotency_key="link",
        **LOCAL,
    )
    boundary, _ = retrieval(service, [])
    shown = await boundary.read(pin(record), principal=worker, project_id="p")
    assert shown["snapshot"]["sources"] == shown["snapshot"]["outgoing_links"] == []
    assert other.task_id not in str(shown)


@pytest.mark.parametrize("change", ["principal-grants", "session-ended", "project-scope"])
async def test_authorization_precedes_optional_lookup(service, change):
    worker = await worker_principal(service.db)
    if change == "principal-grants":
        worker = replace(worker, policy=DENY_ALL)
    if change == "session-ended":
        async with service.db.immediate() as conn:
            await conn.execute(
                update(sessions).where(sessions.c.id == worker.session_id).values(ended_at=2)
            )
    lookup = Mock(side_effect=AssertionError("unauthorized initialization"))
    boundary = KnowledgeRetrieval(service, MemoryConfig(enabled=True), provider_lookup=lookup)
    with pytest.raises(RecordError):
        await boundary.search(
            principal=worker, project_id="q" if change == "project-scope" else "p", semantic=True
        )
    lookup.assert_not_called()


async def test_provider_work_releases_connection_and_never_writes_tasks_or_knowledge(
    service, monkeypatch
):
    worker = await worker_principal(service.db)
    record = await create(service)
    boundary, provider = retrieval(service, [pin(record)])
    async with service.db.immediate() as conn:
        task_before = (
            (await conn.execute(select(tasks).where(tasks.c.id == worker.task_id))).mappings().one()
        )
        records_before = (await conn.execute(select(records))).mappings().all()
        revisions_before = (await conn.execute(select(knowledge_revisions))).mappings().all()

    transaction_active = False
    transaction = service._transaction

    async def tracked_transaction(callback):
        nonlocal transaction_active
        transaction_active = True
        try:
            return await transaction(callback)
        finally:
            transaction_active = False

    monkeypatch.setattr(service, "_transaction", tracked_transaction)

    async def no_connection(_request):
        assert not transaction_active
        return [pin(record)]

    provider.search.side_effect = no_connection
    result = await boundary.search(principal=worker, project_id="p", semantic=True)
    assert result["retrieval"]["mode"] == "semantic"
    async with service.db.immediate() as conn:
        assert (
            await conn.execute(select(tasks).where(tasks.c.id == worker.task_id))
        ).mappings().one() == task_before
        assert (await conn.execute(select(records))).mappings().all() == records_before
        assert (
            await conn.execute(select(knowledge_revisions))
        ).mappings().all() == revisions_before


async def test_cancelled_provider_search_propagates_cancellation(service):
    boundary, provider = retrieval(service, [])
    provider.search.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await boundary.search(semantic=True, **LOCAL)


async def test_boundary_revalidates_constructed_models_and_bounds_batch(service):
    record = await create(service)
    boundary, _ = retrieval(service, [])
    invalid = RetrievalReference.model_construct(**{**pin(record), "score": float("nan")})
    assert await boundary.hydrate_references([invalid], **LOCAL) == []
    with pytest.raises(RecordError) as exc:
        await boundary.hydrate_references([pin(record)] * 101, **LOCAL)
    assert exc.value.code == "record.invalid_input"
    with pytest.raises(RecordError) as exc:
        await boundary.read({**pin(record), "title": "not core"}, **LOCAL)
    assert exc.value.code == "record.invalid_input"


async def test_authority_revocation_during_ranking_uses_current_core_annotation(service):
    record = await create(service, category="policy")
    record = await service.verify(
        identity=f"record:{record['record_id']}",
        if_revision=record["revision_id"],
        idempotency_key="verify",
        reason="Named evidence",
        evidence=await retained_evidence(service),
        **LOCAL,
    )
    service.config.authority_review_required = False
    args = dict(
        identity=f"record:{record['record_id']}",
        if_revision=record["revision_id"],
        reason="Explicit policy decision",
        **LOCAL,
    )
    await service.authority_grant(idempotency_key="grant", **args)
    boundary, provider = retrieval(service, [pin(record)])
    assert (await boundary.search(semantic=True, **LOCAL))["items"][0]["authoritative"]

    async def revoked(_request):
        await service.authority_revoke(idempotency_key="revoke", **args)
        return [pin(record)]

    provider.search.side_effect = revoked
    result = await boundary.search(semantic=True, **LOCAL)
    assert result["items"][0]["verification"] == "verified"
    assert not result["items"][0]["authoritative"]


async def test_session_revocation_during_ranking_cannot_serve_previous_access(service):
    worker = await worker_principal(service.db)
    record = await create(service)
    boundary, provider = retrieval(service, [])

    async def ended(_request):
        async with service.db.immediate() as conn:
            await conn.execute(
                update(sessions).where(sessions.c.id == worker.session_id).values(ended_at=2)
            )
        return [pin(record)]

    provider.search.side_effect = ended
    with pytest.raises(RecordError, match="record.forbidden"):
        await boundary.search(principal=worker, project_id="p", semantic=True)
