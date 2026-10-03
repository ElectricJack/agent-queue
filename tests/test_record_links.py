"""Informational links have their own ownership, history and concurrency fences."""

import asyncio
from uuid import UUID

import pytest
from sqlalchemy import func, select

from src.commands.principal import TRUSTED_LOCAL
from src.database.tables import knowledge_revisions, record_link_versions
from src.knowledge.service import KnowledgeService
from src.records.models import RecordError
from src.records.service import RecordService
from tests.record_helpers import knowledge_config, seed_project, seed_task, snapshot

LOCAL = dict(principal=TRUSTED_LOCAL, project_id="p")


@pytest.fixture
async def services(reuse_database):
    db = await reuse_database()
    await seed_project(db)
    config = knowledge_config()
    return KnowledgeService(db, config), RecordService(db, config)


async def create(service, key):
    return await service.create(snapshot=snapshot(title=key), idempotency_key=key, **LOCAL)


def ident(result):
    return f"record:{result['record_id']}"


def link(target, link_type="references", pin=None):
    return dict(action="add", target=ident(target), link_type=link_type, target_revision_id=pin)


async def test_exact_link_history_pins_floating_and_tombstones(services):
    knowledge, records = services
    source, target = await create(knowledge, "source"), await create(knowledge, "target")
    added = await records.mutate_links(
        identity=ident(source),
        operations=[link(target, pin=target["revision_id"]), link(target)],
        if_revision=source["revision_id"],
        idempotency_key="add",
        **LOCAL,
    )
    old = await knowledge.show(identity=ident(source), **LOCAL)
    assert old["sequence"] == 2
    links = (await records.list_links(identity=ident(source), **LOCAL))["links"]
    pinned = next(item for item in links if item["target_revision_id"])
    floating = next(item for item in links if not item["target_revision_id"])
    edited_target = await knowledge.update(
        identity=ident(target),
        patch={"body": "target changed"},
        if_revision=target["revision_id"],
        idempotency_key="edit-target",
        **LOCAL,
    )
    links = (await records.list_links(identity=ident(source), **LOCAL))["links"]
    assert (
        next(item for item in links if item["link_id"] == pinned["link_id"])["resolved_revision_id"]
        == target["revision_id"]
    )
    assert (
        next(item for item in links if item["link_id"] == floating["link_id"])[
            "resolved_revision_id"
        ]
        == edited_target["revision_id"]
    )
    removed = await records.mutate_links(
        identity=ident(source),
        operations=[{"action": "remove", "link_id": pinned["link_id"]}],
        if_revision=added["revision_id"],
        idempotency_key="remove",
        **LOCAL,
    )
    exact = await knowledge.show(identity=ident(source), revision_id=added["revision_id"], **LOCAL)
    assert exact["snapshot"] == old["snapshot"]
    assert len((await records.list_links(identity=ident(source), **LOCAL))["links"]) == 1
    restored = await knowledge.restore(
        identity=ident(source),
        revision_id=added["revision_id"],
        reason="restore links",
        if_revision=removed["revision_id"],
        idempotency_key="restore",
        **LOCAL,
    )
    assert restored["sequence"] == 4
    links = (await records.list_links(identity=ident(source), **LOCAL))["links"]
    assert next(item for item in links if item["link_id"] == pinned["link_id"])["version"] == 3
    assert next(item for item in links if item["link_id"] == floating["link_id"])["version"] == 1
    async with records.db.immediate() as conn:
        versions = (
            (
                await conn.execute(
                    select(record_link_versions)
                    .where(record_link_versions.c.link_id == UUID(pinned["link_id"]))
                    .order_by(record_link_versions.c.version)
                )
            )
            .mappings()
            .all()
        )
    assert [row["removed"] for row in versions] == [False, True, False]


async def test_task_link_token_and_replay_do_not_touch_execution_or_target(services):
    knowledge, records = services
    await seed_task(records.db)
    task_before = await records.db.get_task("t")
    target = await create(knowledge, "target")
    task = await records.show(identity="task:t", **LOCAL)
    args = dict(
        identity="task:t",
        operations=[link(target, "produces")],
        if_link_token=task["link_token"],
        idempotency_key="link",
        **LOCAL,
    )
    added = await records.mutate_links(**args)
    assert added["link_sequence"] == 1
    assert (await records.mutate_links(**args))["outcome"] == "replayed"
    assert await records.db.get_task("t") == task_before
    assert (await knowledge.show(identity=ident(target), **LOCAL))["revision_id"] == target[
        "revision_id"
    ]
    with pytest.raises(RecordError) as exc:
        await records.mutate_links(**{**args, "idempotency_key": "stale"})
    assert exc.value.code == "record.revision_conflict"
    async with records.db.immediate() as conn:
        assert await conn.scalar(select(func.count()).select_from(record_link_versions)) == 1


