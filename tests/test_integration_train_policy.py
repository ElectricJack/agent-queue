"""Project policy edits affect existing Git train work without a new batch."""

import asyncio
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import select, update

from src.database.tables import (
    playbook_activations,
    playbook_artifacts,
    projects,
    task_branch_origins,
    task_integration_checkpoints,
)
from src.integration.batches import Batch, BatchMember, BatchStore
from src.integration.branch_materialization import BranchMaterializationService
from src.integration.delivery_watchdog import _file_takeover_on
from src.integration.lock import PublishWithdrawn
from src.integration.manual_control import enabled_train_write
from src.integration.train_policy import current_checks, load_train_policy
from src.integration.train_sources import DaemonLanes, DatabaseTargets
from src.models import Task, TaskStatus
from src.playbooks.train_policy import TrainBisectionPolicy, TrainPackingPolicy, TrainPolicy
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
from tests.test_integration_train_sources import MAIN, completed, hosted_train, legacy_hierarchy
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


@pytest.mark.parametrize("mode", ["train", "hierarchy"])
async def test_reenabled_train_backfills_unreserved_roots_and_restores_frontier(world, tmp_path, mode):
    db = world.db
    await db.update_project("p", hierarchical_integration_mode=mode)
    await activate(world, tmp_path, enabled=False)
    published = world.origin.work("published")
    for name in ("paused", "published"):
        await db.create_task(Task(id=name, project_id="p", title=name, description="off filing",
                                  status=TaskStatus.READY, branch_name="aq/" + name))
    hierarchy = legacy_hierarchy(world)
    async with db.immediate() as conn:
        assert (await hierarchy.reserve_ready_root_origin_on(conn, "paused"))["outcome"] == (
            "not_eligible")
        takeover = await _file_takeover_on(
            db, conn, key="off-generation",
            entry={"project_id": "p", "task_id": "old", "branch": "aq/old",
                   "head": "a" * 40, "reason": "publisher_unavailable"},
            repository_id="r", target_ref="refs/heads/main", hierarchy=hierarchy)
    drain = BranchMaterializationService(
        db, hierarchy_service_factory=lambda: hierarchy, legacy_container_collection=False)
    assert await drain.drain_due() == []
    async with db._engine.connect() as conn:
        assert not (await conn.execute(select(task_branch_origins))).all()
    await activate(world, tmp_path)
    assert await db.count_ready_by_profile("p") == {}
    await drain.drain_due()
    assert await db.count_ready_by_profile("p") == {None: 3}
    async with db._engine.connect() as conn:
        origins = {row["task_id"]: dict(row) for row in (await conn.execute(
            select(task_branch_origins))).mappings()}
        checkpoint = (await conn.execute(select(task_integration_checkpoints).where(
            task_integration_checkpoints.c.task_id == "published"))).mappings().one()
    assert checkpoint["checkpoint_sha"] == published
    assert git(world.origin.url, "rev-parse", "aq/published") == published
    assert git(world.origin.url, "rev-parse", "aq/paused") == origins["paused"]["base_sha"]
    assert git(world.origin.url, "rev-parse", "aq/" + takeover) == origins[takeover]["base_sha"]
    assert all(row["materialized"] for row in origins.values())
    assert await drain.drain_due() == []
    async with db._engine.connect() as conn:
        assert {row["task_id"]: dict(row) for row in (await conn.execute(
            select(task_branch_origins))).mappings()} == origins


@pytest.mark.parametrize("filer", ["root", "system", "watchdog"])
async def test_fresh_root_reaches_frontier_without_operator_reservation(world, tmp_path, filer):
    await activate(world, tmp_path)
    hierarchy = legacy_hierarchy(world)
    async with world.db.immediate() as conn:
        if filer == "watchdog":
            task_id = await _file_takeover_on(
                world.db, conn, key="stuck-generation",
                entry={"project_id": "p", "task_id": "old", "branch": "aq/old",
                       "head": "a" * 40, "reason": "publisher_unavailable"},
                repository_id="r", target_ref="refs/heads/main", hierarchy=hierarchy)
        else:
            task = Task(id="fresh", project_id="p", title="fresh", description="fresh root",
                        status=TaskStatus.READY)
            if filer == "root":
                await hierarchy.file_root_on(conn, task)
            else:
                await world.db.create_task(task, conn=conn)
            task_id = task.id
        origin = (await conn.execute(select(task_branch_origins).where(
            task_branch_origins.c.task_id == task_id))).mappings().one_or_none()
        assert (origin is not None) == (filer != "system")
    assert await world.db.count_ready_by_profile("p") == {}
    drain = BranchMaterializationService(
        world.db, hierarchy_service_factory=lambda: hierarchy, legacy_container_collection=False)
    await drain.drain_due()
    assert await world.db.count_ready_by_profile("p") == {None: 1}
    task = await world.db.get_task(task_id)
    assert git(world.origin.url, "rev-parse", task.branch_name)


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


