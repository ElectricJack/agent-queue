"""Prove the storage contract with direct writes and commit-time checks."""

from uuid import uuid4

import pytest
from sqlalchemy import delete, insert, update
from sqlalchemy.exc import IntegrityError

from src.database.tables import (
    knowledge_records,
    knowledge_revision_payloads,
    knowledge_revisions,
    knowledge_search,
    record_installation,
    record_link_heads,
    record_link_versions,
    records,
    task_record_link_state,
)
from tests.record_helpers import (
    append_revision,
    create_knowledge,
    insert_link,
    seed_project,
    seed_task,
    snapshot,
)


@pytest.fixture
async def db(reuse_database):
    return await reuse_database()


async def test_knowledge_envelope_requires_domain_at_commit(db):
    await seed_project(db)
    with pytest.raises(IntegrityError, match="knowledge domain missing"):
        async with db.immediate() as conn:
            scope = await db.ensure_record_scope_on(project_id="p", conn=conn)
            await db.insert_record_on(
                dict(
                    record_id=uuid4(),
                    kind="knowledge",
                    scope_key=scope,
                    knowledge_alias="kn-" + uuid4().hex,
                    created_by="test",
                ),
                conn=conn,
            )


async def test_wrong_kind_domain_and_task_state_are_refused(db):
    task = await seed_task(db)
    with pytest.raises(IntegrityError):
        async with db.immediate() as conn:
            await conn.execute(
                insert(knowledge_records).values(
                    record_id=task["record_id"], current_revision_id=uuid4(), current_sequence=1
                )
            )
    async with db.immediate() as conn:
        record_id, _ = await create_knowledge(db, conn)
    with pytest.raises(IntegrityError, match="requires task"):
        async with db.immediate() as conn:
            await conn.execute(
                insert(task_record_link_state).values(
                    record_id=record_id, link_sequence=0, link_token=uuid4()
                )
            )


async def test_revision_requires_payload_at_commit(db):
    await seed_project(db)
    with pytest.raises(IntegrityError, match="payload missing"):
        async with db.immediate() as conn:
            record_id, revision_id = await create_knowledge(db, conn)
            # Append a real second revision with no payload using raw SQL.
            second = uuid4()
            await conn.execute(
                insert(knowledge_revisions).values(
                    record_id=record_id,
                    revision_id=second,
                    sequence=2,
                    parent_revision_id=revision_id,
                    actor_id="test",
                    change_kind="edit",
                    content_sha256="b" * 64,
                    hash_version=1,
                )
            )
            await conn.execute(
                update(knowledge_records)
                .where(knowledge_records.c.record_id == record_id)
                .values(current_revision_id=second, current_sequence=2)
            )


@pytest.mark.parametrize(
    "field,value",
    [
        ("kind", "task"),
        ("scope_key", "project:elsewhere"),
        ("record_id", uuid4()),
        ("knowledge_alias", "kn-" + "0" * 32),
        ("task_id", "different"),
    ],
)
async def test_record_identity_immutable(db, field, value):
    await seed_project(db)
    async with db.immediate() as conn:
        record_id, _ = await create_knowledge(db, conn)
    with pytest.raises(IntegrityError, match="identity is immutable"):
        async with db.immediate() as conn:
            await conn.execute(
                update(records).where(records.c.record_id == record_id).values(**{field: value})
            )


async def test_installation_and_mapping_cannot_be_deleted_or_reassigned(db):
    task = await seed_task(db)
    for stmt in (
        delete(records).where(records.c.record_id == task["record_id"]),
        update(record_installation).values(installation_id=uuid4()),
        delete(record_installation),
    ):
        with pytest.raises(IntegrityError):
            async with db.immediate() as conn:
                await conn.execute(stmt)


async def test_duplicate_knowledge_alias_and_task_alias(db):
    task = await seed_task(db)
    async with db.immediate() as conn:
        record_id, _ = await create_knowledge(db, conn)
        row = await db.get_record_on(record_id=record_id, conn=conn)
    for identity in (
        dict(kind="task", task_id=task["task_id"]),
        dict(kind="knowledge", knowledge_alias=row["knowledge_alias"]),
    ):
        with pytest.raises(IntegrityError, match="duplicate key"):
            async with db.immediate() as conn:
                await db.insert_record_on(
                    dict(record_id=uuid4(), scope_key="project:p", created_by="test", **identity),
                    conn=conn,
                )


