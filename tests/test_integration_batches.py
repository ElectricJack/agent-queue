"""Real Git retries of the shared root/child batch path; disposable PostgreSQL."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from src.database.tables import integration_batches, integration_subject_journal, task_delivery_receipts
from src.integration.batches import Batch, BatchMember, BatchService, BatchStore, candidate_ref, ordered_members
from src.integration.git_truth import GitTruth
from src.integration.gitops import branch
from tests.test_integration_gitops import commit, git, setup as setup


@pytest.fixture
async def batch_env(setup):
    db, ops, _subject, _fence, repo, base, head, _green = setup
    store = BatchStore(db)
    batch = Batch("batch", "p", "r", "refs/heads/main", created_at=1700000000)
    members = (BatchMember("task", head, base),)
    await store.freeze(batch, members, trees={"task": git(repo.store, "rev-parse", f"{head}^{{tree}}")})
    green = set()
    approved = {"value": True}

    async def eligible(batch, members):
        return approved["value"]

    async def gate(batch, sha, tree):
        return sha in green

    async def publish(repo, ref, *, expected_old_oid, new_oid, authorize):
        assert await authorize()
        await ops.push(repo, ref, new_oid, expected_old_oid)

    truth = GitTruth(ops.git)

    async def snapshot(batch=batch):
        return await truth.snapshot(str(repo.store), project_id="p", repository_id="r",
                                    repository_url=str(ops.git.remote_path), target_ref=batch.target_ref)

    service = BatchService(store, ops, publish=publish, eligible=eligible, gate=gate,
                           attest=AsyncMock(return_value="published"))
    return db, ops, repo, base, head, store, batch, members, green, approved, snapshot, service


@pytest.mark.parametrize("target", ["refs/heads/main", "refs/heads/epic"])
async def test_root_and_child_share_exact_candidate_gate_and_publication(batch_env, target):
    db, ops, repo, base, head, store, batch, members, green, _, snapshot, service = batch_env
    if target != batch.target_ref:
        batch = replace(batch, id="child", target_ref=target)
        git(ops.git.remote_path, "update-ref", target, base)
        await store.freeze(batch, members, trees={"task": git(repo.store, "rev-parse", f"{head}^{{tree}}")})
    result = await service.visit(batch, members, await snapshot(batch))
    assert result.state == "testing", result
    assert git(ops.git.remote_path, "rev-parse", target) == base
    green.add(result.candidate_sha)
    result = await service.visit(batch, members, await snapshot(batch))
    assert result.state == "delivered", result
    assert git(ops.git.remote_path, "rev-parse", target) == result.candidate_sha
    # Stale legacy progress cannot interfere and no promotion receipt/journal is written.
    async with db._engine.begin() as conn:
        await conn.execute(update(integration_batches).where(integration_batches.c.id == batch.id)
                           .values(lifecycle="failed", tested_candidate_sha="stale"))
        assert not (await conn.execute(select(integration_subject_journal))).first()
        assert not (await conn.execute(select(task_delivery_receipts))).first()
    assert (await service.visit(batch, members, await snapshot(batch))).state == "delivered"


async def test_restart_and_lost_success_response_use_refs_not_progress(batch_env):
    _, ops, _, _, _, store, batch, members, green, _, snapshot, service = batch_env
    ops.git.ambiguous = True
    result = await service.visit(batch, members, await snapshot())
    assert result.state == "testing"
    green.add(result.candidate_sha)
    restarted = BatchService(store, ops, publish=service.publish, eligible=service.eligible,
                             gate=service.gate, attest=service.attest)
    assert (await restarted.visit(batch, members, await snapshot())).state == "delivered"
    pushes = ops.git.pushes
    assert (await restarted.visit(batch, members, await snapshot())).state == "delivered"
    assert ops.git.pushes == pushes


async def test_target_moves_rebuild_and_retest_exact_sha(batch_env):
    _, ops, repo, base, _, _, batch, members, green, _, snapshot, service = batch_env
    first = await service.visit(batch, members, await snapshot())
    green.add(first.candidate_sha)
    moved = commit(repo.store, {"upstream.txt": "upstream"}, base=base)
    git(repo.store, "push", "origin", f"{moved}:main")
    result = await service.visit(batch, members, await snapshot())
    assert result.state == "testing", result
    assert result.candidate_sha != first.candidate_sha
    assert git(repo.store, "show", f"{result.candidate_sha}:upstream.txt") == "upstream"
    assert git(ops.git.remote_path, "rev-parse", "main") == moved


async def test_lost_candidate_rebuilds_but_explicit_deletion_aborts(batch_env):
    _, ops, _, _, _, store, batch, members, _, _, snapshot, service = batch_env
    first = await service.visit(batch, members, await snapshot())
    ref = candidate_ref(batch.id)
    git(ops.git.remote_path, "update-ref", "-d", ref)
    second = await service.visit(batch, members, await snapshot())
    assert second.state == "testing" and second.candidate_sha == first.candidate_sha
    await store.set_intent(batch.id, "aborted")
    git(ops.git.remote_path, "update-ref", "-d", ref)
    pushes = ops.git.pushes
    assert (await service.visit(batch, members, await snapshot())).state == "held"
    assert ops.git.pushes == pushes
    with pytest.raises(ValueError, match="cannot reopen"):
        await store.set_intent(batch.id, "open")


async def test_green_does_not_override_hold_or_changed_candidate(batch_env):
    _, ops, repo, _, _, _, batch, members, green, approved, snapshot, service = batch_env
    first = await service.visit(batch, members, await snapshot())
    green.add(first.candidate_sha)
    approved["value"] = False
    assert (await service.visit(batch, members, await snapshot())).state == "held"
    approved["value"] = True
    repaired = commit(repo.store, {"repair.txt": "fixed"}, base=first.candidate_sha)
    git(repo.store, "push", "origin", f"{repaired}:{branch(candidate_ref(batch.id))}")
    result = await service.visit(batch, members, await snapshot())
    assert result.state == "testing" and result.candidate_sha == repaired


async def test_squash_delivery_survives_lost_candidate_and_aborted_intent(batch_env):
    _, ops, repo, base, _, store, batch, members, _, _, snapshot, service = batch_env
    result = await ops.merge_sources(repo, base, members, created_at=batch.created_at, squash=True)
    await ops.push(repo, batch.target_ref, result["head"], base)
    await store.set_intent(batch.id, "aborted")
    assert (await service.visit(batch, members, await snapshot())).state == "delivered"


async def test_freeze_replay_rejects_changed_member_and_source_is_retained(batch_env):
    _, _, repo, base, head, store, batch, members, _, _, _, _ = batch_env
    trees = {"task": git(repo.store, "rev-parse", f"{head}^{{tree}}")}
    assert await store.freeze(batch, members, trees=trees) == batch
    with pytest.raises(ValueError, match="different frozen inputs"):
        await store.freeze(batch, (BatchMember("task", base, base),), trees=trees)


async def test_deleted_target_is_never_recreated_from_frozen_members(batch_env):
    _, ops, _, _, _, _, batch, members, _, _, snapshot, service = batch_env
    git(ops.git.remote_path, "update-ref", "-d", batch.target_ref)
    assert (await service.visit(batch, members, await snapshot())).state == "unknown"
    assert ops.git.pushes == 0


async def test_delivered_candidate_cannot_settle_different_frozen_inputs(batch_env):
    _, _, _, base, _, _, batch, members, green, _, snapshot, service = batch_env
    first = await service.visit(batch, members, await snapshot())
    green.add(first.candidate_sha)
    assert (await service.visit(batch, members, await snapshot())).state == "delivered"
    wrong = (BatchMember("task", base, base),)
    assert (await service.visit(batch, wrong, await snapshot())).state == "unknown"


async def test_seal_requires_current_retained_completion_and_keeps_source_history(batch_env):
    from src.integration.delivery_truth import DeliveryRequest
    from src.integration.provenance import CompletedSource, CompletionIdentity, GitProvenance

    _, ops, repo, base, head, _, batch, members, _, _, snapshot, service = batch_env
    identity = CompletionIdentity("p", "r", "task", "close-1")
    provenance = GitProvenance(ops.git, str(repo.store), repository_url=str(ops.git.remote_path))
    await provenance.write_completion(CompletedSource(identity, head))
    request = DeliveryRequest("p", "r", batch.target_ref, "task", 1, "legacy",
                              completion_id="close-1", has_recorded_source=True)
    assert await service.freeze(batch, members, requests={"task": request},
                                snapshot=await snapshot()) == batch
    retained = git(ops.git.remote_path, "rev-parse", f"{identity.ref}^")
    assert retained == head
    assert git(ops.git.remote_path, "merge-base", "--is-ancestor", base, retained) == ""
    with pytest.raises(ValueError, match="not retained"):
        await service.freeze(batch, members, requests={"task": replace(request, completion_id="new")},
                             snapshot=await snapshot())


async def test_crash_before_candidate_push_rebuilds_same_commit_without_allocation(batch_env):
    _, ops, _, _, _, store, batch, members, _, _, snapshot, service = batch_env
    ops.git.fail_push = True
    failed = await service.visit(batch, members, await snapshot())
    assert failed.state == "unknown"
    ops.git.fail_push = False
    result = await service.visit(batch, members, await snapshot())
    assert result.state == "testing" and result.candidate_sha == failed.candidate_sha
    assert (await store.get(batch.id)).repair_attempt_count == 0


@pytest.mark.parametrize("fail_push", [False, True])
async def test_first_member_conflict_publishes_start_without_settling_batch(batch_env, fail_push):
    _, ops, repo, base, _, store, batch, members, _, _, snapshot, service = batch_env
    target = commit(repo.store, {"new.txt": "upstream conflict\n"}, base=base)
    git(repo.store, "push", "origin", f"{target}:main")
    ops.git.fail_push = fail_push
    observed = await service.visit(batch, members, await snapshot())
    assert observed.detail["outcome"] == "conflict"
    assert observed.detail["member"] == "task"
    assert observed.detail["reason"] == "merge_conflict"
    assert observed.detail["files"] == ["new.txt"]
    assert (await store.get(batch.id)).repair_attempt_count == 0
    if fail_push:
        assert observed.state == "unknown"
        assert observed.candidate_sha is None
        assert observed.detail["repair_start_sha"] == target
        assert await ops.remote(repo, candidate_ref(batch.id)) is None
        return
    assert observed.state == "conflict"
    assert observed.candidate_sha == target
    assert await ops.remote(repo, candidate_ref(batch.id)) == target
    # The published start is already in main, but its conflicting source is not.
    # Neither a revisit nor a fresh service may interpret it as a delivery.
    assert (await service.visit(batch, members, await snapshot())).state == "conflict"
    restarted = BatchService(store, ops, publish=service.publish,
                             eligible=service.eligible, gate=service.gate)
    assert (await restarted.visit(batch, members, await snapshot())).state == "conflict"


async def test_repair_authorization_rechecks_exact_remote_head_and_hold(batch_env):
    _, ops, repo, _, _, _, batch, members, _, approved, snapshot, service = batch_env
    observed = await service.visit(batch, members, await snapshot())
    assert await service.repair_authorized(batch, members, observed.candidate_sha)
    approved["value"] = False
    assert not await service.repair_authorized(batch, members, observed.candidate_sha)
    approved["value"] = True
    moved = commit(repo.store, {"repair.txt": "repair"}, base=observed.candidate_sha)
    git(repo.store, "push", "origin", f"{moved}:{candidate_ref(batch.id)}")
    assert not await service.repair_authorized(batch, members, observed.candidate_sha)
    assert await service.repair_authorized(batch, members, moved)
    git(repo.store, "push", "origin", f":{candidate_ref(batch.id)}")
    assert not await service.repair_authorized(batch, members, moved)


async def test_required_gate_can_pause_before_final_publication(batch_env):
    _, ops, _, base, _, store, batch, members, green, _, snapshot, service = batch_env
    first = await service.visit(batch, members, await snapshot())
    green.add(first.candidate_sha)

    async def gate(batch, sha, tree):
        await store.set_intent(batch.id, "paused")
        return True

    service.gate = gate
    assert (await service.visit(batch, members, await snapshot())).state == "held"
    assert git(ops.git.remote_path, "rev-parse", "main") == base


def test_dependency_order_is_stable_and_cycles_fail():
    oid = "a" * 40
    members = [BatchMember(task, oid, oid) for task in ("c", "b", "a")]
    assert [member.task_id for member in ordered_members(members, {"a": ["c"]})] == ["b", "c", "a"]
    with pytest.raises(ValueError, match="cycle"):
        ordered_members(members, {"a": ["c"], "c": ["a"]})


async def test_candidate_app_check_is_published_before_target_push(batch_env, tmp_path):
    from src.integration.attestation import IntegrationAttestationService
    from src.integration.ci import ATTESTATION_CHECK_NAME, IntegrationTrustManifest
    from src.integration.ci_producers import HostedCIProducer
    from src.integration.promotion_contracts import RootAttestationSubject
    from tests.test_integration_train_sources import HostedGitHub

    db, ops, _, base, _, _, batch, members, green, _, snapshot, service = batch_env
    client = HostedGitHub()
    attestation = IntegrationAttestationService(
        db, data_dir=tmp_path, git_manager=ops.git, github_client_factory=None,
    )
    producer = HostedCIProducer(client, IntegrationTrustManifest(
        schema="aq.integration-trust.v1", canonical_repository_id="r", repository_id=123,
        full_name=client.full_name, ci_producer_app_id=15368, attestation_app_id=101,
        attestation_name=ATTESTATION_CHECK_NAME, required_checks={"version": "v1", "names": ["unit"]},
    ))

    async def attest(batch, sha):
        assert git(ops.git.remote_path, "rev-parse", batch.target_ref) == base
        return (await attestation.publish(RootAttestationSubject(
            repository_numeric_id=123, repository_full_name=client.full_name,
            operation_id=batch.id, batch_id=batch.id, revision=batch.repair_attempt_count,
            candidate_sha=sha, required_check_version="v1",
        ), producer=producer)).outcome

    transport = service.publish

    async def publish(repo, ref, **kwargs):
        if ref == batch.target_ref:
            [record] = client.records
            assert record["name"] == ATTESTATION_CHECK_NAME
            assert record["head_sha"] == kwargs["new_oid"]
            assert record["conclusion"] == "success"
        await transport(repo, ref, **kwargs)

    service.attest, service.publish = attest, publish
    testing = await service.visit(batch, members, await snapshot())
    assert client.records == []  # A pending gate cannot attest.
    green.add(testing.candidate_sha)
    client.runs[testing.candidate_sha] = "success"
    result = await service.visit(batch, members, await snapshot())
    assert result.state == "delivered"
    assert git(ops.git.remote_path, "rev-parse", batch.target_ref) == testing.candidate_sha


@pytest.mark.parametrize("outcome", [None, "not_green", "stale", "configuration_blocked", "error"])
async def test_attestation_unavailable_leaves_green_target_unmoved(batch_env, outcome):
    _, ops, _, base, _, _, batch, members, green, _, snapshot, service = batch_env
    testing = await service.visit(batch, members, await snapshot())
    green.add(testing.candidate_sha)
    service.attest = None if outcome is None else AsyncMock(
        return_value=outcome, side_effect=RuntimeError("provider unavailable") if outcome == "error"
        else None,
    )
    pushes = ops.git.pushes
    result = await service.visit(batch, members, await snapshot())
    assert result.state == "held"
    assert result.detail["outcome"] == "attestation_unavailable"
    assert result.candidate_sha == testing.candidate_sha
    assert git(ops.git.remote_path, "rev-parse", batch.target_ref) == base
    assert ops.git.pushes == pushes
    service.attest = AsyncMock(return_value="already_published")
    assert (await service.visit(batch, members, await snapshot())).state == "delivered"


async def test_local_lane_never_calls_attestation_publish(batch_env, monkeypatch):
    from src.integration.train import TrainTarget
    from src.integration.train_sources import DaemonLanes

    db, ops, repo, _, _, _, batch, members, _, _, snapshot, _ = batch_env
    monkeypatch.setattr(db, "get_repo", AsyncMock(return_value=SimpleNamespace(
        url=str(ops.git.remote_path), default_branch="main",
    )))
    monkeypatch.setattr("src.integration.development_runtime.development_repository",
                        AsyncMock(return_value=repo))
    attestation = SimpleNamespace(publish=AsyncMock(), _load_trust=AsyncMock())
    orchestrator = SimpleNamespace(
        db=db, git=ops.git, integration_attestation_service=attestation,
        github_repository_binding_resolver=AsyncMock(return_value=None),
        development_integration=SimpleNamespace(),
    )
    lanes = DaemonLanes(orchestrator, batches=SimpleNamespace(
        eligible=AsyncMock(return_value=True), pr_gate=None,
    ))
    monkeypatch.setattr(lanes, "_policy", AsyncMock(return_value={}))
    monkeypatch.setattr(lanes, "_settings", AsyncMock(return_value=(
        SimpleNamespace(validation="none", commands=()), "v1",
    )))
    lane = await lanes(TrainTarget("p", "r", batch.target_ref, "development"))
    # No validation is a passing local gate; the local lane publishes without an App check.
    testing = await lane.service.visit(batch, members, await snapshot())
    await lane.checks.for_candidate(batch, testing.candidate_sha)
    delivered = await lane.service.visit(batch, members, await snapshot())
    assert delivered.state == "delivered"
    attestation.publish.assert_not_called()
    attestation._load_trust.assert_not_called()
    assert git(ops.git.remote_path, "rev-parse", batch.target_ref) == testing.candidate_sha


@pytest.mark.parametrize("change", ["hold", "candidate"])
async def test_attestation_io_cannot_override_final_authorization(batch_env, change):
    _, ops, repo, base, _, _, batch, members, green, approved, snapshot, service = batch_env
    testing = await service.visit(batch, members, await snapshot())
    green.add(testing.candidate_sha)

    async def attest(batch, sha):
        if change == "hold":
            approved["value"] = False
        else:
            repaired = commit(repo.store, {"repair.txt": "fixed"}, base=sha)
            git(repo.store, "push", "origin", f"{repaired}:{candidate_ref(batch.id)}")
        return "published"

    service.attest = attest
    refused = await service.visit(batch, members, await snapshot())
    assert refused.state == "held"
    assert git(ops.git.remote_path, "rev-parse", batch.target_ref) == base
