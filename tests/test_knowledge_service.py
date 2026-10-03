"""Real PostgreSQL races, canonical bytes, atomic receipts and lexical retrieval."""

import asyncio
import hashlib
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError

from src.commands.principal import TRUSTED_LOCAL
from src.database.tables import knowledge_revisions, record_outbox, record_requests, records
from src.knowledge.models import canonical_bytes, content_hash, normalize_snapshot
from src.knowledge.service import KnowledgeService
from src.records.models import RecordError
from tests.record_helpers import knowledge_config, seed_project, snapshot

LOCAL = dict(principal=TRUSTED_LOCAL, project_id="p")


@pytest.fixture
async def service(reuse_database):
    db = await reuse_database()
    await seed_project(db)
    return KnowledgeService(db, knowledge_config())


async def create(service, key="new", **changes):
    return await service.create(snapshot=snapshot(**changes), idempotency_key=key, **LOCAL)


def identity(result):
    return f"record:{result['record_id']}"


def test_canonical_golden_hash_preserves_unicode_and_newlines():
    doc = normalize_snapshot(
        snapshot(
            title="Café e\u0301",
            body="α\r\nβ\n",
            tags=[" z ", "a", "z"],
            valid_from="2026-10-01T03:00:00+03:00",
            metadata={"fixture.value": "é"},
        )
    )
    expected = (
        (
            '{"body":"α\\r\\nβ\\n","category":"incident","last_verified_at":null,'
            '"last_verified_by":null,"lifecycle":"active","metadata":{"fixture.value":"é"},'
            '"outgoing_links":[],"recheck_at":null,"retirement_reason":null,"sources":[], '
            '"successor_record_id":null,"summary":null,"summary_of_revision":null,'
            '"tags":["a","z"],"title":"Café é","valid_from":"2026-10-01T00:00:00.000000Z",'
            '"valid_until":null,"verification":"unverified"}'
        )
        .replace('[], "successor', '[],"successor')
        .encode()
    )
    assert canonical_bytes(doc) == expected
    assert content_hash(doc) == hashlib.sha256(expected).hexdigest()
    assert content_hash(doc) == "d6657bff61a79e6173466aac84b29082ceb019d0bbcef1a6750ac3b7c5133c69"


@pytest.mark.parametrize(
    "changes",
    [
        {"body": "é" * 131073},
        {"summary": "é" * 2049},
        {"title": ""},
        {"metadata": {"plain": 1}},
        {"metadata": {"aq.authority": True}},
        {"metadata": {"fixture.value": float("nan")}},
        {"metadata": {"fixture.value": [[[[[[[1]]]]]]]}},
        {"valid_from": "2026-10-02T00:00:00Z", "valid_until": "2026-10-01T00:00:00Z"},
        {"valid_from": "2026-10-01"},
        {"verification": "verified"},
        {"sources": [{"source_id": "one", "kind": "task"}]},
        {
            "sources": [
                {
                    "source_id": "one",
                    "kind": "url",
                    "url": "https://example.test",
                    "observed_at": "2026-10-01T00:00:00Z",
                    "retained": True,
                }
            ]
        },
        {"authority": "policy"},
        {"tags": ["x" * 65]},
        {"tags": [str(i) for i in range(33)]},
    ],
)
def test_snapshot_limits_before_sql(changes):
    with pytest.raises(RecordError, match=".") as exc:
        normalize_snapshot(snapshot(**changes))
    assert exc.value.code == "record.invalid_input"


async def test_replay_precedes_stale_precondition_and_mismatch_is_atomic(service):
    original = await create(service)
    args = dict(
        identity=identity(original),
        patch={"body": "changed"},
        if_revision=original["revision_id"],
        idempotency_key="edit",
        **LOCAL,
    )
    edited = await service.update(**args)
    replay = await service.update(**args)
    assert replay == {**edited, "outcome": "replayed"}
    with pytest.raises(RecordError) as exc:
        await service.update(**{**args, "patch": {"body": "different"}})
    assert exc.value.code == "record.idempotency_conflict"
    with pytest.raises(RecordError) as exc:
        await service.update(**{**args, "idempotency_key": "stale"})
    assert exc.value.code == "record.revision_conflict"
    assert exc.value.details == {"current_token": edited["revision_id"]}
    async with service.db.immediate() as conn:
        assert await conn.scalar(select(func.count()).select_from(knowledge_revisions)) == 2
        assert await conn.scalar(select(func.count()).select_from(record_requests)) == 2
        assert await conn.scalar(select(func.count()).select_from(record_outbox)) == 2


