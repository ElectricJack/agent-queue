"""Project policy edits affect existing Git train work without a new batch."""

import asyncio
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import update

from src.database.tables import playbook_activations, playbook_artifacts, projects
from src.integration.batches import Batch, BatchMember, BatchStore
from src.integration.lock import PublishWithdrawn
from src.integration.manual_control import enabled_train_write
from src.integration.train_policy import current_checks, load_train_policy
from src.integration.train_sources import DaemonLanes, DatabaseTargets
from src.playbooks.train_policy import TrainPolicy
from tests.test_delivery_consumers import git
from tests.test_integration_train import (
    MEMBERS,
    ROOT,
    Batches,
    Checks,
    ChecksState,
    Repair,
    Service,
    Targets,
    batch,
    lane,
    train,
)
from tests.test_integration_train_sources import MAIN, completed, hosted_train
from tests.test_integration_train_sources import world as world  # noqa: PLC0414 -- pytest fixture


async def activate(world, tmp_path, *, enabled=True, **settings):
    artifact = json.loads((Path(__file__).parent /
        "fixtures/playbooks/v2/fast-smooth-train/artifact.json").read_text())
    artifact["integration_policy"]["train"] = TrainPolicy(**settings).model_dump(mode="json")
    data = json.dumps(artifact, sort_keys=True).encode()
    sha = "sha256:" + hashlib.sha256(data).hexdigest()
    path = tmp_path / (sha[7:] + ".json")
    path.write_bytes(data)
    async with world.db.immediate() as conn:
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        await conn.execute(pg_insert(playbook_artifacts).values(
            artifact_sha256=sha, playbook_id="fast-smooth-train", scope="project",
            scope_identifier="p", source_digest="sha256:" + "0" * 64,
            contract_fingerprint="test", compiler_build="test", path=str(path),
            created_at=1.0).on_conflict_do_nothing())
    await world.db.set_playbook_activation(
        playbook_id="fast-smooth-train", scope="project", scope_identifier="p",
        artifact_sha256=sha, enabled=enabled, activated_by="test",
        health="ready" if enabled else "disabled", reasons="[]",
    )
    return sha


async def test_disable_stops_targets_freeze_and_publish_but_preserves_sources(world, tmp_path):
    source = await completed(world, "source")
    base = git(world.origin.clone, "rev-parse", source + "^")
    await activate(world, tmp_path)
    assert await DatabaseTargets(world.db).targets(1.0)
    await activate(world, tmp_path, enabled=False)
    assert not await DatabaseTargets(world.db).targets(2.0)
    with pytest.raises(PublishWithdrawn, match="disabled"):
        async with enabled_train_write(world.db, "r"):
            pytest.fail("disabled train acquired publication authority")
    store = BatchStore(world.db, require_enabled=True)
    with pytest.raises(ValueError, match="playbook is off"):
        await store.freeze(Batch("blocked", "p", "r", MAIN.target_ref),
                           (BatchMember("source", source, base),), trees={})
    # Ordinary source publication remains available with the activation off.
    git(world.origin.clone, "push", "origin", source + ":refs/heads/source-after-off")
    await activate(world, tmp_path)
    assert await DatabaseTargets(world.db).targets(3.0)
    assert git(world.origin.url, "rev-parse", "refs/heads/source-after-off") == source
    assert (await load_train_policy(world.db, "unrelated-project")).enabled


async def test_policy_edit_waits_for_existing_publish_fence(world, tmp_path):
    await activate(world, tmp_path)
    async with enabled_train_write(world.db, "r"):
        disable = asyncio.create_task(activate(world, tmp_path, enabled=False))
        await asyncio.sleep(0.05)
        assert not disable.done()
    await asyncio.wait_for(disable, 3)
    assert not (await load_train_policy(world.db, "p")).enabled


async def test_disabled_train_releases_unused_reservations_only(world, tmp_path):
    from sqlalchemy import insert, select

    from src.database.tables import integration_branch_owners
    from src.integration.canonical_reservation import reserve_canonical_task_branch
    from src.integration.train_policy import worker_integration_enabled

    for name in ("idle", "attached", "preparing"):
        await completed(world, name)
    async with world.db.immediate() as conn:
        for name in ("idle", "attached", "preparing"):
            await conn.execute(insert(integration_branch_owners).values(
                id=name, repository_id="r", ref="aq/" + name, owner_id=name,
                owner_role="worker", fence_token=1,
                handoff_state="attached" if name == "attached" else "reserved",
                session_id="live" if name == "attached" else None,
                workspace_id="slot" if name == "preparing" else None,
                created_at=1.0, updated_at=1.0))
    await activate(world, tmp_path, enabled=False)
    assert not await worker_integration_enabled(world.db, await world.db.get_project("p"))
    assert (await reserve_canonical_task_branch(world.db, "idle"))["reason"] == (
        "train playbook is disabled")
    async with world.db._engine.connect() as conn:
        states = dict((await conn.execute(select(
            integration_branch_owners.c.id, integration_branch_owners.c.handoff_state))).all())
    assert states == {"idle": "released", "attached": "attached", "preparing": "reserved"}


