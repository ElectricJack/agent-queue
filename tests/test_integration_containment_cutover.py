"""Guarded preparation uses real Git ancestry and disposable PostgreSQL."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, func, insert, select, update

from src.database.queries.hierarchy_queries import ProjectIntegrationMode
from src.database import tables as t
from src.git.manager import GitManager
from src.integration import containment_audit
from src.integration.batches import Batch, BatchMember, BatchStore
from src.integration.containment_cutover import ContainmentCutover, digest
from src.integration.containment_git import ContainmentGit
from src.integration.delivery_branches import completed_branch_dispositions
from src.integration.delivery_observer import DeliveryObserver, PrerequisiteView
from src.integration.delivery_truth import DeliveryState, delivered, load_delivery_requests
from src.integration.delivery_truth import CONTAINMENT_CONTRACT_VERSION
from src.integration.engine import lock_key
from src.integration.git_truth import GitTruth
from src.integration.provenance import CompletionIdentity
from src.integration.selection_metrics import SelectionMetrics, selection_metrics_scope
from src.integration.train import TrainTarget
from src.integration.train_sources import DatabaseBatches
from src.models import Project, RepoConfig, RepoSourceType, Task, TaskStatus
from tests.test_delivery_consumers import Origin, close, git


@pytest.fixture
async def cutover(tmp_path, reuse_database):
    origin = Origin(tmp_path)
    db = await reuse_database("containment-cutover.db")
    await db.create_project(Project(id="p", name="Cutover"))
    await db.create_repo(RepoConfig(
        id="r", project_id="p", source_type=RepoSourceType.CLONE, url=origin.url,
        checkout_base_path=str(origin.clone),
    ))
    async with db._engine.begin() as conn:
        await conn.execute(update(t.projects).where(t.projects.c.id == "p").values(
            integration_repository_id="r", status="PAUSED",
            hierarchical_integration_generation=7,
            hierarchical_integration_policy={"stacked": {"enabled": False}},
            promotion_flow=[{"preserve": "release-binding"}],
        ))
    await db.create_task(Task(id="equivalent", project_id="p", repo_id="r", title="Legacy", description="",
                              branch_name="aq/equivalent", status=TaskStatus.IN_PROGRESS))
    source = origin.work("equivalent")
    await close(db, "equivalent", [source], origin=origin)
    git(origin.clone, "checkout", "-B", "main", "origin/main")
    git(origin.clone, "cherry-pick", "--no-commit", source)
    git(origin.clone, "commit", "-qm", "historical hand merge")
    carrying = git(origin.clone, "rev-parse", "HEAD")
    git(origin.clone, "push", "-q", "origin", "main")
    port = ContainmentGit(protection=AsyncMock(return_value={
        "default_branch": "main", "rulesets": [{"enforcement": "active"}],
    }))
    runtime = {"git_first": "active", "deploy": {"target": "release"}}
    service = ContainmentCutover(db, port, runtime_bindings=lambda: runtime)
    return SimpleNamespace(db=db, origin=origin, source=source, carrying=carrying,
                           port=port, service=service, runtime=runtime)


async def preview(world, **kwargs):
    return (await world.service.run("p", **kwargs))["plan"]


async def reviewed(world):
    plan = await preview(world)
    row = plan["rows"][0]
    assert row["classification"] == "equivalent_only"
    assert row["strict_state"] == "pending"
    return await preview(world, choices={"close-equivalent": {
        "action": "delivered_by_other_means", "source_oid": world.source,
        "carrying_oid": world.carrying, "equivalence_proof": row["legacy_reason"],
        "evidence": "Operator reviewed the historical content and preserved the original object.",
    }})


async def apply(world, plan):
    return await world.service.run("p", dry_run=False, plan=plan,
                                   expected_digest=plan["digest"], operator_id="operator:test")


async def durable(world):
    async with world.db._engine.connect() as conn:
        return {table.name: [dict(r) for r in (await conn.execute(select(table))).mappings()]
                for table in (t.projects, t.tasks, t.task_metadata, t.task_completion_records,
                              t.system_config, t.task_branch_origins)}


def refs(world):
    return git(world.origin.clone, "ls-remote", "origin")


async def test_preview_has_no_durable_mutations_and_preserves_dirty_work(cutover):
    w = cutover
    (w.origin.clone / "base.txt").write_text("dirty tracked work\n")
    (w.origin.clone / "private-draft.txt").write_text("untracked work\n")
    before, remote = await durable(w), refs(w)
    local = git(w.origin.clone, "for-each-ref", "--format=%(refname) %(objectname)")
    index = (w.origin.clone / ".git/index").read_bytes()
    first, second = await preview(w), await preview(w)
    assert first["digest"] == second["digest"]
    assert first["observation_token"] != second["observation_token"]
    assert first["rows"][0]["action"] == "choose"
    assert first["blockers"] == [{"code": "legacy_disposition_required", "task_id": "equivalent"}]
    assert await durable(w) == before and refs(w) == remote
    assert git(w.origin.clone, "for-each-ref", "--format=%(refname) %(objectname)") == local
    assert (w.origin.clone / ".git/index").read_bytes() == index
    assert "private-draft.txt" in first["external"]["checkouts"][0]["status"]


async def test_apply_excludes_legacy_obligation_but_never_proves_containment(cutover):
    w = cutover
    plan = await reviewed(w)
    assert not plan["blockers"]
    remote, before = refs(w), await durable(w)
    result = await apply(w, plan)
    assert result["outcome"] == "prepared" and result["generation"] == 8
    assert (await apply(w, plan))["replayed"] is True
    after = await durable(w)
    prior_project = before["projects"][0]
    assert after["projects"][0] == {**prior_project, "hierarchical_integration_generation": 8}
    for table in ("tasks", "task_metadata", "task_completion_records", "task_branch_origins"):
        assert after[table] == before[table]
    assert refs(w) == remote
    stored = json.loads(after["system_config"][0]["value"])
    assert stored["operator_id"] == "operator:test" and stored["manifest"] == result["plan"]
    assert digest(stored["manifest"]) == plan["digest"]
    requests = await load_delivery_requests(w.db, ["equivalent"], repository_id="r",
                                            target_ref="refs/heads/main")
    observation = await GitTruth(GitManager()).snapshot(
        str(w.origin.clone), project_id="p", repository_id="r", repository_url=w.origin.url,
        target_ref="refs/heads/main",
    )
    answer = await delivered(requests["equivalent"], "refs/heads/main", observation=observation)
    assert answer.state is DeliveryState.PENDING
    assert answer.lifecycle_exclusion == "delivered_by_other_means"
    assert answer.disposition.plan_digest == plan["digest"]
    # Lifecycle exclusions are exact and remain separate from ancestry.
    assert replace(answer, request=replace(answer.request, completion_id="reopened")).disposition is None
    assert replace(answer, source_oid="a" * 40).disposition is None
    assert replace(answer, request=replace(answer.request, target_ref="refs/heads/dev")).disposition is None
    assert replace(answer, request=replace(answer.request, task_status="IN_PROGRESS")).disposition is None


async def consumer_origin(w):
    base = git(w.origin.clone, "rev-parse", w.source + "^")
    async with w.db._engine.begin() as conn:
        await conn.execute(update(t.projects).where(t.projects.c.id == "p").values(promotion_flow=[]))
        await conn.execute(insert(t.task_branch_origins).values(
            id="original", task_id="equivalent", repository_id="r", branch_name="aq/equivalent",
            parent_ref="main", base_sha=base, creation_generation=0, reserved=True,
            materialized=True, created_at=1,
        ))
    return base


async def test_actual_admission_metrics_watchdog_and_freeze_consume_exact_audit(cutover):
    w = cutover
    base = await consumer_origin(w)
    truth = GitTruth(GitManager(), contract_version=CONTAINMENT_CONTRACT_VERSION)
    snapshot = await truth.snapshot(
        str(w.origin.clone), project_id="p", repository_id="r", repository_url=w.origin.url,
        target_ref="refs/heads/main",
    )
    target, batches = TrainTarget("p", "r", "refs/heads/main", "root"), DatabaseBatches(w.db)
    assert await batches._candidate_ids(target, snapshot) == ["equivalent"]
    await apply(w, await reviewed(w))
    observed_delivered, metrics = set(), SelectionMetrics()
    with selection_metrics_scope(metrics):
        assert await batches._candidate_ids(target, snapshot,
                                             delivered_ids=observed_delivered) == []
    assert not observed_delivered
    assert metrics.as_dict()["counts"]["excluded_delivered_by_other_means"] == 1
    # A cached frontier and a previously selected batch both recheck the audit.
    assert await batches.pending(target, snapshot, candidate_ids=["equivalent"], gate_pr=False) is None
    batch = Batch("retired", "p", "r", "refs/heads/main")
    with pytest.raises(ValueError, match="retired by containment cutover"):
        await BatchStore(w.db).freeze(batch, [BatchMember("equivalent", w.source, base)], trees={})
    async with w.db._engine.connect() as conn:
        assert not (await conn.execute(select(t.integration_batches))).all()
        entries = await completed_branch_dispositions(
            conn, heads={"aq/equivalent": w.source}, reachable=AsyncMock(return_value=False),
            contains=AsyncMock(return_value=False), now=1e12, project_id="p", repository_id="r",
        )
        assert [(e["rule"], e["accounted"]) for e in entries] == [
            ("delivered_by_other_means", True),
        ]
        changed = await completed_branch_dispositions(
            conn, heads={"aq/equivalent": base}, reachable=AsyncMock(return_value=False),
            contains=AsyncMock(return_value=False), now=1e12, project_id="p", repository_id="r",
        )
        assert changed[0]["rule"] != "delivered_by_other_means"
    request = (await load_delivery_requests(w.db, ["equivalent"], repository_id="r",
                                           target_ref=target.target_ref))["equivalent"]
    assert (await delivered(request, target.target_ref, observation=snapshot)).state is DeliveryState.PENDING
    # A new close of the same original source is ordinary pending work again.
    await w.db.transition_task("equivalent", TaskStatus.IN_PROGRESS, force=True)
    await close(w.db, "equivalent", [w.source], close_id="later-generation", origin=w.origin)
    assert await batches._candidate_ids(target, snapshot) == ["equivalent"]


async def test_prerequisite_exclusion_releases_obligation_without_parent_containment(cutover, tmp_path):
    w = cutover
    await consumer_origin(w)
    await apply(w, await reviewed(w))
    observer = DeliveryObserver(w.db, git=GitManager(), data_dir=tmp_path / "observer",
                                truth=GitTruth(GitManager(),
                                               contract_version=CONTAINMENT_CONTRACT_VERSION))
    view = await observer.observe(["equivalent"])
    assert view.get("equivalent").state is DeliveryState.PENDING
    prerequisites = PrerequisiteView(view, view, {"dependent": {"equivalent": True}})
    async with w.db._engine.connect() as conn:
        mode = await prerequisites.mode(ProjectIntegrationMode(True, "r"), conn=conn)
    assert mode.delivered_prerequisite_ids == mode.default_prerequisite_ids == {"equivalent"}
    assert not mode.parent_contained_task_ids
    await w.db.transition_task("equivalent", TaskStatus.IN_PROGRESS, force=True)
    async with w.db._engine.connect() as conn:
        mode = await prerequisites.mode(ProjectIntegrationMode(True, "r"), conn=conn)
    assert not mode.delivered_prerequisite_ids and not mode.default_prerequisite_ids


async def test_freeze_holds_cutover_exclusion_through_membership_commit(cutover, monkeypatch):
    w = cutover
    base = await consumer_origin(w)
    store = BatchStore(w.db)
    freeze = store._freeze_on

    async def inspect_lock(*args, **kwargs):
        async with w.db._engine.begin() as other:
            assert not await other.scalar(select(func.pg_try_advisory_xact_lock(lock_key("r"))))
        return await freeze(*args, **kwargs)

    monkeypatch.setattr(store, "_freeze_on", inspect_lock)
    batch = Batch("pending", "p", "r", "refs/heads/main")
    tree = git(w.origin.clone, "rev-parse", w.source + "^{tree}")
    await store.freeze(batch, [BatchMember("equivalent", w.source, base)], trees={"equivalent": tree})
    async with w.db._engine.begin() as conn:
        assert await conn.scalar(select(func.pg_try_advisory_xact_lock(lock_key("r"))))
        assert await conn.scalar(select(t.integration_batch_members.c.task_id)) == "equivalent"


@pytest.mark.parametrize("binding", [{"url": "https://example.test/rebound.git"},
                                    {"default_branch": "different"}])
async def test_repository_rebinding_cannot_inherit_audit_exclusion(cutover, binding):
    w = cutover
    await apply(w, await reviewed(w))
    async with w.db._engine.begin() as conn:
        await conn.execute(update(t.repos).where(t.repos.c.id == "r").values(**binding))
    request = (await load_delivery_requests(w.db, ["equivalent"], repository_id="r",
                                           target_ref="refs/heads/main"))["equivalent"]
    assert request.lifecycle_disposition is None
    assert (await durable(w))["system_config"]


@pytest.mark.parametrize("change", ["generation", "runtime", "source", "project", "remote"])
async def test_apply_refuses_stale_manifest(cutover, change):
    w = cutover
    plan = await reviewed(w)
    if change == "runtime":
        w.runtime["deploy"]["target"] = "different"
    elif change == "remote":
        git(w.origin.clone, "commit", "--allow-empty", "-qm", "target advanced")
        git(w.origin.clone, "push", "-q", "origin", "main")
    elif change == "source":
        await w.db.transition_task("equivalent", TaskStatus.IN_PROGRESS)
    else:
        values = ({"hierarchical_integration_generation": 8} if change == "generation"
                  else {"status": "ACTIVE"})
        async with w.db._engine.begin() as conn:
            await conn.execute(update(t.projects).where(t.projects.c.id == "p").values(**values))
    before = await durable(w)
    with pytest.raises(ValueError, match="stale_plan"):
        await apply(w, plan)
    assert await durable(w) == before


async def test_apply_refuses_live_writer_even_with_fresh_digest(cutover):
    w = cutover
    await w.db.create_task(Task(id="writer", project_id="p", repo_id="r", title="Writing", description="",
                               status=TaskStatus.IN_PROGRESS))
    plan = await reviewed(w)
    assert {"code": "active_task", "id": "writer"} in plan["blockers"]
    before = await durable(w)
    assert (await apply(w, plan))["outcome"] == "blocked"
    assert await durable(w) == before


async def test_apply_rechecks_writer_and_config_after_git_retention(cutover):
    w = cutover
    plan = await reviewed(w)
    retain = w.port.retain

    async def race(snapshot, rows):
        retained = await retain(snapshot, rows)
        await w.db.create_task(Task(id="racer", project_id="p", repo_id="r", title="Race", description="",
                                   status=TaskStatus.IN_PROGRESS))
        return retained

    w.port.retain = race
    with pytest.raises(ValueError, match="stale_plan"):
        await apply(w, plan)
    assert not (await durable(w))["system_config"]


async def test_remote_writer_during_retention_refuses_apply(cutover):
    w = cutover
    plan = await reviewed(w)
    retain = w.port.retain

    async def race(snapshot, rows):
        retained = await retain(snapshot, rows)
        # A source branch changes without any target movement or database write.
        git(w.origin.clone, "push", "origin", "main:refs/heads/unexpected-writer")
        return retained

    w.port.retain = race
    with pytest.raises(ValueError, match="external_fence_changed"):
        await apply(w, plan)
    assert not (await durable(w))["system_config"]


async def test_legacy_private_source_is_retained_without_deleting_original(cutover):
    w = cutover
    identity = CompletionIdentity("p", "r", "equivalent", "close-equivalent")
    marker = git(w.origin.clone, "rev-parse", identity.ref)
    git(w.origin.clone, "push", "origin", marker + ":refs/heads/" + identity.legacy_branch,
        ":" + identity.ref)
    plan = await reviewed(w)
    assert (await apply(w, plan))["outcome"] == "prepared"
    remote = refs(w)
    assert marker + "\t" + identity.ref in remote
    assert marker + "\trefs/heads/" + identity.legacy_branch in remote


@pytest.mark.parametrize("state,desired,blocked", [
    ("running", "stopped", True), ("stopped", "running", True),
    ("stopped", "stopped", False),
])
async def test_preparation_requires_confirmed_termination(cutover, state, desired, blocked):
    w = cutover
    async with w.db._engine.begin() as conn:
        await conn.execute(insert(t.sessions).values(
            id="preparing", project_id="p", profile_id="test", harness="fake", provider="fake",
            name="preparing", lifecycle="pool", state=state, desired_state=desired,
            claim_phase="preparing", work_dir=str(w.origin.clone), epoch="e",
            instance_token="test", started_at=1,
        ))
    plan = await reviewed(w)
    blockers = [b for b in plan["blockers"] if b["code"] == "live_sessions"]
    assert bool(blockers) is blocked
    assert (await apply(w, plan))["outcome"] == ("blocked" if blocked else "prepared")


async def test_missing_protection_cannot_apply(cutover):
    w = cutover
    w.port.protection = AsyncMock(return_value=None)
    plan = await reviewed(w)
    assert {"code": "protection_unavailable"} in plan["blockers"]
    assert (await apply(w, plan))["outcome"] == "blocked"


async def test_ambiguous_source_blocks_apply(cutover):
    w = cutover
    identity = CompletionIdentity("p", "r", "equivalent", "close-equivalent")
    git(w.origin.clone, "push", "origin", ":" + identity.ref)
    plan = await preview(w)
    assert plan["rows"][0]["classification"] == "missing_or_ambiguous_source"
    assert {"code": "unresolved_history", "task_id": "equivalent"} in plan["blockers"]
    assert (await apply(w, plan))["outcome"] == "blocked"


async def test_reconstruction_is_explicit_blocker_until_reviewed_merge(cutover):
    w = cutover
    plan = await preview(w, choices={"close-equivalent": {"action": "reconstruct"}})
    assert plan["rows"][0]["action"] == "reconstruct_reviewed_merge_then_preview"
    assert (await apply(w, plan))["outcome"] == "blocked"
    w.origin.land("equivalent")
    plan = await preview(w)
    assert plan["rows"][0]["classification"] == "already_contained"
    assert (await apply(w, plan))["outcome"] == "prepared"
    assert json.loads((await durable(w))["system_config"][0]["value"])["dispositions"] == []


@pytest.mark.parametrize("field,value", [
    ("source_oid", "a" * 40), ("carrying_oid", "1234567"),
    ("equivalence_proof", "invented"), ("evidence", ""),
])
async def test_legacy_disposition_requires_exact_evidence(cutover, field, value):
    w = cutover
    choices = (await reviewed(w))["choices"]
    choices["close-equivalent"][field] = value
    plan = await preview(w, choices=choices)
    assert {"code": "legacy_evidence_invalid", "task_id": "equivalent"} in plan["blockers"]
    assert (await apply(w, plan))["outcome"] == "blocked"


async def test_carrying_commit_must_actually_contain_equivalent_content(cutover):
    w = cutover
    choices = (await reviewed(w))["choices"]
    choices["close-equivalent"]["carrying_oid"] = git(w.origin.clone, "rev-parse", "main^")
    plan = await preview(w, choices=choices)
    assert {"code": "carrying_commit_unverified", "task_id": "equivalent"} in plan["blockers"]


async def test_digest_required_and_tampered_plan_refused(cutover):
    w = cutover
    with pytest.raises(ValueError, match="plan_digest_required"):
        await w.service.run("p", dry_run=False, operator_id="operator")
    plan = await reviewed(w)
    plan["rows"][0]["source_oid"] = "a" * 40
    with pytest.raises(ValueError, match="plan_digest_required"):
        await apply(w, plan)
    plan["digest"] = digest(plan)
    with pytest.raises(ValueError, match="stale_plan"):
        await apply(w, plan)


async def test_source_retention_failure_cannot_commit_disposition(cutover):
    w = cutover
    plan = await reviewed(w)
    w.port.retain = AsyncMock(side_effect=ValueError("retention failed"))
    before = await durable(w)
    with pytest.raises(ValueError, match="retention failed"):
        await apply(w, plan)
    assert await durable(w) == before


async def test_later_equivalent_generation_cannot_receive_one_time_exclusion(cutover):
    w = cutover
    plan = await reviewed(w)
    await apply(w, plan)
    await w.db.transition_task("equivalent", TaskStatus.IN_PROGRESS)
    await close(w.db, "equivalent", [w.source], close_id="later", origin=w.origin)
    with pytest.raises(ValueError, match="already_prepared"):
        await apply(w, plan)
    newer = await preview(w)
    assert newer["rows"][0]["classification"] == "equivalent_only"
    with pytest.raises(ValueError, match="already_prepared"):
        await apply(w, newer)


async def test_disposition_audit_survives_archive_without_task_metadata(cutover):
    w = cutover
    await apply(w, await reviewed(w))
    async with w.db._engine.begin() as conn:
        row = dict((await conn.execute(select(t.tasks).where(
            t.tasks.c.id == "equivalent"))).mappings().one())
        archived = {key: value for key, value in row.items() if key in t.archived_tasks.c}
        archived.update(archived_at=100)
        await conn.execute(insert(t.archived_tasks).values(**archived))
        await conn.execute(delete(t.task_metadata).where(t.task_metadata.c.task_id == "equivalent"))
        await conn.execute(delete(t.tasks).where(t.tasks.c.id == "equivalent"))
    request = (await load_delivery_requests(w.db, ["equivalent"], repository_id="r",
                                           target_ref="refs/heads/main"))["equivalent"]
    assert request.archived and request.disposition.matches(request)


async def test_task_metadata_cannot_forge_operator_disposition(cutover):
    from src.integration.delivery_truth import DELIVERY_DISPOSITION_KEY

    w = cutover
    plan = await reviewed(w)
    choice = plan["choices"]["close-equivalent"]
    forged = {"project_id": "p", "repository_id": "r", "task_id": "equivalent",
              "completion_id": "close-equivalent", "target_ref": "refs/heads/main",
              "source_oid": w.source, "carrying_oid": w.carrying,
              "equivalence_proof": choice["equivalence_proof"], "plan_digest": plan["digest"],
              "kind": "delivered_by_other_means"}
    async with w.db._engine.begin() as conn:
        await conn.execute(insert(t.task_metadata).values(
            task_id="equivalent", key=DELIVERY_DISPOSITION_KEY, value=json.dumps(forged),
        ))
    request = (await load_delivery_requests(w.db, ["equivalent"], repository_id="r",
                                           target_ref="refs/heads/main"))["equivalent"]
    assert request.disposition is None


async def test_audit_rejects_namespace_confusion_corruption_and_growth(cutover, monkeypatch):
    plan = await reviewed(cutover)
    audit = containment_audit.build_audit(plan, "operator:test", 123)
    key = containment_audit.audit_key("p", "r")
    raw = containment_audit.canonical(audit)
    assert len(key) == len(containment_audit.NAMESPACE) + 64
    assert containment_audit.validate_audit(key, raw) == audit
    bad_values = [raw[:-1], '{"schema":1,"schema":2}', "[]"]
    for field, value in (
        ("project_id", "other"), ("repository_id", "other"), ("plan_digest", "a" * 64),
        ("configuration_generation", 9), ("configuration_generation", True),
        ("operator_id", ""), ("operator_id", "x\n"), ("applied_at", float("nan")),
        ("dispositions", []), ("schema", "unrecognized"),
    ):
        bad_values.append(json.dumps({**audit, field: value}))
    changed = deepcopy(audit)
    changed["manifest"]["rows"][0]["source_oid"] = "a" * 40
    bad_values.append(json.dumps(changed))
    for bad in bad_values:
        with pytest.raises(ValueError, match="invalid_containment_audit"):
            containment_audit.validate_audit(key, bad)
    for wrong_key in ("ordinary", containment_audit.audit_key("p", "other"), key + "extra"):
        with pytest.raises(ValueError, match="invalid_containment_audit"):
            containment_audit.validate_audit(wrong_key, raw)
    monkeypatch.setattr(containment_audit, "MAX_GENERATIONS", 0)
    with pytest.raises(ValueError, match="invalid_containment_audit"):
        containment_audit.validate_audit(key, raw)
    monkeypatch.setattr(containment_audit, "MAX_GENERATIONS", 10_000)
    monkeypatch.setattr(containment_audit, "MAX_AUDIT_BYTES", len(raw.encode()) - 1)
    with pytest.raises(ValueError, match="invalid_containment_audit"):
        containment_audit.validate_audit(key, raw)
    cutover.port.retain = AsyncMock()
    with pytest.raises(ValueError, match="invalid_containment_audit"):
        await apply(cutover, plan)
    cutover.port.retain.assert_not_awaited()
    assert not (await durable(cutover))["system_config"]


async def test_audit_insert_cannot_overwrite_and_corruption_fails_closed(cutover):
    from sqlalchemy.exc import IntegrityError

    w = cutover
    await apply(w, await reviewed(w))
    before = await durable(w)
    stored = before["system_config"][0]
    audit = json.loads(stored["value"])
    with pytest.raises(IntegrityError):
        async with w.db._engine.begin() as conn:
            await containment_audit.insert_audit(conn, {**audit, "operator_id": "replacement"})
    assert await durable(w) == before
    async with w.db._engine.begin() as conn:
        await conn.execute(update(t.system_config).where(t.system_config.c.key == stored["key"])
                           .values(value='{"dispositions": ["forged"]}'))
    with pytest.raises(ValueError, match="invalid_containment_audit"):
        await load_delivery_requests(w.db, ["equivalent"], repository_id="r",
                                     target_ref="refs/heads/main")


async def test_concurrent_identical_apply_is_one_immutable_audit(cutover):
    import asyncio

    plan = await reviewed(cutover)
    results = await asyncio.gather(apply(cutover, plan), apply(cutover, plan))
    assert sorted(result["replayed"] for result in results) == [False, True]
    after = await durable(cutover)
    assert len(after["system_config"]) == 1
    assert after["projects"][0]["hierarchical_integration_generation"] == 8


async def test_ordinary_config_surfaces_cannot_overwrite_or_read_audit(cutover, tmp_path):
    from src.commands.system_commands import SystemCommandsMixin
    from src.config import AppConfig

    w = cutover
    await apply(w, await reviewed(w))
    before = (await durable(w))["system_config"]
    key = before[0]["key"]
    config_path = tmp_path / "ordinary-config.yaml"
    config_path.write_text("{}\n")
    config = AppConfig(data_dir=str(tmp_path / "data"))
    config._config_path = str(config_path)
    handler = SimpleNamespace(config=config, db=w.db,
                              orchestrator=SimpleNamespace(config=config, config_watcher=None))
    result = await SystemCommandsMixin._cmd_get_config(handler, {"section": key})
    assert result["config"][key] is None
    await SystemCommandsMixin._cmd_update_config(handler, {
        "section": key, "data": {"dispositions": ["forged"]},
    })
    # Both insert and update paths use other exact namespaces, even for this provider input.
    await w.db.record_probe_health(key, {"ok": True})
    await w.db.record_probe_health(key, {"ok": False})
    await w.db.bump_daemon_start_count()
    await w.db.bump_daemon_start_count()
    after = [row for row in (await durable(w))["system_config"] if row["key"] == key]
    assert after == before


@pytest.mark.parametrize("kind,elevated", [("session", False), ("session", True)])
async def test_operator_only_command_refuses_before_any_reads(kind, elevated):
    from src.commands.integration_commands import IntegrationCommandsMixin
    from src.commands.principal import ExecutionPrincipal, PrincipalKind, TRUSTED_LOCAL, principal_context

    db = SimpleNamespace(get_project=AsyncMock())
    principal = ExecutionPrincipal(PrincipalKind(kind), TRUSTED_LOCAL.policy,
                                   project_id="p", session_id="worker", elevated=elevated)
    with principal_context(principal):
        result = await IntegrationCommandsMixin._cmd_integration_cutover_containment(
            SimpleNamespace(db=db), {"project_id": "p"})
    assert not result["success"] and "local operator" in result["error"]
    db.get_project.assert_not_awaited()


def test_cli_requires_explicit_plan_digest_before_apply():
    from click.testing import CliRunner
    from src.cli.app import cli

    result = CliRunner().invoke(cli, ["integration", "cutover-containment", "--project", "p", "--apply"])
    assert result.exit_code == 2 and "--digest" in result.output