async def test_two_connections_one_cas_winner(service, monkeypatch):
    original = await create(service)
    barrier = asyncio.Barrier(2)
    original_lock = service._lock_source

    async def simultaneous(record, *, conn):
        await barrier.wait()
        return await original_lock(record, conn=conn)

    monkeypatch.setattr(service, "_lock_source", simultaneous)
    results = await asyncio.wait_for(
        asyncio.gather(
            *(
                service.update(
                    identity=identity(original),
                    patch={"body": str(i)},
                    if_revision=original["revision_id"],
                    idempotency_key=f"edit-{i}",
                    **LOCAL,
                )
                for i in range(2)
            ),
            return_exceptions=True,
        ),
        timeout=10,
    )
    winners = [r for r in results if isinstance(r, dict)]
    losers = [r for r in results if isinstance(r, RecordError)]
    assert len(winners) == len(losers) == 1, results
    assert losers[0].code == "record.revision_conflict"
    assert winners[0]["sequence"] == 2


async def test_concurrent_same_key_creates_one_record(service):
    results = await asyncio.gather(create(service), create(service))
    assert {result["outcome"] for result in results} == {"created", "replayed"}
    assert len({result["record_id"] for result in results}) == 1


async def test_noop_missing_precondition_and_corrected_retry(service):
    original = await create(service)
    with pytest.raises(RecordError) as exc:
        await service.update(identity=identity(original), patch={}, idempotency_key="edit", **LOCAL)
    assert exc.value.code == "record.precondition_required"
    result = await service.update(
        identity=identity(original),
        patch={},
        idempotency_key="edit",
        if_revision=original["revision_id"],
        **LOCAL,
    )
    assert result["outcome"] == "unchanged"
    assert result["revision_id"] == original["revision_id"]


async def test_caller_owned_transaction_rolls_back_all_effects(service):
    with pytest.raises(RuntimeError):
        async with service.db.immediate() as conn:
            await service.create_on(snapshot=snapshot(), idempotency_key="new", conn=conn, **LOCAL)
            raise RuntimeError("composition failed")
    async with service.db.immediate() as conn:
        for table in (records, knowledge_revisions, record_requests, record_outbox):
            assert await conn.scalar(select(func.count()).select_from(table)) == 0
    assert (await create(service))["outcome"] == "created"


async def test_outbox_failure_rolls_back_revision_and_receipt(service, monkeypatch):
    async def fail(*args, **kwargs):
        raise RuntimeError("outbox unavailable")

    monkeypatch.setattr(service, "_outbox", fail)
    with pytest.raises(RuntimeError):
        await create(service)
    async with service.db.immediate() as conn:
        assert await conn.scalar(select(func.count()).select_from(records)) == 0
        assert await conn.scalar(select(func.count()).select_from(record_requests)) == 0


async def test_exact_history_retire_restore_and_summary_invalidation(service):
    original = await create(service, body="Original\r\nα")
    summarized = await service.update(
        identity=identity(original),
        patch={"summary": "Generated", "summary_of_revision": original["revision_id"]},
        if_revision=original["revision_id"],
        idempotency_key="summary",
        **LOCAL,
    )
    edited = await service.update(
        identity=identity(original),
        patch={"body": "new"},
        if_revision=summarized["revision_id"],
        idempotency_key="edit",
        **LOCAL,
    )
    assert (await service.show(identity=identity(original), **LOCAL))["snapshot"]["summary"] is None
    exact = await service.show(
        identity=identity(original), revision_id=original["revision_id"], **LOCAL
    )
    assert exact["snapshot"]["body"] == "Original\r\nα"
    with pytest.raises(RecordError) as exc:
        await service.show(identity=identity(original), revision_id=str(uuid4()), **LOCAL)
    assert exc.value.code == "record.revision_unavailable"
    retired = await service.retire(
        identity=identity(original),
        reason="obsolete",
        if_revision=edited["revision_id"],
        idempotency_key="retire",
        **LOCAL,
    )
    assert not (await service.search(**LOCAL))["items"]
    assert len((await service.search(include_retired=True, **LOCAL))["items"]) == 1
    restored = await service.restore(
        identity=identity(original),
        reason="copy original",
        revision_id=original["revision_id"],
        if_revision=retired["revision_id"],
        idempotency_key="restore",
        **LOCAL,
    )
    assert restored["sequence"] == 5
    assert (await service.show(identity=identity(original), **LOCAL))["snapshot"][
        "body"
    ] == "Original\r\nα"
    history = await service.history(identity=identity(original), limit=2, **LOCAL)
    assert [r["sequence"] for r in history["revisions"]] == [5, 4]


