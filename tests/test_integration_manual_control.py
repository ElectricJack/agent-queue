"""Guarded manual integration over PostgreSQL and real Git."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert, select, update

from src.commands.contracts.integration import IntegrationOffArgs
from src.commands.integration_commands import IntegrationCommandsMixin
from src.commands.principal import PrincipalKind, TRUSTED_LOCAL, principal_context
from src.database import tables as t
from src.integration.batches import Batch, BatchMember, BatchStore
from src.integration.gitops import SubjectGitAuthority
from src.integration.lock import PublishWithdrawn
from src.integration.manual_control import ManualIntegration, enabled_train_write
from src.integration.ownership import StaleFence
from src.integration.train_sources import (
    DatabaseTargets, LeasedPublish,
)
from tests.test_delivery_consumers import git
from tests.test_integration_train_sources import MAIN, completed
from tests.test_integration_train_sources import world as world


OPERATOR = "human:local-operator"


async def off(world):
    service = ManualIntegration(world.db)
    request = IntegrationOffArgs(project_id="p", reason="manual recovery")
    preview = await service.run(request, operator_id=OPERATOR)
    await service.run(request.model_copy(update={
        "dry_run": False, "expected_plan_digest": preview["plan_digest"],
    }), operator_id=OPERATOR)


async def apply(service, request):
    preview = await service.run(request, operator_id=OPERATOR)
    applying = request.model_copy(update={"dry_run": False,
                                          "expected_plan_digest": preview["plan_digest"]})
    return await service.run(applying, operator_id=OPERATOR), applying


async def test_off_preserves_frozen_work_and_stale_preview_refuses(world):
    source = await completed(world, "frozen")
    base = git(world.origin.clone, "rev-parse", source + "^")
    batch = Batch("frozen-batch", "p", "r", MAIN.target_ref, created_at=1.0)
    members = (BatchMember("frozen", source, base),)
    store = BatchStore(world.db)
    tree = git(world.origin.clone, "rev-parse", source + "^{tree}")
    await store.freeze(batch, members, trees={"frozen": tree})
    async with world.db.immediate() as conn:
        await conn.execute(insert(t.task_metadata).values(
            task_id="frozen", key="claimed_by_session", value='"held-worker"'))
        await conn.execute(insert(t.task_labels).values(task_id="frozen", label="hold:human"))
        await conn.execute(insert(t.project_integration_schedules).values(
            project_id="p", enabled=True, interval_seconds=60, next_due_at=1.0, updated_at=1.0))
        before_tasks = (await conn.execute(select(t.tasks))).mappings().all()
        before_claims = (await conn.execute(select(t.task_metadata))).mappings().all()
    before_refs = git(world.origin.url, "show-ref")
    service = ManualIntegration(world.db)
    request = IntegrationOffArgs(project_id="p", reason="operator requested manual mode")
    preview = await service.run(request, operator_id=OPERATOR)
    assert (await world.db.get_project("p")).hierarchical_integration_mode == "train"
    result, applying = await apply(service, request)
    assert result["outcome"] == "disabled"
    assert preview["plan_digest"] == result["plan_digest"]
    project = await world.db.get_project("p")
    assert project.hierarchical_integration_mode == "disabled"
    assert project.hierarchical_integration_generation == 1
    assert await store.get(batch.id) == batch
    assert await store.members(batch.id) == members
    assert git(world.origin.url, "show-ref") == before_refs
    async with world.db._engine.connect() as conn:
        assert (await conn.execute(select(t.tasks))).mappings().all() == before_tasks
        assert (await conn.execute(select(t.task_metadata))).mappings().all() == before_claims
        assert await conn.scalar(select(t.task_labels.c.label)) == "hold:human"
        assert await conn.scalar(select(t.project_integration_schedules.c.enabled)) is False
        assert await conn.scalar(select(t.project_integration_schedules.c.next_due_at)) == 1.0
    assert await DatabaseTargets(world.db).targets(1.0) == []
    # Reconciler primitives use the same repository exclusion, with their
    # durable authority rechecked before transport rather than LeasedPublish.
    authority = SubjectGitAuthority(world.db, trusted_green=AsyncMock())
    with pytest.raises(StaleFence, match="automatic integration is off"):
        async with authority.mutation(SimpleNamespace(project_id="p")):
            pytest.fail("off must refuse before a reconciler transport can start")
    with pytest.raises(ValueError, match="plan changed"):
        await service.run(applying, operator_id=OPERATOR)
    guarded = BatchStore(world.db, require_enabled=True)
    with pytest.raises(ValueError, match="integration is off"):
        await guarded.freeze(replace(batch, id="another"), members, trees={"frozen": tree})


async def test_off_waits_for_entered_writer_then_refuses_all_train_pushes(world):
    entered, release = asyncio.Event(), asyncio.Event()

    async def writer():
        async with enabled_train_write(world.db, "r"):
            entered.set()
            await release.wait()

    writing = asyncio.create_task(writer())
    await asyncio.wait_for(entered.wait(), 5)
    disabling = asyncio.create_task(off(world))
    # Observe a real PostgreSQL waiter rather than relying on a timing guess.
    from sqlalchemy import text

    async with asyncio.timeout(5):
        while True:
            async with world.db._engine.connect() as conn:
                waiting = await conn.scalar(text(
                    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype='advisory' "
                    "AND NOT granted AND database=(SELECT oid FROM pg_database "
                    "WHERE datname=current_database()))"))
            if waiting:
                break
            await asyncio.sleep(0.01)
    assert not disabling.done()
    release.set()
    await writing
    await disabling
    publisher = LeasedPublish(world.db, AsyncMock())
    repo = SimpleNamespace(repository_id="r")
    for call in (
        publisher(repo, "refs/heads/main", expected_old_oid="a" * 40, new_oid="b" * 40,
                  authorize=AsyncMock(return_value=True)),
        publisher.qualified(repo, "refs/heads/main", "refs/tags/attestation",
                            expected_old_oid="a" * 40, new_oid="b" * 40,
                            authorize=AsyncMock(return_value=True)),
        publisher.delete(repo, "refs/heads/private", expected_old_oid="a" * 40,
                         authorize=AsyncMock(return_value=True)),
    ):
        with pytest.raises(PublishWithdrawn, match="integration is off"):
            await call
    # A completed worker source can still publish and close in manual mode.
    await completed(world, "ordinary-worker")


@pytest.mark.parametrize("change", ["generation", "repository_url", "default_branch"])
async def test_off_refuses_changed_configuration_after_preview(world, change):
    service = ManualIntegration(world.db)
    request = IntegrationOffArgs(project_id="p", reason="manual recovery")
    preview = await service.run(request, operator_id=OPERATOR)
    async with world.db.immediate() as conn:
        if change == "generation":
            await conn.execute(update(t.projects).where(t.projects.c.id == "p").values(
                hierarchical_integration_generation=1))
        else:
            values = {"url": "https://example.test/changed.git"} if change == "repository_url" else {
                "default_branch": "changed"}
            await conn.execute(update(t.repos).where(t.repos.c.id == "r").values(**values))
    with pytest.raises(ValueError, match="plan changed"):
        await service.run(request.model_copy(update={"dry_run": False,
            "expected_plan_digest": preview["plan_digest"]}), operator_id=OPERATOR)
    assert (await world.db.get_project("p")).hierarchical_integration_mode == "train"


async def test_off_requires_local_principal(world):
    class Handler(IntegrationCommandsMixin):
        db = world.db

    handler = Handler()
    args = {"project_id": "p", "reason": "manual"}
    assert not (await handler._cmd_integration_off(args))["success"]
    for kind in (PrincipalKind.SESSION, PrincipalKind.SERVICE, PrincipalKind.PLAYBOOK):
        with principal_context(replace(TRUSTED_LOCAL, kind=kind)):
            assert not (await handler._cmd_integration_off(args))["success"]
    with principal_context(TRUSTED_LOCAL):
        assert (await handler._cmd_integration_off(args))["outcome"] == "preview"


async def test_command_handler_off_and_status_use_existing_disabled_representation(world):
    import json

    from src.commands import CommandHandler
    from src.config import AppConfig

    handler = CommandHandler(SimpleNamespace(db=world.db), AppConfig())
    args = {"project_id": "p", "reason": "manual recovery"}
    preview = await handler.execute("integration_off", args)
    assert preview["success"] and preview["outcome"] == "preview"
    async with world.db._engine.connect() as conn:
        assert not await conn.scalar(select(t.events.c.id).where(
            t.events.c.event_type == "integration.operator_off"))
    result = await handler.execute("integration_off", args | {
        "dry_run": False, "expected_plan_digest": preview["plan_digest"]})
    assert result["success"] and result["outcome"] == "disabled"
    status = await handler.execute("integration_status", {"project_id": "p"})
    assert status["effective_mode"] == status["desired_mode"] == "disabled"
    assert status["generation"] == preview["plan"]["generation"] + 1
    async with world.db._engine.connect() as conn:
        payload = json.loads(await conn.scalar(select(t.events.c.payload).where(
            t.events.c.event_type == "integration.operator_off")))
    assert payload["principal"] == OPERATOR and payload["plan_digest"] == preview["plan_digest"]
