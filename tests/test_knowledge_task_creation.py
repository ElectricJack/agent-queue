"""Task filing and Save finding stay atomic, guarded and replayable."""

import asyncio

import pytest
from sqlalchemy import func, select

from src.database.tables import knowledge_records, record_link_heads, record_requests, tasks
from src.records.models import RecordError
from tests.record_helpers import knowledge_config, seed_project, seed_task

pytestmark = pytest.mark.usefixtures("unpooled_postgres")


@pytest.fixture
async def handler(command_handler_factory):
    h = await command_handler_factory()
    await seed_project(h.db, "p")
    h.config.knowledge = knowledge_config(ui_enabled=True)
    return h


async def source(handler):
    return await handler.execute(
        "knowledge_create",
        dict(
            project_id="p",
            title="Evidence",
            body="Selected observation",
            category="incident",
            idempotency_key="source",
        ),
    )


def filing(record, **changes):
    return dict(
        project_id="p",
        identity=f"record:{record['record_id']}",
        revision_id=record["revision_id"],
        title="Investigate",
        description="User-selected work",
        idempotency_key="filing",
        **changes,
    )


async def counts(db):
    async with db.immediate() as conn:
        return [
            await conn.scalar(select(func.count()).select_from(table))
            for table in (tasks, knowledge_records, record_link_heads, record_requests)
        ]


async def test_replay_is_one_task_and_pinned_informational_link(handler):
    record = await source(handler)
    first = await handler.execute("knowledge_create_task", filing(record))
    assert first["success"], first
    again = await handler.execute(
        "knowledge_create_task", {**filing(record), "task_type": None, "priority": 100, "root": False}
    )
    assert again["task_id"] == first["task_id"]
    assert again["link_id"] == first["link_id"]
    assert again["outcome"] == "replayed"
    assert await counts(handler.db) == [1, 1, 1, 2]
    task = await handler.db.get_task(first["task_id"])
    assert task.route_source == "unrouted"
    shown = await handler.execute(
        "knowledge_show", {"project_id": "p", "identity": filing(record)["identity"]}
    )
    assert shown["revision_id"] == record["revision_id"]
    linked = await handler.execute(
        "link_list", {"project_id": "p", "identity": f"task:{first['task_id']}"}
    )
    assert linked["links"][0]["link_type"] == "motivated_by"
    assert linked["links"][0]["target_revision_id"] == record["revision_id"]


async def test_concurrent_replay_rolls_back_competing_task(handler):
    record = await source(handler)
    results = await asyncio.gather(
        *(handler.execute("knowledge_create_task", filing(record)) for _ in range(2))
    )
    assert all(result.get("success") for result in results), results
    assert results[0]["task_id"] == results[1]["task_id"]
    assert await counts(handler.db) == [1, 1, 1, 2]


async def test_changed_request_and_route_override_refused_without_new_task(handler):
    record = await source(handler)
    assert (await handler.execute("knowledge_create_task", filing(record)))["success"]
    changed = filing(record)
    changed["title"] = "Changed work"
    result = await handler.execute("knowledge_create_task", changed)
    assert result["error_code"] == "record.idempotency_conflict"
    result = await handler.execute(
        "knowledge_create_task", {**filing(record), "profile_id": "worker-codex"}
    )
    assert result.get("error")
    assert await counts(handler.db) == [1, 1, 1, 2]


async def test_link_failure_rolls_back_task_gate_and_receipt(handler, monkeypatch):
    record = await source(handler)
    before = await counts(handler.db)

    async def fail(*args, **kwargs):
        raise RecordError("record.retryable")

    monkeypatch.setattr(handler._knowledge_service(), "_write_links", fail)
    result = await handler.execute("knowledge_create_task", filing(record))
    assert result["error_code"] == "record.retryable"
    assert await counts(handler.db) == before


