"""Manual mode and one-time legacy retirement over PostgreSQL and real Git."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert, select, update

from src.commands.contracts.integration import IntegrationOffArgs, IntegrationRetireLegacyArgs
from src.commands.integration_commands import IntegrationCommandsMixin
from src.commands.principal import PrincipalKind, TRUSTED_LOCAL, principal_context
from src.database import tables as t
from src.git.manager import GitError
from src.integration.batches import Batch, BatchMember, BatchService, BatchStore
from src.integration.delivery_branches import completed_branch_dispositions
from src.integration.delivery_observer import DeliveryTarget
from src.integration.delivery_truth import DeliveryState, load_delivery_requests
from src.integration.gitops import SubjectGitAuthority
from src.integration.lock import PublishWithdrawn
from src.integration.manual_control import ManualIntegration, enabled_train_write
from src.integration.ownership import StaleFence
from src.integration.provenance import CompletionIdentity
from src.integration.train_sources import (
    DatabaseBatches, DatabaseTargets, LeasedPublish, project_delivered,
)
from tests.test_delivery_consumers import close, git
from tests.test_integration_train_sources import MAIN, completed
from tests.test_integration_train_sources import origin_template as origin_template
from tests.test_integration_train_sources import world as world


OPERATOR = "human:local-operator"


async def off(world):
    service = ManualIntegration(world.db)
    request = IntegrationOffArgs(project_id="p", reason="manual recovery")
    preview = await service.run(request, operator_id=OPERATOR)
    await service.run(request.model_copy(update={
        "dry_run": False, "expected_plan_digest": preview["plan_digest"],
    }), operator_id=OPERATOR)


async def equivalent(world):
    source = await completed(world, "legacy")
    origin = world.origin
    git(origin.clone, "checkout", "-q", "-B", "main", "origin/main")
    git(origin.clone, "merge", "--squash", "origin/aq/legacy")
    git(origin.clone, "commit", "-q", "-m", "manual equivalent landing")
    content = git(origin.clone, "rev-parse", "HEAD")
    git(origin.clone, "push", "-q", "origin", "main")
    await off(world)
    return IntegrationRetireLegacyArgs(project_id="p", reason="hand landed after green CI",
        entries=[{"task_id": "legacy", "completion_id": "close-legacy",
                  "source_sha": source, "content_sha": content}])


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


async def test_legacy_retirement_is_not_delivery_or_a_new_close(world):
    request = await equivalent(world)
    service = ManualIntegration(world.db)
    preview = await service.run(request, operator_id=OPERATOR)
    async with world.db._engine.connect() as conn:
        assert not await conn.scalar(select(t.integration_delivery_cutovers.c.project_id))
        before = (await conn.execute(select(t.task_completion_records))).mappings().all()
    result, applying = await apply(service, request)
    assert result["plan_digest"] == preview["plan_digest"]
    assert result["plan"]["entries"][0]["equivalence"] in {"whole_source_patch", "full_tree"}
    assert await service.run(applying, operator_id=OPERATOR) == result
    async with world.db._engine.connect() as conn:
        assert (await conn.execute(select(t.task_completion_records))).mappings().all() == before
    loaded = await load_delivery_requests(world.db, ["legacy"], repository_id="r",
                                           target_ref=MAIN.target_ref, reduced=True)
    current = loaded["legacy"]
    assert current.legacy_disposition.kind == "delivered_by_other_means"
    snapshot = await world.db._delivery_observer.snapshot(DeliveryTarget(
        project_id="p", repository_id="r", repository_url=world.origin.url,
        target_ref=MAIN.target_ref))
    evidence = await snapshot.is_delivered(current)
    assert evidence.state is DeliveryState.PENDING and not evidence.satisfied
    assert (await snapshot.observation.evaluate(current)).state is DeliveryState.PENDING
    assert await project_delivered(world.db, ["legacy"], project_id="p", repository_id="r",
                                   target_ref=MAIN.target_ref, snapshot=snapshot) == set()
    assert await DatabaseBatches(world.db)._candidate_ids(MAIN, snapshot) == []
    async with world.db._engine.connect() as conn:
        rows = await completed_branch_dispositions(conn,
            heads={"aq/legacy": request.entries[0].source_sha},
            reachable=AsyncMock(return_value=False), contains=AsyncMock(return_value=False),
            now=9999999999, project_id="p", repository_id="r")
    assert rows[0]["rule"] == "delivered_by_other_means" and rows[0]["accounted"]
    original = CompletionIdentity("p", "r", "legacy", "close-legacy")
    assert git(world.origin.url, "rev-parse", original.ref) == result["plan"]["entries"][0][
        "retained_oid"]
    other = await load_delivery_requests(world.db, ["legacy"], repository_id="r",
                                         target_ref="refs/heads/epic", reduced=True)
    assert other["legacy"].legacy_disposition is None
    new_source = world.origin.work("legacy", "second")
    await close(world.db, "legacy", [new_source], close_id="new-close", origin=world.origin)
    later = await load_delivery_requests(world.db, ["legacy"], repository_id="r",
                                         target_ref=MAIN.target_ref, reduced=True)
    assert later["legacy"].legacy_disposition is None
    assert later["legacy"].strict_containment
    extending = request.model_copy(update={"entries": [request.entries[0].model_copy(
        update={"completion_id": "new-close", "source_sha": new_source})]})
    with pytest.raises(ValueError, match="immutable legacy cutover"):
        await service.run(extending, operator_id=OPERATOR)


@pytest.mark.parametrize("change", ["generation", "task", "target", "original"])
async def test_retirement_refuses_preview_drift(world, change):
    request = await equivalent(world)
    service = ManualIntegration(world.db)
    preview = await service.run(request, operator_id=OPERATOR)
    if change == "generation":
        async with world.db.immediate() as conn:
            await conn.execute(update(t.projects).where(t.projects.c.id == "p").values(
                hierarchical_integration_generation=2))
    elif change == "task":
        await world.db.update_task("legacy", title="changed after preview")
    elif change == "target":
        world.origin.work("unrelated")
        world.origin.land("unrelated")
    else:
        identity = CompletionIdentity("p", "r", "legacy", "close-legacy")
        git(world.origin.url, "update-ref", "-d", identity.ref)
    with pytest.raises(ValueError, match="plan changed|unknown|retention"):
        await service.run(request.model_copy(update={"dry_run": False,
            "expected_plan_digest": preview["plan_digest"]}), operator_id=OPERATOR)
    async with world.db._engine.connect() as conn:
        assert not await conn.scalar(select(t.integration_delivery_cutovers.c.project_id))


@pytest.mark.parametrize("blocker", ["hold", "gate", "frozen", "mode", "missing", "foreign",
                                     "claim", "unknown_content", "non_equivalent"])
async def test_retirement_preserves_and_refuses_blockers(world, blocker):
    request = await equivalent(world)
    async with world.db.immediate() as conn:
        if blocker == "hold":
            await conn.execute(insert(t.task_labels).values(task_id="legacy", label="hold:human"))
        elif blocker == "gate":
            await conn.execute(insert(t.gates).values(id="human", project_id="p", gate_type="review",
                title="Human review", await_id="review", created_at=1.0, status="open"))
            await conn.execute(insert(t.task_gates).values(task_id="legacy", gate_id="human"))
        elif blocker == "mode":
            await conn.execute(update(t.projects).where(t.projects.c.id == "p").values(
                hierarchical_integration_mode="train"))
        elif blocker == "foreign":
            await conn.execute(update(t.tasks).where(t.tasks.c.id == "legacy").values(repo_id=None))
        elif blocker == "claim":
            await conn.execute(insert(t.task_metadata).values(
                task_id="legacy", key="claimed_by_session", value='"unresolved-holder"'))
    if blocker == "frozen":
        source = request.entries[0].source_sha
        base = git(world.origin.clone, "rev-parse", source + "^")
        await BatchStore(world.db).freeze(Batch("frozen", "p", "r", MAIN.target_ref, created_at=1.0),
            (BatchMember("legacy", source, base),),
            trees={"legacy": git(world.origin.clone, "rev-parse", source + "^{tree}")})
    elif blocker == "missing":
        git(world.origin.url, "update-ref", "-d", CompletionIdentity("p", "r", "legacy",
            "close-legacy").ref)
    elif blocker in {"unknown_content", "non_equivalent"}:
        content = ("a" * 40 if blocker == "unknown_content" else git(
            world.origin.clone, "rev-parse", request.entries[0].source_sha + "^"))
        request = request.model_copy(update={"entries": [request.entries[0].model_copy(
            update={"content_sha": content})]})
    with pytest.raises((ValueError, GitError)):
        await ManualIntegration(world.db).run(request, operator_id=OPERATOR)


async def test_manual_commands_require_local_principal(world):
    class Handler(IntegrationCommandsMixin):
        db = world.db

    handler = Handler()
    args = {"project_id": "p", "reason": "manual"}
    assert not (await handler._cmd_integration_off(args))["success"]
    assert not (await handler._cmd_integration_retire_legacy(args))["success"]
    for kind in (PrincipalKind.SESSION, PrincipalKind.SERVICE, PrincipalKind.PLAYBOOK):
        with principal_context(replace(TRUSTED_LOCAL, kind=kind)):
            assert not (await handler._cmd_integration_off(args))["success"]
            assert not (await handler._cmd_integration_retire_legacy(args))["success"]
    with principal_context(TRUSTED_LOCAL):
        assert (await handler._cmd_integration_off(args))["outcome"] == "preview"


async def test_retirement_refuses_an_observation_from_another_binding(world):
    request = await equivalent(world)

    async def wrong_snapshot(target):
        snapshot = await world.db._delivery_observer.snapshot(target)
        return replace(snapshot, observation=replace(snapshot.observation, repository_id="foreign"))

    with pytest.raises(ValueError, match="another repository or target binding"):
        await ManualIntegration(world.db, snapshot=wrong_snapshot).run(request, operator_id=OPERATOR)
    async with world.db._engine.connect() as conn:
        assert not await conn.scalar(select(t.integration_delivery_cutovers.c.project_id))


async def test_later_equivalent_generation_stays_pending_after_cutover(world):
    request = await equivalent(world)
    await apply(ManualIntegration(world.db), request)
    await close(world.db, "legacy", [request.entries[0].source_sha],
                close_id="later-equivalent", origin=world.origin)
    loaded = await load_delivery_requests(world.db, ["legacy"], repository_id="r",
                                          target_ref=MAIN.target_ref, reduced=True)
    current = loaded["legacy"]
    assert current.strict_containment and current.legacy_disposition is None
    snapshot = await world.db._delivery_observer.snapshot(DeliveryTarget(
        "p", "r", world.origin.url, MAIN.target_ref))
    base = git(world.origin.clone, "rev-parse", request.entries[0].source_sha + "^")
    evidence = await snapshot.is_delivered(current, source_base=base)
    assert evidence.state is DeliveryState.PENDING and not evidence.satisfied
    assert (await snapshot.observation.evaluate(current)).state is DeliveryState.PENDING
    assert await snapshot.contains_source("legacy", request.entries[0].source_sha, base,
                                          strict=True) is False
    assert await DatabaseBatches(world.db)._candidate_ids(MAIN, snapshot) == ["legacy"]
    store = BatchStore(world.db)
    batch = Batch("later-equivalent", "p", "r", MAIN.target_ref)
    member = BatchMember("legacy", request.entries[0].source_sha, base)
    await store.freeze(batch, (member,), trees={"legacy": git(world.origin.clone,
        "rev-parse", member.source_sha + "^{tree}")})
    service = BatchService(store, SimpleNamespace(
        repository=AsyncMock(return_value=SimpleNamespace()), validate_repository=AsyncMock()),
        publish=AsyncMock(), eligible=AsyncMock(return_value=False), gate=AsyncMock())
    assert (await service.visit(batch, (member,), snapshot)).state == "held"


async def test_unselected_equivalent_source_does_not_inherit_retirement(world):
    request = await equivalent(world)
    await apply(ManualIntegration(world.db), request)
    source = request.entries[0].source_sha
    await completed(world, "unselected", head=source)
    loaded = await load_delivery_requests(world.db, ["unselected"], repository_id="r",
                                          target_ref=MAIN.target_ref, reduced=True)
    current = loaded["unselected"]
    assert current.strict_containment and current.legacy_disposition is None
    snapshot = await world.db._delivery_observer.snapshot(DeliveryTarget(
        "p", "r", world.origin.url, MAIN.target_ref))
    assert (await snapshot.is_delivered(current)).state is DeliveryState.PENDING


async def test_command_handler_dispatches_manual_preview_and_retirement(world):
    from src.commands import CommandHandler
    from src.config import AppConfig

    request = await equivalent(world)
    handler = CommandHandler(SimpleNamespace(db=world.db), AppConfig())
    preview = await handler.execute("integration_retire_legacy", request.model_dump())
    assert preview["success"] and preview["outcome"] == "preview"
    result = await handler.execute("integration_retire_legacy", request.model_copy(update={
        "dry_run": False, "expected_plan_digest": preview["plan_digest"],
    }).model_dump())
    assert result["success"] and result["outcome"] == "retired"
