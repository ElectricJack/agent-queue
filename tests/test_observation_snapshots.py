"""Immutable pages, current authorization and atomic quotas on real PostgreSQL."""

import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, func, insert, select, update

from src.commands.handler import CommandHandler
from src.commands.principal import (
    TRUSTED_LOCAL, ExecutionPrincipal, PrincipalKind, principal_context,
)
from src.config import AppConfig
from src.database import Database
from src.database import tables as t
from src.database.queries.observation_snapshot_queries import consistent_read
from src.doctor.models import CheckResult, DoctorCheck, Severity
from src.doctor.observations import finding, partition
from src.doctor.runner import DoctorRegistry
from src.models import Project, Task, TaskStatus
from src.observations.snapshots import (
    PAGE_BYTES, RESULT_BYTES, Limits, SnapshotError, SnapshotService, encoded,
)
from src.playbooks.private_values import PrivateOwner, PrivateValueError, PrivateValueService
from src.profiles.capabilities import CapabilityPolicy
from tests.db_fixtures import lease_dsn


@pytest.fixture
async def db():
    database = Database(lease_dsn("observation-snapshots"))
    await database.initialize()
    yield database
    await database.close()


@pytest.fixture
def service(db):
    private = PrivateValueService(db._engine, active_key_id="test", keys={"test": b"x" * 32})
    db.set_playbook_private_values(private)
    return SnapshotService(db._engine, private)


def handler(db):
    config = AppConfig()
    return CommandHandler(SimpleNamespace(db=db, config=config, plugin_registry=None,
                                         doctor_registry=None), config)


async def materialize(service, rows=None, **kwargs):
    rows = rows if rows is not None else [{"id": f"t{i}", "data": "secret"} for i in range(55)]
    args = dict(producer="list_tasks", request={"status": "BLOCKED"}, principal=TRUSTED_LOCAL,
                scope=("system", "system"), collections={"tasks": rows},
                identities=[{"id": row["id"]} for row in rows], coverage=[], complete=True,
                observed_at=time.time(), page_size=20)
    return await service.materialize(**(args | kwargs))


async def read(service, response, token=None, **kwargs):
    args = dict(producer="list_tasks", request={"status": "BLOCKED"}, principal=TRUSTED_LOCAL,
                scope=("system", "system"), snapshot_id=response["snapshot_id"],
                page_token=token or response["page_tokens"][0]["page_token"])
    return await service.read(**(args | kwargs))


async def test_pages_round_trip_immutable_and_encrypted(db, service):
    initial = await materialize(service)
    assert initial["complete"] and initial["tasks"] == []
    assert len(initial["page_tokens"]) == 3
    pages = [await read(service, initial, token["page_token"]) for token in initial["page_tokens"]]
    assert sum(len(page["tasks"]) for page in pages) == 55
    assert all(len(encoded(page)) <= RESULT_BYTES for page in pages)
    assert (await read(service, initial)) == pages[0]
    assert all(page["expires_at"] == initial["expires_at"] for page in pages)
    assert initial["expires_at"] - initial["materialized_at"] == 1800
    async with db._engine.connect() as conn:
        stored = (await conn.execute(select(t.playbook_private_values))).mappings().all()
        assert len(stored) == 3
        assert all(b"secret" not in row["ciphertext"] for row in stored)
        assert "secret" not in str((await conn.execute(select(t.observation_snapshots))).all())
        assert "secret" not in str((await conn.execute(select(t.observation_snapshot_pages))).all())


async def test_api_row_defaults_do_not_inflate_snapshot_pages(db, service):
    from src.api.models.task import ListTasksResponse

    rows = [{"id": f"t{i}", "project_id": "p", "title": "T", "status": "BLOCKED"}
            for i in range(500)]
    initial = await materialize(service, rows=rows, page_size=500)
    page = await read(service, initial)
    api = ListTasksResponse.model_validate(page).model_dump(mode="json")
    assert api["tasks"] == rows
    assert len(encoded(api)) <= RESULT_BYTES
    legacy = ListTasksResponse.model_validate({"tasks": rows[:1]}).model_dump()
    assert "description" in legacy["tasks"][0]
    assert "tasks" not in ListTasksResponse.model_validate(page).model_dump(exclude={"tasks"})