async def test_disabled_readonly_and_wrong_revision_do_not_file(handler):
    record = await source(handler)
    for field in ("enabled", "writes_enabled"):
        setattr(handler.config.knowledge, field, False)
        assert not (await handler.execute("knowledge_create_task", filing(record)))["success"]
        setattr(handler.config.knowledge, field, True)
    wrong = filing(record)
    wrong["revision_id"] = "00000000-0000-0000-0000-000000000001"
    assert not (await handler.execute("knowledge_create_task", wrong))["success"]
    assert (await counts(handler.db))[0] == 0


async def test_save_selected_finding_atomically_links_task_and_replays(handler):
    await seed_task(handler.db)
    task = await handler.execute("record_show", {"project_id": "p", "identity": "task:t"})
    args = dict(
        project_id="p",
        title="Finding",
        body="Only selected text",
        category="note",
        source_task_id="t",
        if_link_token=task["link_token"],
        idempotency_key="finding",
    )
    first = await handler.execute("knowledge_create", args)
    assert first["success"], first
    again = await handler.execute("knowledge_create", args)
    assert again["record_id"] == first["record_id"]
    assert again["link_id"] == first["link_id"]
    linked = await handler.execute("link_list", {"project_id": "p", "identity": "task:t"})
    assert linked["links"][0]["link_type"] == "produces"
    assert linked["links"][0]["target_revision_id"] == first["revision_id"]
    assert (await handler.db.get_task("t")).description == "Work"


async def test_save_finding_conflict_and_readonly_actions(handler):
    await seed_task(handler.db)
    result = await handler.execute(
        "knowledge_create",
        dict(
            project_id="p",
            title="Finding",
            body="Selection",
            category="note",
            source_task_id="t",
            idempotency_key="finding",
        ),
    )
    assert result["error_code"] == "record.precondition_required"
    assert (await counts(handler.db))[1] == 0
    record = await source(handler)
    handler.config.knowledge.writes_enabled = False
    shown = await handler.execute(
        "knowledge_show", {"project_id": "p", "identity": filing(record)["identity"]}
    )
    assert shown["allowed_actions"] == ["history"]


async def test_filing_preserves_ordinary_routing_gate_on_replay(handler, monkeypatch):
    from src.database.tables import gates, task_gates

    handler.orchestrator.playbook_manager = object()
    monkeypatch.setattr("src.playbooks.routing.requires_routing_gate", lambda *a: True)
    record = await source(handler)
    first = await handler.execute("knowledge_create_task", filing(record))
    assert first["success"], first
    assert first["gate_ids"]
    replay = await handler.execute("knowledge_create_task", filing(record))
    assert replay["task_id"] == first["task_id"]
    assert replay["gate_ids"] == first["gate_ids"]
    async with handler.db.immediate() as conn:
        assert await conn.scalar(select(func.count()).select_from(gates)) == 1
        assert await conn.scalar(select(func.count()).select_from(task_gates)) == 1
    task = await handler.db.get_task(first["task_id"])
    assert task.status.value == first["status"]
    async with handler.db.immediate() as conn:
        assert (
            await conn.scalar(select(gates.c.status).where(gates.c.id == first["gate_ids"][0]))
            == "open"
        )
    assert task.route_source == "unrouted"


async def test_worker_filing_is_parented_and_stale_claim_cannot_replay(handler):
    from src.commands.principal import principal_context
    from src.database.tables import tasks
    from sqlalchemy import update
    from tests.record_helpers import worker_principal

    principal = await worker_principal(handler.db, grants=["knowledge_create_task", "create_task"])
    scope = dict(
        kind="session",
        session_id=principal.session_id,
        project_id="p",
        task_id=principal.task_id,
        elevated=False,
    )
    record = await source(handler)
    request = {
        **filing(record),
        "reason": "Investigation discovered from held work",
        "claim_epoch": 1,
        "_scope": scope,
    }
    with principal_context(principal):
        first = await handler.execute("knowledge_create_task", request)
        assert first["success"], first
        replay = await handler.execute("knowledge_create_task", request)
        assert replay["task_id"] == first["task_id"]
        assert first["parent_id"] == principal.task_id
        async with handler.db.immediate() as conn:
            await conn.execute(
                update(tasks).where(tasks.c.id == principal.task_id).values(claim_epoch=2)
            )
        denied = await handler.execute("knowledge_create_task", request)
        assert not denied["success"], denied
    assert (await counts(handler.db))[0] == 2