@pytest.mark.parametrize(
    "changes",
    [
        {"title": ""},
        {"title": "x" * 241},
        {"title": None},
        {"body": 12},
        {"body": "é" * 131073},
        {"category": "task"},
        {"lifecycle": "READY"},
        {"verification": "approved"},
        {"tags": ["same", "same"]},
        {"tags": [42]},
        {"tags": ["x" * 65]},
        {"tags": [str(i) for i in range(33)]},
        {"summary": "é" * 2049},
        {"metadata": {"unscoped": 1}},
        {"metadata": {"vendor.key": "x" * 16384}},
        {"metadata": {"vendor.key": [[[[[[[[1]]]]]]]]}},
        {"valid_from": "2026-10-02T00:00:00.000000Z", "valid_until": "2026-10-01T00:00:00.000000Z"},
        {"valid_from": "2026-02-31T00:00:00.000000Z"},
        {"valid_from": "2026-10-01T00:00:00+00:00"},
        {"lifecycle": "retired", "retirement_reason": " "},
        {"verification": "verified"},
        {"last_verified_by": "unearned"},
        {"sources": [{"source_id": "x", "kind": "artifact"}]},
        {"sources": [{"source_id": "x", "kind": "task", "task_id": "t"}] * 2},
        {"sources": [42]},
        {"outgoing_links": [{}]},
        {"outgoing_links": [None]},
    ],
)
async def test_invalid_snapshot_direct_sql_is_refused(db, changes):
    await seed_project(db)
    with pytest.raises(IntegrityError):
        async with db.immediate() as conn:
            await create_knowledge(db, conn, doc=snapshot(**changes))


async def test_missing_required_key_and_invalid_tombstone(db):
    await seed_project(db)
    doc = snapshot()
    del doc["last_verified_at"]
    with pytest.raises(IntegrityError):
        async with db.immediate() as conn:
            await create_knowledge(db, conn, doc=doc)
    with pytest.raises(IntegrityError):
        async with db.immediate() as conn:
            await conn.execute(insert(knowledge_revision_payloads).values(revision_id=uuid4()))


async def test_envelopes_payloads_and_link_versions_are_append_only(db):
    task = await seed_task(db)
    async with db.immediate() as conn:
        record_id, revision_id = await create_knowledge(db, conn)
        link_id = await insert_link(conn, task["record_id"], record_id, pin=revision_id)
    for table, predicate, values in (
        (
            knowledge_revisions,
            knowledge_revisions.c.revision_id == revision_id,
            {"actor_id": "other"},
        ),
        (
            knowledge_revision_payloads,
            knowledge_revision_payloads.c.revision_id == revision_id,
            {"snapshot": snapshot(body="tampered")},
        ),
        (record_link_versions, record_link_versions.c.link_id == link_id, {"metadata": {}}),
    ):
        for stmt in (
            update(table).where(predicate).values(**values),
            delete(table).where(predicate),
        ):
            with pytest.raises(IntegrityError, match="immutable"):
                async with db.immediate() as conn:
                    await conn.execute(stmt)


async def test_wrong_record_head_parent_and_target_pins(db):
    task = await seed_task(db)
    async with db.immediate() as conn:
        record_id, first = await create_knowledge(db, conn)
        other_id, other = await create_knowledge(db, conn)
    with pytest.raises(IntegrityError):
        async with db.immediate() as conn:
            await conn.execute(
                update(knowledge_records)
                .where(knowledge_records.c.record_id == record_id)
                .values(current_revision_id=other)
            )
    with pytest.raises(IntegrityError):
        async with db.immediate() as conn:
            await append_revision(db, conn, record_id, other)
    for target, pin in ((record_id, other), (task["record_id"], first)):
        with pytest.raises(IntegrityError):
            async with db.immediate() as conn:
                await insert_link(conn, other_id, target, pin=pin, source_revision=other)


async def test_head_cannot_rewind_skip_parent_or_leave_stale_search(db):
    await seed_project(db)
    async with db.immediate() as conn:
        record_id, first = await create_knowledge(db, conn)
        second = await append_revision(db, conn, record_id, first)
    for values in (
        dict(current_revision_id=first, current_sequence=1),
        dict(current_revision_id=second, current_sequence=3),
    ):
        with pytest.raises(IntegrityError):
            async with db.immediate() as conn:
                await conn.execute(
                    update(knowledge_records)
                    .where(knowledge_records.c.record_id == record_id)
                    .values(**values)
                )
    with pytest.raises(IntegrityError, match="append to previous"):
        async with db.immediate() as conn:
            await append_revision(db, conn, record_id, first, sequence=3)
    with pytest.raises(IntegrityError, match="search disagrees"):
        async with db.immediate() as conn:
            await conn.execute(update(knowledge_search).values(revision_id=first))
    with pytest.raises(IntegrityError, match="projection missing"):
        async with db.immediate() as conn:
            await conn.execute(delete(knowledge_search))


