"""Mixed records and informational graph reads cannot affect task execution."""

from uuid import UUID

import pytest
from sqlalchemy import func, select

from src.commands.principal import TRUSTED_LOCAL
from src.database.tables import records, task_dependencies
from src.knowledge.service import KnowledgeService
from src.models import Task, TaskStatus
from src.records.models import RecordError
from src.records.service import RecordService
from tests.record_helpers import knowledge_config, seed_project, snapshot, worker_principal

LOCAL = dict(principal=TRUSTED_LOCAL, project_id="p")


@pytest.fixture
async def services(reuse_database):
    db = await reuse_database()
    await seed_project(db)
    config = knowledge_config()
    return KnowledgeService(db, config), RecordService(db, config)


async def create(service, key):
    return await service.create(snapshot=snapshot(title=key), idempotency_key=key, **LOCAL)


def ident(record):
    return f"record:{record['record_id']}"


async def task(db, task_id, **fields):
    title = fields.pop("title", task_id)
    await db.create_task(
        Task(id=task_id, project_id="p", title=title, description="Private body", **fields)
    )


@pytest.mark.parametrize("limit", [1, 2, 3, 4, 5])
async def test_mixed_pages_include_unmapped_live_and_archived_tasks_without_bodies(services, limit):
    knowledge, service = services
    await create(knowledge, "one")
    await create(knowledge, "two")
    for name in ["a", "b", "c"]:
        await task(
            service.db, name, status=TaskStatus.COMPLETED if name == "b" else TaskStatus.DEFINED
        )
    await service.db.archive_task("b")
    async with service.db.immediate() as conn:
        assert (
            await conn.scalar(
                select(func.count()).select_from(records).where(records.c.kind == "task")
            )
            == 0
        )
    items, cursor = [], None
    for _ in range(10):
        page = await service.search(kind="all", limit=limit, cursor=cursor, **LOCAL)
        assert len(page["items"]) <= limit
        items.extend(page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    else:
        pytest.fail("cursor did not terminate")
    assert [item["kind"] for item in items] == ["knowledge", "knowledge", "task", "task", "task"]
    assert [item["task_id"] for item in items if item["kind"] == "task"] == ["c", "b", "a"]
    assert len({item["record_id"] for item in items}) == 5
    assert next(item for item in items if item.get("task_id") == "b")["archived"] is True
    assert all("body" not in item and "description" not in item for item in items)
    assert (await service.search(kind="knowledge", **LOCAL))["items"][0]["kind"] == "knowledge"
    assert len((await service.search(kind="task", **LOCAL))["items"]) == 3


async def test_cursor_is_bound_to_scope_principal_kind_and_filters(services):
    knowledge, service = services
    await create(knowledge, "one")
    await create(knowledge, "two")
    cursor = (await service.search(kind="all", limit=1, **LOCAL))["next_cursor"]
    for changes in [{"kind": "task"}, {"query": "two"}, {"limit": 2}, {"category": "fact"}]:
        with pytest.raises(RecordError, match="record.invalid_cursor"):
            await service.search(
                **{**LOCAL, "kind": "all", "limit": 1, "cursor": cursor, **changes}
            )
    principal = await worker_principal(service.db, grants=["record_search"])
    with pytest.raises(RecordError, match="record.invalid_cursor"):
        await service.search(
            kind="all", limit=1, cursor=cursor, principal=principal, project_id="p"
        )
    for cursor in ["not base64!", "W10=", "bnVsbA=="]:
        with pytest.raises(RecordError, match="record.invalid_cursor"):
            await service.search(kind="all", cursor=cursor, **LOCAL)


async def test_worker_search_uses_record_search_grant_and_only_held_task(services):
    knowledge, service = services
    await create(knowledge, "finding")
    await task(service.db, "other")
    await seed_project(service.db, "q")
    await service.db.create_task(Task(id="secret", project_id="q", title="Secret", description=""))
    principal = await worker_principal(service.db, grants=["record_search", "record_show"])
    result = await service.search(kind="all", principal=principal, project_id="p")
    assert {item.get("task_id") for item in result["items"] if item["kind"] == "task"} == {
        principal.task_id
    }
    assert "secret" not in str(result) and "other" not in str(result)
    assert len(result["items"]) == 2
    await service.db.add_dependency("other", principal.task_id, "blocks")
    shown = await service.show(
        identity=f"task:{principal.task_id}",
        include_edges=True,
        principal=principal,
        project_id="p",
    )
    assert shown["edges"] == []


async def test_task_search_treats_patterns_literally_and_excludes_other_projects(services):
    _, service = services
    await task(service.db, "with-percent", title="100%_observed")
    await task(service.db, "ordinary")
    result = await service.search(kind="task", query="%_", **LOCAL)
    assert [item["task_id"] for item in result["items"]] == ["with-percent"]


async def test_pinned_graph_edges_and_historical_removal_are_exact(services):
    knowledge, service = services
    source, target = await create(knowledge, "source"), await create(knowledge, "target")
    changed = await service.mutate_links(
        identity=ident(source),
        operations=[
            dict(
                action="add",
                target=ident(target),
                link_type="supports",
                target_revision_id=target["revision_id"],
            )
        ],
        if_revision=source["revision_id"],
        idempotency_key="link",
        **LOCAL,
    )
    await knowledge.update(
        identity=ident(target),
        patch={"body": "New evidence"},
        if_revision=target["revision_id"],
        idempotency_key="edit",
        **LOCAL,
    )
    edges = (await service.show(identity=ident(source), include_edges=True, **LOCAL))["edges"]
    assert len(edges) == 1 and edges[0]["domain"] == "informational"
    assert edges[0]["target_revision_id"] == target["revision_id"]
    await service.mutate_links(
        identity=ident(source),
        operations=[dict(action="remove", link_id=edges[0]["edge_id"].removeprefix("link:"))],
        if_revision=changed["revision_id"],
        idempotency_key="remove",
        **LOCAL,
    )
    assert (await service.show(identity=ident(source), include_edges=True, **LOCAL))["edges"] == []
    assert (
        await service.show(
            identity=ident(source), revision_id=changed["revision_id"], include_edges=True, **LOCAL
        )
    )["edges"] == edges


async def test_execution_and_record_links_are_distinct_and_progress_unchanged(services):
    knowledge, service = services
    await task(service.db, "root")
    await task(service.db, "child", status=TaskStatus.READY)
    await service.db.add_dependency("child", "root", "parent-child")
    before = await service.db.get_group_progress("root")
    dependencies = await service.db.get_dependencies("child")
    task_before = await service.db.get_task("child")
    target = await create(knowledge, "finding")
    root = await service.show(identity="task:root", **LOCAL)
    await service.mutate_links(
        identity="task:root",
        operations=[
            dict(
                action="add",
                target=ident(target),
                link_type="produces",
                target_revision_id=target["revision_id"],
            )
        ],
        if_link_token=root["link_token"],
        idempotency_key="finding-link",
        **LOCAL,
    )
    result = await service.show(identity="task:root", include_edges=True, **LOCAL)
    assert {(edge["domain"], edge["type"]) for edge in result["edges"]} == {
        ("execution", "parent-child"),
        ("informational", "produces"),
    }
    await service.search(kind="all", **LOCAL)
    assert await service.db.get_group_progress("root") == before
    assert await service.db.get_dependencies("child") == dependencies
    assert await service.db.get_task("child") == task_before
    async with service.db.immediate() as conn:
        assert await conn.scalar(select(func.count()).select_from(task_dependencies)) == 1


async def test_feature_off_and_read_only_preserve_operator_controls(services):
    knowledge, service = services
    await create(knowledge, "finding")
    service.config.writes_enabled = False
    assert (await service.search(kind="all", **LOCAL))["success"] is True
    service.config.enabled = False
    with pytest.raises(RecordError, match="knowledge.disabled"):
        await service.search(kind="all", **LOCAL)


async def test_handler_mixed_surface(command_handler_factory):
    handler = await command_handler_factory()
    await seed_project(handler._db)
    handler.config.knowledge = knowledge_config()
    await task(handler._db, "t")
    result = await handler.execute("record_search", {"project_id": "p", "kind": "all"})
    assert result["success"] is True and result["items"][0]["task_id"] == "t"
    result = await handler.execute(
        "record_show", {"project_id": "p", "identity": "task:t", "include_edges": True}
    )
    assert result["success"] is True and result["edges"] == []
    assert UUID(result["record_id"])


async def test_graph_omits_cross_project_endpoint_and_does_not_leak_counts(services):
    knowledge, service = services
    await task(service.db, "visible")
    await seed_project(service.db, "q")
    private = await knowledge.create(
        snapshot=snapshot(title="Private q evidence"),
        idempotency_key="private",
        principal=TRUSTED_LOCAL,
        project_id="q",
    )
    source = await service.show(identity="task:visible", **LOCAL)
    from tests.record_helpers import insert_link

    async with service.db.immediate() as conn:
        await insert_link(conn, UUID(source["record_id"]), UUID(private["record_id"]))
    result = await service.show(identity="task:visible", include_edges=True, **LOCAL)
    assert result["edges"] == []
    assert private["record_id"] not in str(result)
    assert "Private q evidence" not in str(result)


async def test_redacted_pin_does_not_resolve_to_current_revision(services):
    knowledge, service = services
    await task(service.db, "visible")
    target = await create(knowledge, "old")
    source = await service.show(identity="task:visible", **LOCAL)
    await service.mutate_links(
        identity="task:visible",
        operations=[
            dict(
                action="add",
                target=ident(target),
                link_type="references",
                target_revision_id=target["revision_id"],
            )
        ],
        if_link_token=source["link_token"],
        idempotency_key="pin",
        **LOCAL,
    )
    current = await knowledge.update(
        identity=ident(target),
        patch={"body": "New bytes"},
        if_revision=target["revision_id"],
        idempotency_key="edit",
        **LOCAL,
    )
    await knowledge.redact(
        identity=ident(target),
        revision_id=target["revision_id"],
        if_revision=current["revision_id"],
        reason_code="sensitive",
        dry_run=False,
        idempotency_key="redact",
        **LOCAL,
    )
    result = await service.show(identity="task:visible", include_edges=True, **LOCAL)
    edge = result["edges"][0]
    assert edge["target_revision_id"] == target["revision_id"]
    assert edge["availability"] == "record.revision_redacted"
    assert current["revision_id"] not in str(edge)