async def test_tokens_bind_producer_request_snapshot_and_authority(db, service):
    initial = await materialize(service)
    other = await materialize(service)
    for kwargs in ({"producer": "list_projects"}, {"request": {"status": "READY"}},
                   {"scope": ("project", "p")}, {"producer_version": 2},
                   {"page_token": other["page_tokens"][0]["page_token"]},
                   {"principal": replace(TRUSTED_LOCAL, kind=PrincipalKind.SERVICE)}):
        with pytest.raises(SnapshotError):
            await read(service, initial, **kwargs)
    principal = ExecutionPrincipal(kind=PrincipalKind.SESSION, session_id="s", project_id="p",
                                   policy=CapabilityPolicy(aq_commands=frozenset({"list_tasks"})))
    scoped = await materialize(service, principal=principal, scope=("project", "p"))
    assert (await read(service, scoped, principal=principal, scope=("project", "p")))["tasks"]
    for changed in (replace(principal, project_id="q"), replace(principal, session_id="other"),
                    replace(principal, policy=CapabilityPolicy())):
        with pytest.raises(SnapshotError):
            await read(service, scoped, principal=changed, scope=("project", "p"))


async def test_expiry_and_missing_page_are_failures(db, service):
    initial = await materialize(service)
    async with db.immediate() as conn:
        await conn.execute(delete(t.observation_snapshot_pages).where(
            t.observation_snapshot_pages.c.ordinal == 1))
    with pytest.raises(SnapshotError, match="page_unavailable"):
        await read(service, initial)
    fresh = await materialize(service)
    async with db.immediate() as conn:
        await conn.execute(update(t.observation_snapshots).values(
            materialized_at=time.time() - 1900, expires_at=time.time() - 1))
    with pytest.raises(SnapshotError, match="expired"):
        await read(service, fresh)
    assert await service.cleanup(limit=1) == 1
    assert await service.cleanup() == 1
    async with db._engine.connect() as conn:
        for table in (t.observation_snapshots, t.observation_snapshot_pages, t.playbook_private_values):
            assert await conn.scalar(select(func.count()).select_from(table)) == 0


async def test_byte_pages_and_oversize_artifact_remain_whole(db, service):
    rows = [{"id": f"t{i}", "data": "å" * 12_000} for i in range(4)]
    initial = await materialize(service, rows=rows)
    assert len(initial["page_tokens"]) == 2
    for token in initial["page_tokens"]:
        page = await read(service, initial, token["page_token"])
        assert len(encoded({"tasks": page["tasks"]})) <= PAGE_BYTES
    huge = [{"id": "big", "data": {"unchanged": "x" * PAGE_BYTES}}]
    initial = await materialize(service, rows=huge)
    assert initial["complete"] is False and initial["error_code"] == "snapshot.row_oversize"
    assert not initial["page_tokens"]
    artifact = initial["artifacts"][0]
    assert await service.private.read(artifact["reference"], owner=PrivateOwner(**artifact["owner"]),
                                      principal=TRUSTED_LOCAL) == {"tasks": huge}
    with pytest.raises(PrivateValueError):
        await service.private.read(artifact["reference"], owner=PrivateOwner(**artifact["owner"]),
                                  principal=replace(TRUSTED_LOCAL, kind=PrincipalKind.SERVICE))


async def test_manifest_oversize_missing_key_and_rollback(db, service, monkeypatch):
    with pytest.raises(SnapshotError, match="manifest_oversize"):
        await materialize(service, identities=[{"id": "x" * RESULT_BYTES}])
    with pytest.raises(PrivateValueError, match="key_unavailable"):
        await materialize(SnapshotService(db._engine, PrivateValueService(db._engine)))
    from src.playbooks.private_values import PrivateValueBatch
    original = PrivateValueBatch.persist

    async def fail_after_insert(self, conn):
        await original(self, conn)
        raise RuntimeError("injected rollback")

    monkeypatch.setattr(PrivateValueBatch, "persist", fail_after_insert)
    with pytest.raises(RuntimeError, match="rollback"):
        await materialize(service)
    async with db._engine.connect() as conn:
        for table in (t.observation_snapshots, t.observation_snapshot_pages, t.playbook_private_values):
            assert await conn.scalar(select(func.count()).select_from(table)) == 0


async def test_atomic_quota_race_and_tightened_read_quota(db, service):
    first = await materialize(service)
    async with db._engine.connect() as conn:
        size = await conn.scalar(select(t.observation_snapshots.c.byte_count))
    limited = SnapshotService(db._engine, service.private, limits=Limits(host=size * 2 + 100))
    results = await asyncio.gather(materialize(limited), materialize(limited), return_exceptions=True)
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, SnapshotError) for result in results) == 1
    limited.limits = Limits(host=1)
    with pytest.raises(SnapshotError, match="quota_exceeded"):
        await read(limited, first)


