"""Explicit historical abandonment retains its journal, completions and fences."""

import json
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import insert, select, update

from src.commands.contracts.integration import IntegrationRetireLegacyParkArgs
from src.commands.handler import CommandHandler
from src.commands.integration_commands import IntegrationCommandsMixin
from src.commands.principal import TRUSTED_LOCAL, principal_context
from src.database import tables as t
from src.config import AppConfig
from src.database.queries.hierarchy_queries import HierarchyError
from src.integration.legacy_park_retirement import LegacyParkRetirement
from src.models import (
    Project, RepoConfig, RepoSourceType, SessionRecord, Task, TaskCompletion, TaskStatus, Workspace,
)
from tests.test_archive import db as fixture_db

db = fixture_db


async def seed(db):
    await db.create_project(Project(id="p", name="p"))
    await db.create_repo(RepoConfig(id="r", project_id="p", source_type=RepoSourceType.LINK))
    await db.update_project("p", integration_repository_id="r")
    for task_id in ("a", "b", "protected"):
        await db.create_task(Task(id=task_id, project_id="p", title=task_id,
                                 description="", status=TaskStatus.COMPLETED))
    row = {"id": "legacy-operation:old", "project_id": "p", "state": "parked",
           "manifest": [{"task_id": "a", "source_sha": "a" * 40},
                        {"task_id": "b", "source_sha": "b" * 40}],
           "target_ref": "refs/heads/main", "created_at": 1.0, "updated_at": 2.0,
           "evidence": {"kind": "merge_conflict"}}
    await db.log_event("development.operation", project_id="p", payload=json.dumps(row))
    return IntegrationRetireLegacyParkArgs(project_id="p", operation_id=row["id"],
        task_ids=["a", "b"], reason="operator abandons both historical sources")


async def journal(db):
    async with db._engine.connect() as conn:
        return list((await conn.execute(select(t.events.c.payload).where(
            t.events.c.event_type == "development.operation").order_by(t.events.c.id))).scalars())


async def applying(db, request):
    preview = await LegacyParkRetirement(db).run(request)
    return request.model_copy(update={"dry_run": False,
        "expected_event_id": preview["event_id"], "expected_sha256": preview["sha256"]})


async def test_retirement_unblocks_shared_manifest_archive_and_preserves_history(db):
    request = await seed(db)
    await db.save_task_completion(TaskCompletion(id="immutable-close", task_id="a", outcome="pass",
        completed_at=1.0, commits=["a" * 40], tests=["original passing evidence"]))
    completion = await db.get_task_completion("a")
    original = await journal(db)
    with pytest.raises(HierarchyError, match="development batch"):
        await db.archive_task("a", abandon_undelivered=True, abandon_reason="old")
    async with db._engine.begin() as conn:
        await conn.execute(insert(t.integration_branch_owners).values(
            id="retained-cleanup", repository_id="r", ref="refs/heads/aq/integration/old",
            owner_id="ended", owner_role="collector", fence_token=3, handoff_state="reserved",
            created_at=1.0, updated_at=1.0))
    apply = await applying(db, request)
    assert await journal(db) == original  # preview writes nothing
    result = await LegacyParkRetirement(db).run(apply)
    assert result["outcome"] == "retired"
    assert (await LegacyParkRetirement(db).run(apply))["outcome"] == "retired"
    rows = await journal(db)
    assert rows[0] == original[0] and len(rows) == 2
    revised = json.loads(rows[1])
    assert revised["manifest"] == json.loads(original[0])["manifest"]
    assert revised["evidence"]["kind"] == "merge_conflict"
    assert revised["state"] == "cancelled"
    assert revised["evidence"]["operator_retirement"]["disposition"] == "abandoned"
    for task_id in ("a", "b"):
        assert await db.archive_task(task_id, abandon_undelivered=True, abandon_reason="old")
        assert (await db.get_archived_task(task_id))["status"] == "COMPLETED"
    assert (await db.get_task("protected")).status is TaskStatus.COMPLETED
    assert await db.get_task_completion("a") == completion
    async with db._engine.connect() as conn:
        owner = (await conn.execute(select(t.integration_branch_owners))).mappings().one()
    assert owner["fence_token"] == 3 and owner["handoff_state"] == "reserved"


@pytest.mark.parametrize("guard", ["partial", "extra", "changed", "project", "active",
                                    "missing", "claim", "workspace", "prepared", "writer", "lease"])