async def test_link_matrix_self_link_duplicate_and_scope(db):
    task = await seed_task(db)
    async with db.immediate() as conn:
        record_id, _ = await create_knowledge(db, conn)
        await insert_link(conn, task["record_id"], record_id)
    for source, target, kind in (
        (task["record_id"], record_id, "references"),  # duplicate floating tuple
        (task["record_id"], task["record_id"], "references"),
        (task["record_id"], record_id, "supports"),
        (task["record_id"], record_id, "blocks"),
    ):
        with pytest.raises(IntegrityError):
            async with db.immediate() as conn:
                await insert_link(conn, source, target, link_type=kind)
    with pytest.raises(IntegrityError):
        async with db.immediate() as conn:
            await db.ensure_record_scope_on(project_id=None, conn=conn)
            await conn.execute(update(record_link_heads).values(owner_scope_key="global"))


async def test_knowledge_link_versions_must_belong_to_source_revision(db):
    await seed_project(db)
    async with db.immediate() as conn:
        source, source_revision = await create_knowledge(db, conn)
        target, target_revision = await create_knowledge(db, conn)
    for revision in (None, target_revision, source_revision):
        with pytest.raises(IntegrityError):
            async with db.immediate() as conn:
                await insert_link(conn, source, target, source_revision=revision)


async def test_tables_are_native_postgresql_with_named_constraints():
    from sqlalchemy import DateTime, JSON
    from sqlalchemy.dialects.postgresql import JSONB, UUID
    from src.database.tables import metadata
    from src.records.schema import RECORD_TABLE_NAMES

    for name in RECORD_TABLE_NAMES:
        table = metadata.tables[name]
        assert all(constraint.name for constraint in table.constraints)
        for column in table.c:
            if isinstance(column.type, JSON):
                assert isinstance(column.type, JSONB)
            if isinstance(column.type, DateTime):
                assert column.type.timezone
        if "record_id" in table.c:
            assert isinstance(table.c.record_id.type, UUID)
    assert not records.c.task_id.foreign_keys
    assert awaitable_free_identity_schema()


def awaitable_free_identity_schema():
    # Pure metadata check: new records never appear in execution dependency tables.
    from src.database.tables import task_dependencies

    return all(fk.column.table.name != "records" for fk in task_dependencies.foreign_keys)


async def test_concurrent_direct_link_inserts_have_one_winner(db):
    import asyncio
    from sqlalchemy import func, select

    task = await seed_task(db)
    async with db.immediate() as conn:
        target, _ = await create_knowledge(db, conn)
    barrier = asyncio.Barrier(2)

    async def write():
        async with db.immediate() as conn:
            await barrier.wait()
            return await insert_link(conn, task["record_id"], target)

    outcomes = await asyncio.gather(write(), write(), return_exceptions=True)
    assert sum(isinstance(value, IntegrityError) for value in outcomes) == 1
    assert sum(not isinstance(value, Exception) for value in outcomes) == 1
    async with db.immediate() as conn:
        assert await conn.scalar(select(func.count()).select_from(record_link_heads)) == 1


async def test_pinned_and_floating_links_are_distinct_and_foreign_keys_pin_history(db):
    task = await seed_task(db)
    async with db.immediate() as conn:
        target, first = await create_knowledge(db, conn)
        await insert_link(conn, task["record_id"], target)
        await insert_link(conn, task["record_id"], target, pin=first)
        await append_revision(db, conn, target, first)
    async with db.immediate() as conn:
        exact = await db.get_knowledge_revision_on(target, revision_id=first, conn=conn)
        assert exact["sequence"] == 1
    with pytest.raises(IntegrityError):
        async with db.immediate() as conn:
            await conn.execute(
                delete(knowledge_records).where(knowledge_records.c.record_id == target)
            )