async def test_expired_payload_counts_toward_quota_until_collected(db, service):
    await materialize(service)
    async with db.immediate() as conn:
        size = await conn.scalar(select(t.observation_snapshots.c.byte_count))
        await conn.execute(update(t.observation_snapshots).values(
            materialized_at=time.time() - 1900, expires_at=time.time() - 1))
    limited = SnapshotService(db._engine, service.private, limits=Limits(host=size + 100))
    with pytest.raises(SnapshotError, match="quota_exceeded"):
        await materialize(limited)
    assert await service.cleanup() == 1
    assert (await materialize(limited))["complete"]


async def test_list_tasks_over_200_and_source_changes_do_not_change_pages(db, service):
    await db.create_project(Project(id="p", name="P"))
    now = time.time()
    async with db.immediate() as conn:
        await conn.execute(insert(t.tasks), [
            {"id": f"t{i:03}", "project_id": "p", "title": "secret task", "description": "",
             "status": "BLOCKED",
             "created_at": now, "updated_at": now} for i in range(251)])
    h = handler(db)
    args = {"pagination": "snapshot", "project_id": "p", "status": "BLOCKED", "page_size": 100}
    result = await h.execute("list_tasks", args)
    assert result["complete"], result
    assert len(result["identity_manifest"]) == 251
    assert len(result["page_tokens"]) == 3
    async with db.immediate() as conn:
        await conn.execute(update(t.tasks).values(status="READY"))
    seen = []
    for token in result["page_tokens"]:
        page = await h.execute("list_tasks", {**args, "snapshot_id": result["snapshot_id"],
                                               "page_token": token["page_token"]})
        assert page["success"], page
        seen.extend(page["tasks"])
    assert len(seen) == 251 and all(row["status"] == "BLOCKED" for row in seen)
    assert (await h.execute("list_tasks", {**args, "display_mode": "tree"}))["success"] is False
    # Default is still the existing live shape and behavior.
    live = await h.execute("list_tasks", {"project_id": "p", "status": "READY"})
    assert "snapshot_id" not in live and len(live["tasks"]) == 251
    async with db.immediate() as conn:
        await conn.execute(update(t.tasks).values(status="COMPLETED"))
    live = await h.execute("list_tasks", {"project_id": "p", "completed_only": True})
    assert live["total"] == 251 and len(live["tasks"]) == 200
    complete = await h.execute("list_tasks", {"project_id": "p", "completed_only": True,
                                              "pagination": "snapshot"})
    assert len(complete["identity_manifest"]) == 251


async def test_projects_and_stuck_tasks_freeze_identities(db, service):
    await db.create_project(Project(id="p", name="P"))
    await db.create_task(Task(id="old", project_id="p", title="old", description="",
                              status=TaskStatus.ASSIGNED,
                              updated_at=time.time() - 10_000))
    async with db._engine.begin() as conn:
        await conn.execute(update(t.tasks).where(t.tasks.c.id == "old")
                           .values(updated_at=time.time() - 10_000))
    h = handler(db)
    for producer, kind in (("list_projects", "projects"), ("get_stuck_tasks", "stuck")):
        args = {"pagination": "snapshot"}
        initial = await h.execute(producer, args)
        assert initial["complete"], initial
        assert initial["identity_manifest"]
        page = await h.execute(producer, {**args, "snapshot_id": initial["snapshot_id"],
                                         "page_token": initial["page_tokens"][0]["page_token"]})
        assert page[kind], page