async def test_retirement_refuses_unselected_changed_or_active_work_without_writes(db, guard):
    request = await seed(db)
    request = await applying(db, request)
    if guard == "partial":
        request = request.model_copy(update={"task_ids": ["a"]})
    elif guard == "extra":
        request = request.model_copy(update={"task_ids": ["a", "b", "protected"]})
    elif guard == "changed":
        row = json.loads((await journal(db))[0])
        row["evidence"]["new"] = "new attempt"
        await db.log_event("development.operation", project_id="p", payload=json.dumps(row))
    elif guard == "project":
        request = request.model_copy(update={"project_id": "other"})
    elif guard == "active":
        async with db._engine.begin() as conn:
            await conn.execute(update(t.tasks).where(t.tasks.c.id == "a").values(status="PAUSED"))
    elif guard == "missing":
        request = request.model_copy(update={"operation_id": "missing"})
    elif guard == "claim":
        await db.set_task_meta("a", "claimed_by_session", "unproved-holder")
    elif guard == "workspace":
        await db.create_workspace(Workspace(id="w", project_id="p", workspace_path="/tmp/old",
                                           source_type=RepoSourceType.LINK,
                                           locked_by_task_id="a", locked_at=time.time()))
    elif guard == "writer":
        async with db._engine.begin() as conn:
            await conn.execute(insert(t.integration_branch_owners).values(
                id="writer", repository_id="r", ref="refs/heads/aq/a", owner_id="a",
                owner_role="worker", fence_token=1, handoff_state="attached",
                created_at=1.0, updated_at=1.0))
    elif guard == "lease":
        async with db._engine.begin() as conn:
            await conn.execute(insert(t.project_integration_leases).values(
                project_id="p", repository_id="r", batch_id="active", owner_id="publisher",
                fence_token=1, heartbeat_at=time.time(), expires_at=time.time()+60))
    elif guard == "prepared":
        row = json.loads((await journal(db))[0])
        row["state"] = "prepared"
        await db.log_event("development.operation", project_id="p", payload=json.dumps(row))
    before = await journal(db)
    with pytest.raises(ValueError):
        await LegacyParkRetirement(db).run(request)
    assert await journal(db) == before


async def test_archived_manifest_member_remains_readable_for_exact_abandonment(db):
    request = await seed(db)
    # An archived source can remain in a shared historical manifest.
    async with db._engine.begin() as conn:
        await db._archive_one(await db._get_task_conn("b", conn=conn), conn=conn)
    apply = await applying(db, request)
    assert (await LegacyParkRetirement(db).run(apply))["outcome"] == "retired"


async def test_command_requires_local_operator_even_for_preview(db):
    request = await seed(db)
    handler = IntegrationCommandsMixin()
    handler.orchestrator = SimpleNamespace(db=db)
    # The real CommandHandler exposes db as a property; the mixin double uses a field.
    handler.db = db
    result = await handler._cmd_integration_retire_legacy_park(request.model_dump())
    assert result["outcome"] == "refused"
    with principal_context(TRUSTED_LOCAL):
        result = await handler._cmd_integration_retire_legacy_park(request.model_dump())
    assert result["success"] and result["outcome"] == "preview"


async def test_actual_local_command_handler_dispatches_preview_and_apply(db):
    request = await seed(db)
    handler = CommandHandler(SimpleNamespace(db=db), AppConfig())
    preview = await handler.execute("integration_retire_legacy_park", request.model_dump())
    assert preview["success"] and preview["outcome"] == "preview"
    request = request.model_copy(update={"dry_run": False,
        "expected_event_id": preview["event_id"], "expected_sha256": preview["sha256"]})
    result = await handler.execute("integration_retire_legacy_park", request.model_dump())
    assert result["success"] and result["outcome"] == "retired"


@pytest.mark.parametrize("task_id", [None, "a"])
async def test_taskless_stop_requested_supervisor_is_not_a_task_branch_writer(db, task_id):
    request = await seed(db)
    await db.create_session(SessionRecord(id="stopping", project_id="p", task_id=task_id,
        profile_id="supervisor", harness="codex", provider="tmux", name="stopping",
        lifecycle="named", work_dir="/unused", epoch="test", started_at=1.0,
        state="running", desired_state="stopped", instance_token="exact"))
    if task_id is None:
        assert (await LegacyParkRetirement(db).run(request))["outcome"] == "preview"
    else:
        with pytest.raises(ValueError, match="session"):
            await LegacyParkRetirement(db).run(request)