def test_packing_contract_defaults():
    assert TrainPackingPolicy().model_dump() == {
        "signature_version": 1, "policy_version": 1, "enabled": False,
        "max_members": 4, "green_weight": 100, "merge_clean_weight": 40,
        "signature_size_weight": 10, "nonhot_weight": 10, "age_per_hour": 1.0,
        "max_age_score": 80, "pass_rate_weight": 10, "pass_rate_neutral": 5,
        "pass_rate_min_checks": 20, "hotness_window_batches": 50,
        "oldest_slot_after_seconds": 7200,
    }
    assert TrainBisectionPolicy().model_dump() == {"enabled": False, "max_probes": 3}


@pytest.mark.parametrize("field,low,high", [
    ("max_members", 1, 8), ("green_weight", 0, 1000), ("merge_clean_weight", 0, 1000),
    ("signature_size_weight", 0, 100), ("nonhot_weight", 0, 100),
    ("age_per_hour", 0, 100), ("max_age_score", 0, 1000),
    ("pass_rate_weight", 0, 100), ("pass_rate_neutral", 0, 100),
    ("pass_rate_min_checks", 20, 10000), ("hotness_window_batches", 1, 1000),
    ("oldest_slot_after_seconds", 1, 7200),
])
def test_packing_numeric_bounds_are_strict(field, low, high):
    # Keep the cross-field neutral bound valid while testing individual ranges.
    settings = {"pass_rate_neutral": 0, "pass_rate_weight": 100}
    for value in (low, high):
        assert getattr(TrainPackingPolicy(**(settings | {field: value})), field) == value
    for value in (low - 1, high + 1, True, str(low), float("inf"), float("nan")):
        with pytest.raises(ValidationError):
            TrainPackingPolicy(**(settings | {field: value}))


@pytest.mark.parametrize("field", ["signature_version", "policy_version"])
@pytest.mark.parametrize("value", [0, 2, True, "1", 1.0])
def test_packing_rejects_unsupported_or_coerced_versions(field, value):
    with pytest.raises(ValidationError):
        TrainPackingPolicy(**{field: value})


@pytest.mark.parametrize("settings", [
    {"enabled": "false"}, {"enabled": 1}, {"online_learning": True},
    {"pass_rate_neutral": 11}, {"max_members": 4.0},
])
def test_packing_rejects_ambiguous_or_unknown_policy(settings):
    with pytest.raises(ValidationError):
        TrainPackingPolicy(**settings)


@pytest.mark.parametrize("settings", [
    {"max_probes": 0}, {"max_probes": 4}, {"max_probes": True},
    {"max_probes": "3"}, {"enabled": "false"}, {"adaptive_budget": True},
])
def test_bisection_cannot_exceed_three_probes_or_coerce_policy(settings):
    with pytest.raises(ValidationError):
        TrainBisectionPolicy(**settings)


@pytest.mark.parametrize("value", [0, 1, "false", "true"])
def test_parallel_mode_requires_explicit_boolean(value):
    with pytest.raises(ValidationError):
        TrainPolicy(parallel_disjoint=value)


@pytest.mark.parametrize("parallel,legacy,valid", [
    (False, False, True), (True, False, True), (False, True, True), (True, True, False),
])
async def test_activated_policy_rejects_conflicting_lane_modes(
    world, tmp_path, parallel, legacy, valid,
):
    await world.db.update_project("p", hierarchical_integration_policy={
        "train": {"speculative_ci": legacy},
    })
    digest = await activate(world, tmp_path, parallel_disjoint=parallel)
    view = await load_train_policy(world.db, "p")
    assert view.enabled is valid
    assert view.reason == (None if valid else "train_policy_modes_conflict")
    assert view.policy_revision == (digest if valid else None)


async def test_policy_revision_changes_with_tuning_without_enabling_packing(world, tmp_path):
    first = await activate(world, tmp_path, packing={})
    before = await load_train_policy(world.db, "p")
    second = await activate(world, tmp_path, packing={"max_members": 8})
    after = await load_train_policy(world.db, "p")
    assert first != second
    assert before.policy_revision == first and after.policy_revision == second
    assert before.policy.packing.signature_version == after.policy.packing.signature_version == 1
    assert before.policy.packing.policy_version == after.policy.packing.policy_version == 1
    assert not before.policy.packing.enabled and not after.policy.packing.enabled


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