async def test_task_filters_and_dependencies_are_in_the_frozen_page(db, service):
    await db.create_project(Project(id="p", name="P"))
    for tid in ("a", "b", "c"):
        await db.create_task(Task(id=tid, project_id="p", title=tid, description=""))
    async with db.immediate() as conn:
        await conn.execute(insert(t.task_labels).values(task_id="a", label="watch"))
        await conn.execute(insert(t.task_dependencies).values(task_id="a", depends_on_task_id="b"))
        await conn.execute(insert(t.task_dependencies).values(task_id="a", depends_on_task_id="c",
                                                               dep_type="discovered-from"))
        await conn.execute(insert(t.task_dependencies).values(task_id="c", depends_on_task_id="a"))
    h = handler(db)
    args = {"pagination": "snapshot", "labels": ["watch"], "show_dependencies": True}
    initial = await h.execute("list_tasks", args)
    assert initial["complete"], initial
    assert [row["id"] for row in initial["identity_manifest"]] == ["a"]
    async with db.immediate() as conn:
        await conn.execute(update(t.tasks).values(status="COMPLETED"))
    page = await h.execute("list_tasks", {**args, "snapshot_id": initial["snapshot_id"],
                                         "page_token": initial["page_tokens"][0]["page_token"]})
    row = page["tasks"][0]
    assert row["depends_on"] == [{"id": "b", "title": "b", "status": "DEFINED"}]
    assert row["blocks"] == [{"id": "c", "title": "c", "status": "DEFINED"}]


async def test_repeatable_read_freezes_related_queries(db):
    await db.create_project(Project(id="p", name="P"))
    async with consistent_read(db._engine) as conn:
        assert await conn.scalar(select(t.projects.c.name)) == "P"
        async with db.immediate() as writer:
            await writer.execute(update(t.projects).values(name="changed"))
        assert await conn.scalar(select(t.projects.c.name)) == "P"


async def test_stall_scan_freezes_related_holds(db, monkeypatch):
    from src.database.queries import stall_watch_queries as queries

    await db.create_project(Project(id="p", name="P"))
    await db.create_task(Task(id="t", project_id="p", title="t", description=""))
    original = queries.active_projects

    async def concurrent_hold(conn, project_id, limit=None):
        projects = await original(conn, project_id, limit)
        async with db.immediate() as writer:
            await writer.execute(insert(t.task_labels).values(task_id="t", label="hold:operator"))
        return projects

    monkeypatch.setattr(queries, "active_projects", concurrent_hold)
    frozen = await queries.read_snapshot(db, "p", max_tasks=100)
    assert "t" in frozen.tasks and "t" not in frozen.held
    monkeypatch.setattr(queries, "active_projects", original)
    assert "t" in (await queries.read_snapshot(db, "p", max_tasks=100)).held


async def test_key_rotation_scope_quota_and_source_bounds(db, service):
    from src.database.queries.observation_snapshot_queries import bounded_rows

    initial = await materialize(service)
    rotated = PrivateValueService(db._engine, active_key_id="new",
                                  keys={"test": b"x" * 32, "new": b"n" * 32})
    reader = SnapshotService(db._engine, rotated)
    assert (await read(reader, initial))["tasks"]
    reader.private = PrivateValueService(db._engine, active_key_id="new", keys={"new": b"n" * 32})
    with pytest.raises(PrivateValueError):
        await read(reader, initial)
    limited = SnapshotService(db._engine, service.private, limits=Limits(per_scope=1))
    with pytest.raises(SnapshotError, match="quota_exceeded"):
        await materialize(limited)
    with pytest.raises(SnapshotError, match="quota_exceeded"):
        await read(limited, initial)
    await db.create_project(Project(id="p", name="P"))
    await db.create_project(Project(id="q", name="Q"))
    async with consistent_read(db._engine) as conn:
        with pytest.raises(SnapshotError, match="too_many_rows"):
            await bounded_rows(conn, select(t.projects.c.id), limit=1)


async def test_command_failures_preserve_incomplete_and_maintenance_authority(db, service):
    h = handler(db)
    for args in ({"page_size": 10}, {"pagination": "snapshot", "page_size": True},
                 {"pagination": "snapshot", "status": "invented"},
                 {"pagination": "snapshot", "snapshot_id": "missing"}):
        result = await h.execute("list_tasks", args)
        assert result["success"] is False and result["complete"] is False
        assert not result["page_tokens"]
    principal = ExecutionPrincipal(kind=PrincipalKind.SESSION, session_id="s", project_id="p",
                                   policy=CapabilityPolicy(aq_commands=frozenset({"list_tasks"})))
    with principal_context(principal):
        result = await h._cmd_list_tasks({"pagination": "snapshot", "project_id": "q"})
        assert result["error_code"] == "snapshot.unauthorized"
        assert (await h._cmd_observation_snapshot_cleanup({}))["success"] is False
    assert (await h.execute("observation_snapshot_cleanup", {}))["success"]


