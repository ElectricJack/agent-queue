"""Started mixed tasks retain code ownership while explicit acceptance moves out."""

from __future__ import annotations

import asyncio
import copy
import time
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, insert, select, update

from src.commands.contracts import CONTRACTS
from src.commands.contracts.builtin import TaskSplitArgs
from src.database.tables import (
    integration_batch_members,
    integration_batches,
    task_branch_origins,
    task_criteria,
    task_dependencies,
    task_integration_checkpoints,
    tasks,
)
from src.models import Project, SessionRecord, Task, TaskStatus
from tests.test_integration_hierarchy import db as db  # noqa: F401
from tests.test_integration_hierarchy import hierarchy as hierarchy  # noqa: F401

CODE = "Implement and verify the code"
OBSERVE = "Observe production for 30 days after landing"
CONTEXT = "Keep the published API and all safety constraints."


@pytest.fixture
async def started(db, hierarchy, command_handler_factory):  # noqa: F811
    handler = await command_handler_factory()
    handler.orchestrator.db = db
    handler.orchestrator._emit_task_event = AsyncMock()
    handler._hierarchy_integration_service = lambda: hierarchy
    await db.create_task(
        Task(
            id="epic",
            project_id="p",
            repo_id="repo",
            title="Delivery",
            description="Finite implementation",
            status=TaskStatus.IN_PROGRESS,
        )
    )
    filed = await hierarchy.file_children("epic", [{"title": "Mixed"}], 0)
    source_id = filed["children"][0]["task_id"]
    async with db.immediate() as conn:
        await conn.execute(
            update(tasks)
            .where(tasks.c.id == source_id)
            .values(
                description=f"{CONTEXT}\n\n## Acceptance Criteria\n- {CODE}\n- {OBSERVE}",
                status="IN_PROGRESS",
                claim_epoch=5,
            )
        )
        await conn.execute(
            insert(task_criteria),
            [
                {
                    "id": "code",
                    "task_id": source_id,
                    "type": "acceptance",
                    "content": CODE,
                    "sort_order": 0,
                },
                {
                    "id": "observation",
                    "task_id": source_id,
                    "type": "acceptance",
                    "content": OBSERVE,
                    "sort_order": 1,
                },
                {
                    "id": "constraint",
                    "task_id": source_id,
                    "type": "constraint",
                    "content": "Do not weaken verification",
                    "sort_order": 2,
                },
            ],
        )
        await db._upsert_meta(
            source_id, "spec_ingest", {"review_id": "approved", "section": "7"}, conn=conn
        )
        await db._upsert_meta(source_id, "checks", {"unit": "passed"}, conn=conn)
        await conn.execute(
            update(task_branch_origins)
            .where(
                task_branch_origins.c.task_id == source_id,
            )
            .values(materialized=1)
        )
    now = time.time()
    await db.create_session(
        SessionRecord(
            id="writer",
            task_id=source_id,
            project_id="p",
            profile_id="generic",
            harness="codex",
            provider="openai",
            name="writer",
            lifecycle="task",
            state="running",
            work_dir="/tmp/work",
            epoch="e",
            instance_token="writer-token",
            started_at=now,
        )
    )
    args = {
        "task_id": source_id,
        "retained_acceptance": [CODE],
        "moved": {
            "title": "Observe production",
            "acceptance": [OBSERVE],
            "placement": "operational_root",
            "delivery_targets": [source_id],
        },
        "reason": "Observation is post-landing work",
        "expected_generation": 1,
    }
    yield handler, args
    await handler._db.close()


async def _row(db, table, task_id):
    async with db._engine.connect() as conn:
        column = table.c.id if table is tasks else table.c.task_id
        return dict((await conn.execute(select(table).where(column == task_id))).mappings().one())