@pytest.mark.parametrize("link_type", ["blocks", "parent-child", "waits-for", "executes"])
async def test_execution_edges_refused_before_any_write(services, link_type):
    knowledge, records = services
    source, target = await create(knowledge, "source"), await create(knowledge, "target")
    with pytest.raises(RecordError) as exc:
        await records.mutate_links(
            identity=ident(source),
            operations=[link(target, link_type)],
            if_revision=source["revision_id"],
            idempotency_key="bad",
            **LOCAL,
        )
    assert exc.value.code == "record.execution_link_forbidden"
    assert (await knowledge.show(identity=ident(source), **LOCAL))["sequence"] == 1


async def test_matrix_self_duplicate_pin_and_atomic_batch(services):
    knowledge, records = services
    source, target = await create(knowledge, "source"), await create(knowledge, "target")
    for operations, code in [
        ([link(source)], "record.invalid_link"),
        ([link(target, "produces")], "record.invalid_link"),
        ([link(target), link(target)], "record.duplicate_link"),
        ([link(target, pin=source["revision_id"])], "record.revision_unavailable"),
    ]:
        with pytest.raises(RecordError) as exc:
            await records.mutate_links(
                identity=ident(source),
                operations=operations,
                if_revision=source["revision_id"],
                idempotency_key="retry",
                **LOCAL,
            )
        assert exc.value.code == code
    assert (await records.list_links(identity=ident(source), **LOCAL))["links"] == []
    result = await records.mutate_links(
        identity=ident(source),
        operations=[link(target, "supports")],
        if_revision=source["revision_id"],
        idempotency_key="retry",
        **LOCAL,
    )
    assert result["sequence"] == 2


async def test_concurrent_duplicate_has_one_winner(services, monkeypatch):
    knowledge, records = services
    source, target = await create(knowledge, "source"), await create(knowledge, "target")
    barrier = asyncio.Barrier(2)
    original = records._lock_source

    async def lock(record, *, conn):
        await barrier.wait()
        return await original(record, conn=conn)

    monkeypatch.setattr(records, "_lock_source", lock)
    results = await asyncio.wait_for(
        asyncio.gather(
            *(
                records.mutate_links(
                    identity=ident(source),
                    operations=[link(target)],
                    if_revision=source["revision_id"],
                    idempotency_key=f"link-{i}",
                    **LOCAL,
                )
                for i in range(2)
            ),
            return_exceptions=True,
        ),
        timeout=10,
    )
    assert len([r for r in results if isinstance(r, dict)]) == 1, results
    assert [r.code for r in results if isinstance(r, RecordError)] == ["record.revision_conflict"]
    async with records.db.immediate() as conn:
        assert await conn.scalar(select(func.count()).select_from(record_link_versions)) == 1
        assert await conn.scalar(select(func.count()).select_from(knowledge_revisions)) == 3


async def test_retirement_serializes_with_successor_link_removal(services, monkeypatch):
    knowledge, records = services
    old, successor = await create(knowledge, "old"), await create(knowledge, "successor")
    linked = await records.mutate_links(
        identity=ident(successor),
        operations=[link(old, "supersedes")],
        if_revision=successor["revision_id"],
        idempotency_key="supersedes",
        **LOCAL,
    )
    links = await records.list_links(identity=ident(successor), **LOCAL)
    locked, remove_attempted, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    retire_lock, remove_lock = knowledge._lock_source, records._lock_source
    lock_order = []

    async def pause_retire(record, *, conn):
        row = await retire_lock(record, conn=conn)
        lock_order.append(record["record_id"])
        if len(lock_order) == 2:
            locked.set()
            await release.wait()
        return row

    async def removing(record, *, conn):
        remove_attempted.set()
        return await remove_lock(record, conn=conn)

    monkeypatch.setattr(knowledge, "_lock_source", pause_retire)
    monkeypatch.setattr(records, "_lock_source", removing)
    retirement = asyncio.create_task(
        knowledge.retire(
            identity=ident(old),
            reason="successor published",
            successor_record_id=successor["record_id"],
            if_revision=old["revision_id"],
            idempotency_key="retire",
            **LOCAL,
        )
    )
    await asyncio.wait_for(locked.wait(), 2)
    removal = asyncio.create_task(
        records.mutate_links(
            identity=ident(successor),
            operations=[dict(action="remove", link_id=links["links"][0]["link_id"])],
            if_revision=linked["revision_id"],
            idempotency_key="remove",
            **LOCAL,
        )
    )
    await asyncio.wait_for(remove_attempted.wait(), 2)
    release.set()
    outcomes = await asyncio.gather(retirement, removal, return_exceptions=True)
    assert lock_order == sorted(lock_order)
    assert isinstance(outcomes[0], dict), outcomes
    assert isinstance(outcomes[1], RecordError), outcomes
    assert outcomes[1].code == "record.integrity_conflict"
    assert (await knowledge.show(identity=ident(old), **LOCAL))["snapshot"][
        "successor_record_id"
    ] == successor["record_id"]