@pytest.mark.parametrize("oversize", [False, True])
async def test_doctor_pages_all_findings_and_never_reruns_probe(db, service, oversize):
    items = [finding("test", "broken", "p", "task", f"t{i}", "detail", {"secret": i})
             for i in range(75)]
    data = {"original": items, **({"opaque": "x" * PAGE_BYTES} if oversize else {})}
    probe = AsyncMock(return_value=CheckResult(
        id="test", severity=Severity.WARN, detail="broken", data=data,
        execution_status="succeeded", complete=True,
        coverage=[partition("test", "broken", "p", complete=True, observed=75, total=75)],
    ))
    check = DoctorCheck(id="test", owner="tests", run=probe,
                        read_only_audited=True, supported_scopes=("system",),
                        finding_schema={"kinds": ["broken"]},
                        identity_adapter=lambda result: result.data["original"],
                        completeness_contract="all")
    h = handler(db)
    h._doctor_registry = DoctorRegistry()
    h._doctor_registry.register(check)
    args = {"check_id": "test", "pagination": "snapshot", "page_size": 25}
    initial = await h.execute("doctor_run", args)
    assert initial["complete"] is not oversize, initial
    assert len(initial["identity_manifest"]) == 75
    pages = [await h.execute("doctor_run", {**args, "snapshot_id": initial["snapshot_id"],
                                           "page_token": token["page_token"]})
             for token in initial["page_tokens"]]
    assert sum(len(page["findings"]) for page in pages) == 75
    if oversize:
        assert initial["error_code"] == "snapshot.row_oversize"
        artifact = initial["artifacts"][0]
        recovered = await service.private.read(
            artifact["reference"], owner=PrivateOwner(**artifact["owner"]), principal=TRUSTED_LOCAL)
        assert recovered["checks"][0]["data"] == data
        assert not [c for page in pages for c in page["checks"]]
    else:
        assert [c for page in pages for c in page["checks"]][0]["data"] == data
    probe.assert_awaited_once()


async def test_run_quota_is_shared_across_scopes_and_bound_to_current_run(db, service, monkeypatch):
    import src.observations.snapshots as snapshots

    monkeypatch.setattr(snapshots, "current_invocation", lambda: SimpleNamespace(run_id="run"))
    initial = await materialize(service)
    async with db._engine.connect() as conn:
        size = await conn.scalar(select(t.observation_snapshots.c.byte_count))
    limited = SnapshotService(db._engine, service.private, limits=Limits(per_run=size + 100))
    with pytest.raises(SnapshotError, match="quota_exceeded"):
        await materialize(limited, scope=("project", "p"))
    monkeypatch.setattr(snapshots, "current_invocation", lambda: SimpleNamespace(run_id="other"))
    with pytest.raises(SnapshotError, match="unauthorized"):
        await read(service, initial)
    assert (await materialize(limited, scope=("project", "p")))["complete"]


@pytest.mark.parametrize(("name", "outcome"), [
    ("list_projects", "listed"), ("list_tasks", "listed"), ("get_stuck_tasks", "read"),
])
async def test_typed_adapters_and_api_dtos_preserve_snapshot_receipts(db, service, name, outcome):
    from src.api.models.project import ListProjectsResponse
    from src.api.models.system import GetStuckTasksResponse
    from src.api.models.task import ListTasksResponse
    from src.commands.contracts import CONTRACTS
    from src.commands.contracts.builtin import (
        GetStuckTasksArgs, ListProjectsArgs, ListTasksArgs, set_handler_provider,
    )

    args_models = {"list_projects": ListProjectsArgs, "list_tasks": ListTasksArgs,
                   "get_stuck_tasks": GetStuckTasksArgs}
    dto_models = {"list_projects": ListProjectsResponse, "list_tasks": ListTasksResponse,
                  "get_stuck_tasks": GetStuckTasksResponse}
    set_handler_provider(lambda: handler(db))
    try:
        contract = CONTRACTS.require(name)
        result = await contract.invoke(args_models[name](pagination="snapshot"), None)
        assert result.outcome == outcome
        public = result.value.model_dump()
        assert public["complete"] and public["snapshot_id"] and public["expires_at"]
        assert dto_models[name].model_validate(public).model_dump()["snapshot_id"] == public["snapshot_id"]
        failed = await contract.invoke(args_models[name](pagination="snapshot", snapshot_id="bad",
                                                        page_token="bad"), None)
        assert failed.outcome == "rejected"
        assert failed.value.complete is False and failed.value.error_code == "snapshot.unavailable"
    finally:
        set_handler_provider(None)