async def test_cross_project_and_task_identity_are_refused(handler):
    await seed_task(handler.db)
    record = await source(handler)
    task = await handler.execute("record_show", {"project_id": "p", "identity": "task:t"})
    request = {**filing(record), "identity": "task:t", "revision_id": record["revision_id"]}
    assert not (await handler.execute("knowledge_create_task", request))["success"]
    await seed_project(handler.db, "q")
    assert not (
        await handler.execute("knowledge_create_task", {**filing(record), "project_id": "q"})
    )["success"]
    assert task["kind"] == "task"
    assert (await counts(handler.db))[0] == 1


async def test_list_filters_are_applied_before_paging_and_capabilities_local(handler):
    caps = await handler.execute("record_capabilities", {"project_id": "p"})
    assert "knowledge_create_task" in caps["capabilities"]["granted_operations"]
    active = await source(handler)
    retired = await handler.execute(
        "knowledge_create",
        dict(project_id="p", title="Old", body="Old", category="note", idempotency_key="old"),
    )
    await handler.execute(
        "knowledge_retire",
        dict(
            project_id="p",
            identity=f"record:{retired['record_id']}",
            if_revision=retired["revision_id"],
            reason="superseded",
            idempotency_key="retire",
        ),
    )
    page = await handler.execute(
        "record_search", dict(project_id="p", lifecycle="retired", include_retired=True, limit=1)
    )
    assert [item["record_id"] for item in page["items"]] == [retired["record_id"]]
    assert page["next_cursor"] is None
    assert active["record_id"] != page["items"][0]["record_id"]


async def test_concurrent_worker_replay_at_last_filing_allowance(handler, monkeypatch):
    from src.commands.principal import principal_context
    from tests.record_helpers import worker_principal

    principal = await worker_principal(handler.db, grants=["knowledge_create_task", "create_task"])
    handler.config.swarm.max_filings_per_task = 1
    barrier = asyncio.Barrier(2)
    ordinary_filer = handler._cmd_create_task

    async def synchronize(args):
        await barrier.wait()
        return await ordinary_filer(args)

    monkeypatch.setattr(handler, "_cmd_create_task", synchronize)
    record = await source(handler)
    request = {
        **filing(record),
        "reason": "Discovered work",
        "claim_epoch": 1,
        "_scope": dict(
            kind="session",
            session_id=principal.session_id,
            project_id="p",
            task_id=principal.task_id,
            elevated=False,
        ),
    }
    with principal_context(principal):
        results = await asyncio.gather(
            *(handler.execute("knowledge_create_task", request) for _ in range(2))
        )
    assert all(result["success"] for result in results), results
    assert results[0]["task_id"] == results[1]["task_id"]
    assert (await counts(handler.db))[0] == 2
    assert (await counts(handler.db))[2] == 1


async def test_readable_historical_pin_survives_redacted_current(handler):
    record = await source(handler)
    identity = filing(record)["identity"]
    current = await handler.execute(
        "knowledge_update",
        dict(
            project_id="p",
            identity=identity,
            body="Separate current content",
            if_revision=record["revision_id"],
            idempotency_key="update",
        ),
    )
    erased = await handler.execute(
        "knowledge_redact",
        dict(
            project_id="p",
            identity=identity,
            revision_id=current["revision_id"],
            if_revision=current["revision_id"],
            reason_code="sensitive",
            dry_run=False,
            idempotency_key="redact",
        ),
    )
    assert erased["success"], erased
    shown = await handler.execute(
        "knowledge_show", dict(project_id="p", identity=identity, revision_id=record["revision_id"])
    )
    assert shown["success"], shown
    assert shown["snapshot"]["body"] == "Selected observation"
    assert shown["current_revision_id"] == current["revision_id"]
    assert "edit" not in shown["allowed_actions"]