@pytest.mark.parametrize("mode", ["hierarchy", "train"])
async def test_started_mixed_task_preserves_source_and_relocates_observation(started, mode):
    handler, args = started
    db = handler.db
    await db.update_project("p", hierarchical_integration_mode=mode)
    source_id = args["task_id"]
    before = await _row(db, tasks, source_id)
    origin = await _row(db, task_branch_origins, source_id)
    checkpoint = await _row(db, task_integration_checkpoints, source_id)
    parent = await _row(db, task_integration_checkpoints, "epic")
    result = await handler.execute("task_split", args)
    assert result.get("success"), result
    moved_id = result["moved_task_id"]
    after = await _row(db, tasks, source_id)
    assert {k: v for k, v in after.items() if k not in {"description", "updated_at"}} == {
        k: v for k, v in before.items() if k not in {"description", "updated_at"}
    }
    assert after["description"] == f"{CONTEXT}\n\n## Acceptance Criteria\n- {CODE}"
    assert await _row(db, task_branch_origins, source_id) == origin
    assert await _row(db, task_integration_checkpoints, source_id) == checkpoint
    assert (await db.get_session("writer")).task_id == source_id
    assert await db.get_task_meta(source_id, "checks") == {"unit": "passed"}
    moved = await db.get_task(moved_id)
    assert moved.parent_task_id is None
    assert moved.profile_id is None  # the router chooses independently
    assert moved.is_blocked
    assert OBSERVE in moved.description and CODE not in moved.description
    assert CONTEXT in moved.description
    async with db._engine.connect() as conn:
        edges = (
            (
                await conn.execute(
                    select(task_dependencies).where(
                        task_dependencies.c.task_id == moved_id,
                    )
                )
            )
            .mappings()
            .all()
        )
        criteria = (await conn.execute(select(task_criteria))).mappings().all()
    assert {(e["depends_on_task_id"], e["dep_type"]) for e in edges} == {
        (source_id, "delivery-of"),
        (source_id, "discovered-from"),
    }
    assert {
        c["id"]: c["task_id"] for c in criteria if c["id"] in {"code", "constraint", "observation"}
    } == {
        "code": source_id,
        "constraint": source_id,
        "observation": moved_id,
    }
    assert {(c["task_id"], c["content"]) for c in criteria if c["type"] == "constraint"} == {
        (source_id, "Do not weaken verification"),
        (moved_id, "Do not weaken verification"),
    }
    assert await db.get_task_meta(moved_id, "spec_ingest") == await db.get_task_meta(
        source_id, "spec_ingest"
    )
    assert await db.get_task_meta(moved_id, "split") == await db.get_task_meta(source_id, "split")
    assert await db.get_task_meta(moved_id, "delivery_condition") == {
        "kind": "delivery-of",
        "targets": [source_id],
    }
    assert (await _row(db, task_integration_checkpoints, "epic"))["generation"] == parent[
        "generation"
    ] + 1


@pytest.mark.parametrize(
    "change,code",
    [
        ({"retained_acceptance": ["Implement some code"]}, "split.scope_change"),
        ({"retained_acceptance": [CODE, OBSERVE]}, "split.scope_change"),
        ({"expected_generation": 0}, "stale_generation"),
        ({"moved": {"delivery_targets": ["epic"]}}, "split.delivery_target"),
        ({"moved": {"delivery_targets": ["missing"]}}, "split.delivery_target"),
    ],
)
async def test_invalid_repair_writes_nothing(started, change, code):
    handler, original = started
    args = copy.deepcopy(original)
    if "moved" in change:
        args["moved"].update(change["moved"])
    else:
        args.update(change)
    before = await _row(handler.db, tasks, args["task_id"])
    result = await handler.execute("task_split", args)
    assert not result.get("success"), result
    assert result["code"] == code
    assert await _row(handler.db, tasks, args["task_id"]) == before
    assert len(await handler.db.list_tasks(project_id="p")) == 2
    assert await handler.db.get_task_meta(args["task_id"], "split") is None


async def test_racing_splits_have_one_winner(started):
    handler, args = started
    results = await asyncio.gather(
        handler._cmd_task_split(copy.deepcopy(args)),
        handler._cmd_task_split(copy.deepcopy(args)),
    )
    assert sum(bool(r.get("success")) for r in results) == 1
    assert next(r for r in results if not r.get("success"))["code"] == "stale_generation"
    assert len(await handler.db.list_tasks(project_id="p")) == 3


async def test_failure_after_filing_rolls_back_everything(started, monkeypatch):
    handler, args = started
    before = await _row(handler.db, tasks, args["task_id"])
    original = handler.db._upsert_meta

    async def fail_split(*values, **kwargs):
        if values[1] == "split":
            raise RuntimeError("injected split failure")
        return await original(*values, **kwargs)

    monkeypatch.setattr(handler.db, "_upsert_meta", fail_split)
    result = await handler.execute("task_split", args)
    assert "error" in result
    assert await _row(handler.db, tasks, args["task_id"]) == before
    assert len(await handler.db.list_tasks(project_id="p")) == 2
    async with handler.db._engine.connect() as conn:
        assert (
            await conn.scalar(
                select(task_criteria.c.task_id).where(task_criteria.c.id == "observation")
            )
            == args["task_id"]
        )


async def test_worker_cannot_use_split_even_with_held_source(started):
    handler, args = started
    result = await handler.execute(
        "task_split",
        {
            **args,
            "_scope": {
                "kind": "session",
                "session_id": "writer",
                "project_id": "p",
                "task_id": args["task_id"],
                "elevated": False,
            },
        },
    )
    assert not result.get("success"), result
    assert len(await handler.db.list_tasks(project_id="p")) == 2


