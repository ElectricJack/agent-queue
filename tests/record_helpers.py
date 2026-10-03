"""Synthetic record fixtures; no legacy imports or providers."""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import func, insert, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.database.tables import knowledge_search, record_link_heads, record_link_versions, tasks
from src.models import Project, Task, TaskStatus
from src.records.identity import knowledge_identity

NOW = datetime(2026, 10, 1, tzinfo=UTC)


def knowledge_config(**changes):
    from src.config import KnowledgeConfig

    return KnowledgeConfig(
        **{"enabled": True, "enabled_projects": ["p", "q"], "writes_enabled": True, **changes}
    )


async def worker_principal(db, name="worker", *, project_id="p", elevated=False, grants=None):
    from src.commands.principal import ExecutionPrincipal, PrincipalKind
    from src.models import Agent, SessionRecord
    from src.profiles.capabilities import CapabilityPolicy

    await seed_project(db, project_id)
    await db.create_agent(Agent(id=name, name=name, profile_id="test-worker"))
    task_id = None if elevated else f"task-{name}"
    if task_id:
        await db.create_task(
            Task(
                id=task_id,
                project_id=project_id,
                title="Held task",
                description="",
                status=TaskStatus.IN_PROGRESS,
                assigned_agent_id=name,
                claim_epoch=1,
            )
        )
        async with db.immediate() as conn:
            await conn.execute(update(tasks).where(tasks.c.id == task_id).values(claim_epoch=1))
    await db.create_session(
        SessionRecord(
            id=name,
            project_id=project_id,
            profile_id="test-worker",
            harness="codex",
            provider="fake",
            name=name,
            lifecycle="named" if elevated else "pool",
            work_dir="/tmp/knowledge-fixture",
            epoch="test",
            instance_token=f"instance-{name}",
            started_at=1,
            task_id=task_id,
            state="running",
            agent_id=name,
            last_claim_epoch=1,
        )
    )
    if grants is None:
        grants = [
            "knowledge_create",
            "knowledge_show",
            "knowledge_update",
            "knowledge_history",
            "knowledge_search",
            "knowledge_retire",
            "knowledge_restore",
            "record_show",
            "link_create",
            "link_remove",
            "link_list",
        ]
    return ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        policy=CapabilityPolicy.from_namespaces(aq_commands=grants),
        session_id=name,
        session_instance_token=f"instance-{name}",
        task_id=task_id,
        project_id=project_id,
        profile_id="test-worker",
        elevated=elevated,
    )


def snapshot(**changes):
    value = dict(
        title="Observed incident",
        body="Retained evidence.\n",
        category="incident",
        tags=[],
        lifecycle="active",
        verification="unverified",
        summary=None,
        summary_of_revision=None,
        valid_from=None,
        valid_until=None,
        recheck_at=None,
        last_verified_at=None,
        last_verified_by=None,
        retirement_reason=None,
        successor_record_id=None,
        sources=[],
        metadata={},
        outgoing_links=[],
    )
    value.update(changes)
    return value


async def seed_project(db, project_id="p"):
    if not await db.get_project(project_id):
        await db.create_project(Project(id=project_id, name=project_id))


async def seed_task(db, task_id="t", project_id="p"):
    await seed_project(db, project_id)
    await db.create_task(
        Task(
            id=task_id,
            project_id=project_id,
            title="Task",
            description="Work",
            status=TaskStatus.COMPLETED,
        )
    )
    async with db.immediate() as conn:
        return await db.ensure_task_record_on(task_id, actor_id="test", conn=conn)


async def project_search(conn, record_id, revision_id, doc, scope_key="project:p"):
    values = dict(
        record_id=record_id,
        revision_id=revision_id,
        scope_key=scope_key,
        title=doc["title"],
        summary=doc["summary"],
        category=doc["category"],
        lifecycle=doc["lifecycle"],
        verification=doc["verification"],
        valid_until=doc["valid_until"],
        recheck_at=doc["recheck_at"],
        updated_at=NOW,
        search_vector=func.to_tsvector(
            "simple", doc["title"] + " " + (doc["summary"] or "") + " " + doc["body"]
        ),
    )
    await conn.execute(
        pg_insert(knowledge_search)
        .values(**values)
        .on_conflict_do_update(
            index_elements=[knowledge_search.c.record_id],
            set_=values,
        )
    )


async def create_knowledge(db, conn, *, doc=None, record_id=None, alias=None, revision_id=None):
    generated, generated_alias = knowledge_identity()
    record_id, alias = record_id or generated, alias or generated_alias
    revision_id = revision_id or uuid4()
    doc = snapshot() if doc is None else doc
    scope_key = await db.ensure_record_scope_on(project_id="p", conn=conn)
    await db.insert_record_on(
        dict(
            record_id=record_id,
            kind="knowledge",
            scope_key=scope_key,
            knowledge_alias=alias,
            created_by="test",
        ),
        conn=conn,
    )
    await db.insert_knowledge_record_on(
        dict(record_id=record_id, current_revision_id=revision_id, current_sequence=1), conn=conn
    )
    await db.append_knowledge_revision_on(
        dict(
            revision_id=revision_id,
            record_id=record_id,
            sequence=1,
            parent_revision_id=None,
            actor_id="test",
            change_kind="create",
            content_sha256="a" * 64,
            hash_version=1,
        ),
        doc,
        conn=conn,
    )
    await project_search(conn, record_id, revision_id, doc)
    return record_id, revision_id


async def append_revision(db, conn, record_id, parent_id, *, sequence=2, doc=None):
    doc = snapshot(body="Changed evidence") if doc is None else doc
    revision_id = uuid4()
    await db.append_knowledge_revision_on(
        dict(
            revision_id=revision_id,
            record_id=record_id,
            sequence=sequence,
            parent_revision_id=parent_id,
            actor_id="test",
            change_kind="edit",
            content_sha256="b" * 64,
            hash_version=1,
        ),
        doc,
        conn=conn,
    )
    assert await db.advance_knowledge_head_on(
        record_id,
        expected_revision=parent_id,
        revision_id=revision_id,
        sequence=sequence,
        updated_at=NOW,
        conn=conn,
    )
    await project_search(conn, record_id, revision_id, doc)
    return revision_id


async def insert_link(
    conn, source, target, *, pin=None, source_revision=None, link_type="references", link_id=None
):
    link_id = link_id or uuid4()
    await conn.execute(
        insert(record_link_heads).values(
            link_id=link_id, source_record_id=source, owner_scope_key="project:p", current_version=1
        )
    )
    await conn.execute(
        insert(record_link_versions).values(
            link_id=link_id,
            version=1,
            target_record_id=target,
            target_revision_id=pin,
            link_type=link_type,
            actor_id="test",
            metadata={},
            source_revision_id=source_revision,
        )
    )
    return link_id