async def test_search_scope_ranking_cursor_and_read_only(service):
    await seed_project(service.db, "q")
    exact = await create(service, "exact", title="needle", body="unrelated")
    for i in range(3):
        await create(service, str(i), title=f"other {i}", body="needle")
    await service.create(
        snapshot=snapshot(title="secret needle"),
        idempotency_key="q",
        principal=TRUSTED_LOCAL,
        project_id="q",
    )
    first = await service.search(query="needle", limit=2, **LOCAL)
    assert first["items"][0]["record_id"] == exact["record_id"]
    second = await service.search(query="needle", limit=2, cursor=first["next_cursor"], **LOCAL)
    assert len({item["record_id"] for item in first["items"] + second["items"]}) == 4
    assert second["next_cursor"] is None
    with pytest.raises(RecordError) as exc:
        await service.search(query="other", limit=2, cursor=first["next_cursor"], **LOCAL)
    assert exc.value.code == "record.invalid_cursor"
    assert len((await service.search(query=exact["knowledge_alias"], **LOCAL))["items"]) == 1
    assert not (await service.search(query="' OR true --", **LOCAL))["items"]
    service.config.writes_enabled = False
    assert len((await service.search(**LOCAL))["items"]) == 4
    with pytest.raises(RecordError) as exc:
        await create(service, "readonly")
    assert exc.value.code == "knowledge.read_only"


async def test_export_intent_is_private_metadata_only(service):
    service.config.export.enabled = True
    result = await create(service, title="secret", body="private bytes")
    assert result["export_state"] == "pending"
    async with service.db.immediate() as conn:
        rows = (await conn.execute(select(record_outbox))).mappings().all()
    assert {row["destination"] for row in rows} == {"audit", "export"}
    assert all(
        "secret" not in str(row["payload"]) and "private bytes" not in str(row["payload"])
        for row in rows
    )
    assert all(row["revision_id"] == UUID(result["revision_id"]) for row in rows)


async def test_normalized_retry_and_snapshot_source_order(service):
    original = await create(service)
    args = dict(
        identity=identity(original),
        if_revision=original["revision_id"],
        idempotency_key="normalized",
        **LOCAL,
    )
    result = await service.update(
        patch={"tags": [" z ", "a", "z"], "valid_from": "2026-10-01T03:00:00+03:00"}, **args
    )
    replay = await service.update(
        patch={"tags": ["a", "z"], "valid_from": "2026-10-01T00:00:00.000000Z"}, **args
    )
    assert replay == {**result, "outcome": "replayed"}

    def source(ident):
        return {
            "source_id": ident,
            "kind": "url",
            "url": "https://example.test",
            "observed_at": "2026-10-01T00:00:00Z",
            "retained": False,
        }

    doc = normalize_snapshot(snapshot(sources=[source("z"), source("a")]))
    assert [item["source_id"] for item in doc["sources"]] == ["a", "z"]


@pytest.mark.parametrize("failures", [2, 3])
async def test_serialization_retry_is_bounded_and_receipts_rollback(service, monkeypatch, failures):
    class SerializationError(Exception):
        sqlstate = "40001"

    original = service._append_snapshot
    calls = 0

    async def fail_before_revision(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls <= failures:
            raise DBAPIError("synthetic serialization", {}, SerializationError())
        return await original(*args, **kwargs)

    monkeypatch.setattr(service, "_append_snapshot", fail_before_revision)
    if failures == 3:
        with pytest.raises(RecordError) as exc:
            await create(service)
        assert exc.value.code == "record.retryable"
    else:
        assert (await create(service))["outcome"] == "created"
    assert calls == 3
    async with service.db.immediate() as conn:
        assert await conn.scalar(select(func.count()).select_from(record_requests)) == (
            failures == 2
        )