async def test_foreign_delivery_target_is_rejected(started):
    handler, args = started
    await handler.db.create_project(Project(id="foreign", name="Foreign"))
    await handler.db.create_task(
        Task(
            id="foreign-leaf",
            project_id="foreign",
            title="Foreign",
            description="Foreign code",
        )
    )
    args["moved"]["delivery_targets"].append("foreign-leaf")
    result = await handler.execute("task_split", args)
    assert result["code"] == "split.delivery_target"


async def test_build_split_stays_inside_original_epic(started):
    handler, args = started
    args["moved"].update(placement="child", delivery_targets=[])
    result = await handler.execute("task_split", args)
    assert result.get("success"), result
    assert (await handler.db.get_task(result["moved_task_id"])).parent_task_id == "epic"
    assert (await handler.db.get_task(args["task_id"])).parent_task_id == "epic"


async def test_description_only_legacy_acceptance_is_preserved(started):
    handler, args = started
    async with handler.db.immediate() as conn:
        await conn.execute(delete(task_criteria).where(task_criteria.c.type == "acceptance"))
    result = await handler.execute("task_split", args)
    assert result.get("success"), result
    async with handler.db._engine.connect() as conn:
        criteria = (
            (
                await conn.execute(
                    select(task_criteria).where(
                        task_criteria.c.type == "acceptance",
                    )
                )
            )
            .mappings()
            .all()
        )
    assert {(c["task_id"], c["content"]) for c in criteria} == {
        (args["task_id"], CODE),
        (result["moved_task_id"], OBSERVE),
    }


@pytest.mark.parametrize("mode", ["hierarchy", "disabled"])
async def test_root_split_preserves_source_checkpoint_and_uses_metadata_generation(started, mode):
    handler, args = started
    await handler.db.update_project("p", hierarchical_integration_mode=mode)
    async with handler.db.immediate() as conn:
        await conn.execute(
            update(tasks).where(tasks.c.id == args["task_id"]).values(parent_task_id=None)
        )
    checkpoint = await _row(handler.db, task_integration_checkpoints, args["task_id"])
    args["expected_generation"] = 0
    result = await handler.execute("task_split", args)
    assert result.get("success"), result
    assert result["generation"] == 1
    assert await _row(handler.db, task_integration_checkpoints, args["task_id"]) == checkpoint
    assert (await handler.execute("task_split", args))["code"] == "stale_generation"


async def test_active_integration_batch_requires_quiescence(started):
    handler, args = started
    before = await _row(handler.db, tasks, args["task_id"])
    async with handler.db.immediate() as conn:
        await conn.execute(
            insert(integration_batches).values(
                id="batch",
                project_id="p",
                repository_id="repo",
                request_id="request",
                source_manifest_digest="digest",
                base_sha="a" * 40,
                lifecycle="sealing",
                integration_branch="aq/integration/batch",
                policy_snapshot={},
                artifact_snapshot={},
                cleanup_state="pending",
                created_at=1.0,
                updated_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_batch_members).values(
                batch_id="batch",
                ordinal=0,
                task_id=args["task_id"],
                repository_id="repo",
                source_sha="a" * 40,
                source_base_sha="a" * 40,
                reviewed_head_sha="a" * 40,
                reviewed_tree_sha="a" * 40,
                review_evidence={},
            )
        )
    result = await handler.execute("task_split", args)
    assert result["code"] == "needs_quiescence", result
    assert result["owner"]["batch_id"] == "batch"
    assert "Preserve the source claim" in result["safe_continuation"]
    assert await _row(handler.db, tasks, args["task_id"]) == before
    assert len(await handler.db.list_tasks(project_id="p")) == 2


@pytest.mark.parametrize(
    "change",
    [
        {"retained_acceptance": []},
        {"reason": " "},
        {"expected_generation": True},
        {"moved": {"title": "Observe", "acceptance": [OBSERVE], "placement": "operational_root"}},
    ],
)
def test_contract_rejects_incomplete_split(change):
    args = {
        "task_id": "source",
        "retained_acceptance": [CODE],
        "moved": {
            "title": "Observe",
            "acceptance": [OBSERVE],
            "placement": "operational_root",
            "delivery_targets": ["source"],
        },
        "reason": "Post-landing",
        "expected_generation": 0,
    }
    with pytest.raises(ValidationError):
        TaskSplitArgs.model_validate({**args, **change})


def test_command_is_registered_with_typed_surface():
    from src.mcp_registration import _discover_all_commands

    assert CONTRACTS.get("task_split").contract.execution.args_model is TaskSplitArgs
    schema = _discover_all_commands()["task_split"]["input_schema"]
    assert set(schema["required"]) == {
        "task_id",
        "retained_acceptance",
        "moved",
        "reason",
        "expected_generation",
    }