async def test_custom_bound_policy_corruption_is_unknown_not_legacy(world, tmp_path):
    sha = await activate(world, tmp_path)
    project = await world.db.get_project("p")
    policy = project.hierarchical_integration_policy or {}
    async with world.db.immediate() as conn:
        await conn.execute(update(projects).where(projects.c.id == "p").values(
            hierarchical_integration_policy={**policy, "train": {"playbook_id": "custom-train"}}))
        await conn.execute(update(playbook_activations).where(
            playbook_activations.c.playbook_id == "fast-smooth-train").values(
                playbook_id="custom-train"))
    assert (await load_train_policy(world.db, "p")).enabled
    for path in tmp_path.glob("*.json"):
        path.write_text("tampered")
    view = await load_train_policy(world.db, "p")
    assert not view.enabled and view.reason == "train_policy_unavailable"
    # Retain an observable target, whose visit will report a finite retry.
    assert await DatabaseTargets(world.db).targets(3.0)
    with pytest.raises(PublishWithdrawn, match="unavailable"):
        async with enabled_train_write(world.db, "r"):
            pytest.fail("unreadable policy authorized publication")
    # Off is explicit intent even when the artifact can no longer be read.
    await world.db.set_playbook_activation(
        playbook_id="custom-train", scope="project", scope_identifier="p",
        artifact_sha256=sha, enabled=False, activated_by="test", health="disabled", reasons="[]")
    assert (await load_train_policy(world.db, "p")).reason == "train_playbook_disabled"


async def test_current_checks_replace_policy_without_changing_frozen_batch(world, tmp_path):
    source = await completed(world, "frozen")
    base = git(world.origin.clone, "rev-parse", source + "^")
    store = BatchStore(world.db)
    frozen = Batch("frozen", "p", "r", MAIN.target_ref)
    members = (BatchMember("frozen", source, base),)
    await store.freeze(frozen, members,
                       trees={"frozen": git(world.origin.clone, "rev-parse", source + "^{tree}")})
    frozen = await store.get("frozen")
    old = {"version": "v1", "names": ["linux", "windows"], "producer_id": "trusted-ci"}
    new = {**old, "version": "v2", "names": ["linux"]}
    await activate(world, tmp_path, required_checks=old)
    assert current_checks({}, await load_train_policy(world.db, "p"))["root"][
        "required_checks"] == old
    await activate(world, tmp_path, required_checks=new)
    owner = object.__new__(DaemonLanes)
    owner.db = world.db
    assert (await owner._policy("p"))["root"]["required_checks"] == new
    assert await store.get("frozen") == frozen
    assert await store.members("frozen") == members


@pytest.mark.parametrize("state", ["red", "conflict"])
async def test_policy_wait_is_bounded_and_does_not_allocate_fixer(state):
    repair = Repair()
    mechanism = Service("conflict" if state == "conflict" else "testing")
    selected = replace(lane(mechanism, Checks(ChecksState.RED)),
                       train_policy=TrainPolicy(red="wait", conflict="wait", wait_seconds=17))
    runtime = train(Targets(ROOT), Batches({ROOT.key: (batch(), MEMBERS)}),
                    {ROOT.key: selected}, repair)
    visit = await runtime.visit(ROOT)
    assert visit.state == state
    assert visit.detail == {"reason": "train_policy_wait", "retry_at": 117.0}
    assert repair.calls == []


async def test_green_wins_even_when_red_policy_waits():
    service = Service("testing", "delivered")
    selected = replace(lane(service, Checks(ChecksState.GREEN)),
                       train_policy=TrainPolicy(red="wait"))
    runtime = train(Targets(ROOT), Batches({ROOT.key: (batch(), MEMBERS)}), {ROOT.key: selected})
    assert (await runtime.visit(ROOT)).state == "delivered"


def test_unknown_policy_fact_has_finite_safe_default():
    policy = TrainPolicy()
    assert policy.action("unavailable") == "wait"
    assert 0 < policy.wait_seconds <= 3600


async def test_pending_candidate_uses_edited_checks_and_lands_same_sha(world, tmp_path):
    await completed(world, "ready")
    runtime, github, _ = await hosted_train(world)
    await activate(world, tmp_path, required_checks={"version": "v1", "names": ["unit"], "producer_id": "15368"})
    pending = await runtime.visit(MAIN)
    assert pending.state == "testing"
    store = BatchStore(world.db)
    members = await store.members(pending.batch_id)
    github.runs[pending.candidate_sha] = "success"
    await activate(world, tmp_path, required_checks={
        "version": "v2", "names": ["unit", "windows"], "producer_id": "15368"})
    waiting = await runtime.visit(MAIN)
    assert waiting.state in {"testing", "repair", "red"}, waiting
    assert waiting.batch_id == pending.batch_id
    assert git(world.origin.url, "rev-parse", "main") != pending.candidate_sha
    await activate(world, tmp_path, required_checks={"version": "v3", "names": ["unit"], "producer_id": "15368"})
    landed = await runtime.visit(MAIN)
    assert landed.state == "delivered"
    assert landed.batch_id == pending.batch_id and landed.candidate_sha == pending.candidate_sha
    assert await store.members(pending.batch_id) == members
    assert git(world.origin.url, "rev-parse", "main") == pending.candidate_sha


async def test_idle_policy_wait_does_not_freeze_then_edit_dispatches(world, tmp_path):
    await completed(world, "ready")
    runtime, _, _ = await hosted_train(world)
    await activate(world, tmp_path, idle="wait", wait_seconds=20)
    paused = await runtime.visit(MAIN)
    assert paused.batch_id is None and paused.detail["reason"] == "train_policy_wait"
    assert paused.detail["retry_at"] > paused.observed_at
    await activate(world, tmp_path)
    assert (await runtime.visit(MAIN)).state == "testing"