async def test_successful_knowledge_link_history_and_removal_keep_exact_old_snapshot(db):
    await seed_project(db)
    source, first, link_id = uuid4(), uuid4(), uuid4()
    async with db.immediate() as conn:
        target, target_revision = await create_knowledge(db, conn)
        link = dict(
            link_id=str(link_id),
            version=1,
            link_type="supports",
            target_record_id=str(target),
            target_revision_id=str(target_revision),
            metadata={},
        )
        await create_knowledge(
            db, conn, record_id=source, revision_id=first, doc=snapshot(outgoing_links=[link])
        )
        await insert_link(
            conn,
            source,
            target,
            pin=target_revision,
            source_revision=first,
            link_id=link_id,
            link_type="supports",
        )
    async with db.immediate() as conn:
        second = await append_revision(db, conn, source, first, doc=snapshot(outgoing_links=[link]))
    async with db.immediate() as conn:
        third = await append_revision(db, conn, source, second, sequence=3)
        await conn.execute(
            insert(record_link_versions).values(
                link_id=link_id,
                version=2,
                target_record_id=target,
                target_revision_id=target_revision,
                link_type="supports",
                removed=True,
                source_revision_id=third,
                metadata={},
                actor_id="test",
            )
        )
        await conn.execute(
            update(record_link_heads)
            .where(record_link_heads.c.link_id == link_id)
            .values(current_version=2)
        )
    async with db.immediate() as conn:
        old = await db.get_knowledge_revision_on(source, revision_id=first, conn=conn)
        current = await db.get_knowledge_revision_on(source, conn=conn)
        assert old["snapshot"]["outgoing_links"] == [link]
        assert current["snapshot"]["outgoing_links"] == []


async def test_valid_verification_retirement_and_sources_at_database_boundary(db):
    # Authority to set these fields is K05's service responsibility. SQL still
    # requires evidence, actor/time and a retirement reason for their shape.
    await seed_project(db)
    source = dict(source_id="evidence", kind="task", task_id="retained-task")
    async with db.immediate() as conn:
        await create_knowledge(
            db,
            conn,
            doc=snapshot(
                verification="verified",
                last_verified_by="operator",
                last_verified_at="2026-10-01T00:00:00.000000Z",
                sources=[source],
                lifecycle="retired",
                retirement_reason="Replaced after investigation",
            ),
        )


async def test_invalid_source_field_types_and_duplicate_link_ids_are_rejected(db):
    await seed_project(db)
    link = dict(
        link_id=str(uuid4()),
        version=1,
        link_type="references",
        target_record_id=str(uuid4()),
        target_revision_id=None,
        metadata={},
    )
    for doc in (
        snapshot(sources=[dict(source_id="t", kind="task", task_id=123)]),
        snapshot(outgoing_links=[link, link]),
    ):
        with pytest.raises(IntegrityError, match="snapshot_v1"):
            async with db.immediate() as conn:
                await create_knowledge(db, conn, doc=doc)


async def test_summary_cannot_name_its_own_revision(db):
    await seed_project(db)
    revision = uuid4()
    with pytest.raises(IntegrityError, match="summary input"):
        async with db.immediate() as conn:
            await create_knowledge(
                db,
                conn,
                revision_id=revision,
                doc=snapshot(summary="Generated", summary_of_revision=str(revision)),
            )


async def test_supersedes_removal_cannot_orphan_a_named_successor(db):
    await seed_project(db)
    successor, revision, link_id = uuid4(), uuid4(), uuid4()
    async with db.immediate() as conn:
        retired, _ = await create_knowledge(
            db,
            conn,
            doc=snapshot(
                lifecycle="retired",
                retirement_reason="Replacement",
                successor_record_id=str(successor),
            ),
        )
        link = dict(
            link_id=str(link_id),
            version=1,
            link_type="supersedes",
            target_record_id=str(retired),
            target_revision_id=None,
            metadata={},
        )
        await create_knowledge(
            db, conn, record_id=successor, revision_id=revision, doc=snapshot(outgoing_links=[link])
        )
        await insert_link(
            conn,
            successor,
            retired,
            source_revision=revision,
            link_id=link_id,
            link_type="supersedes",
        )
    with pytest.raises(IntegrityError, match="orphan a named successor"):
        async with db.immediate() as conn:
            new = await append_revision(db, conn, successor, revision)
            await conn.execute(
                insert(record_link_versions).values(
                    link_id=link_id,
                    version=2,
                    target_record_id=retired,
                    link_type="supersedes",
                    removed=True,
                    source_revision_id=new,
                    metadata={},
                    actor_id="test",
                )
            )
            await conn.execute(
                update(record_link_heads)
                .where(record_link_heads.c.link_id == link_id)
                .values(current_version=2)
            )
