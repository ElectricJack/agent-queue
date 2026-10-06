"""Real Git/PostgreSQL promotion crash matrix (dev-branch releases §3.3)."""

import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert, select, update

from src.database.tables import agent_profiles, projects, repos, task_context, tasks
from src.git.manager import GitError
from src.integration.batches import Batch, BatchMember, BatchStore
from src.integration.git_truth import GitTruth
from src.integration.ci import ATTESTATION_CHECK_NAME, IntegrationTrustManifest
from src.integration.provenance import CompletionIdentity
from src.integration.promotion_steps import (
    FlowSchema, PromotionChecks, PromotionIntentInvalid, PromotionVisit, StepAdmission,
    promotion_ref, settle_promotion,
)
from tests.test_integration_gitops import commit, git, setup as setup
from tests.test_integration_train_sources import HostedGitHub


@pytest.fixture
async def promotion(setup, request):
    db, ops, _, _, repo, base, source, _ = setup
    # Only replace GitHub credential selection: all object/ref operations use real Git.
    destination = ops.git._apush_destination

    async def local_destination(path, remote, **kwargs):
        return await destination(path, remote, repository_url=str(ops.git.remote_path))

    ops.git._apush_destination = local_destination
    step = FlowSchema.validate([{
        "id": "release", "source": "dev", "target": "main",
        "versioning": {"kind": "semver_tag", "source": "pyproject"},
    }], default_branch="dev").flow[0]
    step.update(getattr(request, "param", {}))
    batch = Batch("promotion-batch", "p", "r", "refs/heads/main", created_at=1700000000)
    member = BatchMember("promotion-task", source, base)
    meta = {
        "request_id": "promotion:r:release:1.2.3", "repository_id": "r",
        "target_ref": batch.target_ref, "source_sha": source, "base_sha": base,
        "step": step, "version": None if step["versioning"]["kind"] == "none" else "1.2.3",
        "checks_version": "checks-v1",
        "requested_at": batch.created_at,
    }
    async with db._engine.begin() as conn:
        await conn.execute(insert(projects).values(id="p", name="p", created_at=1))
        await conn.execute(insert(repos).values(
            id="r", project_id="p", url=str(ops.git.remote_path), checkout_base_path=str(repo.store),
        ))
        await conn.execute(insert(tasks).values(
            id=member.task_id, project_id="p", repo_id="r", title="Promote release", description="",
            task_type="promotion", status="IN_PROGRESS", created_at=1, updated_at=1,
        ))
        await conn.execute(insert(task_context).values(
            id="promotion-context", task_id=member.task_id, type="promotion_intent",
            content=json.dumps(meta), created_at=1,
        ))
    store = BatchStore(db)
    batch = await store.freeze(batch, (member,), trees={
        member.task_id: git(repo.store, "rev-parse", f"{source}^{{tree}}"),
    }, promotion=meta)
    admission = StepAdmission(db, ops, step)
    await admission.retain(batch, member, meta["request_id"])
    gate = SimpleNamespace(green=True)
    exact = SimpleNamespace(read=AsyncMock(side_effect=lambda _: SimpleNamespace(green=gate.green)))
    checks = PromotionChecks(admission, AsyncMock(return_value=exact))
    truth = GitTruth(ops.git)

    async def snapshot():
        return await truth.snapshot(
            str(repo.store), project_id="p", repository_id="r",
            repository_url=str(ops.git.remote_path), target_ref=batch.target_ref,
        )

    async def publish(repo, ref, *, expected_old_oid, new_oid, authorize):
        if not await authorize():
            raise GitError("withdrawn")
        await ops.push(repo, ref, new_oid, expected_old_oid)

    async def publish_tag(repo, target, ref, *, expected_old_oid, new_oid, authorize):
        if not await authorize():
            raise GitError("withdrawn")
        await ops.git.apush_qualified_ref(
            str(repo.store), ref=ref, tip_oid=new_oid, expected_old_oid=expected_old_oid,
        )

    service = PromotionVisit(
        store, ops, admission=admission, checks=checks, publish=publish,
        publish_tag=publish_tag, attest=AsyncMock(return_value="published"), snapshot=snapshot,
    )
    return SimpleNamespace(
        db=db, ops=ops, repo=repo, base=base, source=source, batch=batch, member=member,
        meta=meta, store=store, admission=admission, checks=checks, gate=gate,
        snapshot=snapshot, service=service,
    )


async def visit(env):
    return await env.service.visit(env.batch, (env.member,), await env.snapshot())


async def update_meta(env, **changes):
    env.meta.update(changes)
    async with env.db._engine.begin() as conn:
        await conn.execute(update(task_context).values(content=json.dumps(env.meta)))


async def test_cached_source_checks_publish_exact_source_and_annotated_tag(promotion):
    e = promotion
    result = await visit(e)
    assert result.state == "delivered", result
    assert git(e.ops.git.remote_path, "rev-parse", "main") == e.source
    assert git(e.ops.git.remote_path, "cat-file", "-t", "refs/tags/v1.2.3") == "tag"
    assert git(e.ops.git.remote_path, "rev-parse", "refs/tags/v1.2.3^{}") == e.source
    assert f"AQ-Promotion: {e.member.task_id}@{e.source}" in git(
        e.ops.git.remote_path, "cat-file", "-p", "refs/tags/v1.2.3",
    )
    assert not git(e.ops.git.remote_path, "for-each-ref", "refs/heads/aq/promote/")
    await settle_promotion(e.db, e.batch, result)
    async with e.db._engine.connect() as conn:
        assert await conn.scalar(select(tasks.c.status)) == "COMPLETED"
        meta = json.loads(await conn.scalar(select(task_context.c.content)))
        assert meta["tag_oid"] == result.detail["tag_oid"]
    # Restart after both writes, before/after settlement needs no further publication.
    pushes = e.ops.git.pushes
    assert (await visit(e)).state == "delivered"
    assert e.ops.git.pushes == pushes


async def test_missing_checks_create_exact_promotion_ref_then_resume(promotion):
    e = promotion
    e.gate.green = False
    assert (await visit(e)).state == "testing"
    assert git(e.ops.git.remote_path, "rev-parse", "main") == e.base
    ref = promotion_ref(e.meta["step"], e.meta)
    assert git(e.ops.git.remote_path, "rev-parse", ref) == e.source
    assert not git(e.ops.git.remote_path, "for-each-ref", "refs/tags/")
    e.gate.green = True
    assert (await visit(e)).state == "delivered"
    assert not git(e.ops.git.remote_path, "for-each-ref", ref)


async def test_crash_before_branch_write_retries_without_publishing_a_tag(promotion):
    e = promotion
    original = e.service.publish
    e.service.publish = AsyncMock(side_effect=GitError("crash before branch write"))
    assert (await visit(e)).state == "unknown"
    assert git(e.ops.git.remote_path, "rev-parse", "main") == e.base
    assert not git(e.ops.git.remote_path, "for-each-ref", "refs/tags/")
    e.service.publish = original
    assert (await visit(e)).state == "delivered"


async def test_crash_after_branch_before_tag_recovers_without_branch_rewrite(promotion):
    e = promotion
    e.service.publish_tag = AsyncMock(side_effect=GitError("lost connection before tag write"))
    assert (await visit(e)).state == "unknown"
    assert git(e.ops.git.remote_path, "rev-parse", "main") == e.source
    assert not git(e.ops.git.remote_path, "for-each-ref", "refs/tags/")
    pushes = e.ops.git.pushes

    async def recover(repo, target, ref, **args):
        assert await args["authorize"]()
        await e.ops.git.apush_qualified_ref(str(repo.store), ref=ref, tip_oid=args["new_oid"],
                                          expected_old_oid=args["expected_old_oid"])
    e.service.publish_tag = recover
    e.gate.green = False
    e.service.attest = AsyncMock(side_effect=AssertionError("must not repeat attestation"))
    assert (await visit(e)).state == "delivered"
    assert e.ops.git.pushes == pushes


async def test_crash_after_tag_write_before_response_reads_back_success(promotion):
    e = promotion
    original = e.service.publish_tag

    async def uncertain(*args, **kwargs):
        await original(*args, **kwargs)
        raise GitError("lost successful tag response")

    e.service.publish_tag = uncertain
    assert (await visit(e)).state == "delivered"


@pytest.mark.parametrize("kind", ["lightweight", "wrong-source", "wrong-identity"])
async def test_existing_tag_conflicts_are_immutable(promotion, kind):
    e = promotion
    if kind == "lightweight":
        git(e.repo.store, "tag", "v1.2.3", e.source)
    else:
        git(e.repo.store, "tag", "-a", "v1.2.3", "-m", "unrelated release",
            e.base if kind == "wrong-source" else e.source)
    git(e.repo.store, "push", "origin", "refs/tags/v1.2.3")
    old = git(e.ops.git.remote_path, "rev-parse", "refs/tags/v1.2.3")
    result = await visit(e)
    assert result.state == "held" and result.detail["reason"] == "promotion_tag_conflict"
    assert git(e.ops.git.remote_path, "rev-parse", "refs/tags/v1.2.3") == old
    assert git(e.ops.git.remote_path, "rev-parse", "main") == e.base


async def test_tag_present_branch_absent_recovers_fast_forward(promotion):
    e = promotion
    assert (await visit(e)).state == "delivered"
    tag_oid = git(e.ops.git.remote_path, "rev-parse", "refs/tags/v1.2.3")
    git(e.ops.git.remote_path, "update-ref", "refs/heads/main", e.base)
    assert (await visit(e)).state == "delivered"
    assert git(e.ops.git.remote_path, "rev-parse", "refs/tags/v1.2.3") == tag_oid


@pytest.mark.parametrize("promotion", [{"versioning": {"kind": "none"}}], indirect=True)
async def test_unversioned_step_uses_fast_forward_without_any_tag(promotion):
    e = promotion
    result = await visit(e)
    assert result.state == "delivered" and result.detail["tag_oid"] is None
    assert git(e.ops.git.remote_path, "rev-parse", "main") == e.source
    assert not git(e.ops.git.remote_path, "for-each-ref", "refs/tags/")


async def test_diverged_target_is_held_without_creating_merge_commit(promotion):
    e = promotion
    other = commit(e.repo.store, {"other.txt": "other"}, base=e.base)
    git(e.repo.store, "push", "origin", f"{other}:main")
    result = await visit(e)
    assert result.state == "held" and result.detail["reason"] == "promotion_not_fast_forward"
    assert git(e.ops.git.remote_path, "rev-parse", "main") == other
    assert not git(e.ops.git.remote_path, "for-each-ref", "refs/tags/")


@pytest.mark.parametrize("change", ["source", "step", "profile", "status", "provenance", "version"])
async def test_admission_refuses_changed_or_unretained_inputs(promotion, change):
    e = promotion
    if change == "source":
        await update_meta(e, source_sha=e.base)
    elif change == "version":
        await update_meta(e, version="9.9.9")
    elif change == "step":
        step = copy.deepcopy(e.meta["step"])
        step["gate"]["checks"] = "manifest:other"
        await update_meta(e, step=step)
    elif change == "provenance":
        identity = CompletionIdentity("p", "r", e.member.task_id, "promotion:" + e.meta["request_id"])
        git(e.ops.git.remote_path, "update-ref", "-d", "refs/heads/" + identity.branch)
    else:
        async with e.db._engine.begin() as conn:
            if change == "profile":
                await conn.execute(insert(agent_profiles).values(
                    id="worker", name="Worker", description="", model="test",
                    created_at=1, updated_at=1,
                ))
            await conn.execute(update(tasks).values(**(
                {"profile_id": "worker", "route_source": "override"}
                if change == "profile" else {"status": "CANCELLED"}
            )))
    with pytest.raises(PromotionIntentInvalid):
        await e.admission.load(e.batch, (e.member,))
    assert (await visit(e)).state == "held"
    assert git(e.ops.git.remote_path, "rev-parse", "main") == e.base


async def test_hold_after_attestation_withdraws_both_publications(promotion):
    e = promotion

    async def attest(*args):
        await e.store.set_intent(e.batch.id, "paused")
        return "published"

    e.service.attest = attest
    assert (await visit(e)).state != "delivered"
    assert git(e.ops.git.remote_path, "rev-parse", "main") == e.base
    assert not git(e.ops.git.remote_path, "for-each-ref", "refs/tags/")


async def test_settlement_requires_git_truth_even_with_source_and_tag_present(promotion):
    e = promotion
    assert (await visit(e)).state == "delivered"
    real_snapshot = await e.snapshot()
    disputed = SimpleNamespace(
        observation=real_snapshot.observation, target_oid=e.source, error=None,
        contains_source=AsyncMock(return_value=False),
    )
    e.service.snapshot = AsyncMock(return_value=disputed)
    result = await e.service.visit(e.batch, (e.member,), disputed)
    assert result.state != "delivered"
    async with e.db._engine.connect() as conn:
        assert await conn.scalar(select(tasks.c.status)) == "IN_PROGRESS"


async def test_daemon_lane_reuses_source_checks_and_isolates_step_proofs(promotion):
    from urllib.parse import parse_qs, urlparse

    from src.integration.train import TrainTarget
    from src.integration.train_sources import DaemonLanes, DatabaseBatches

    e = promotion

    class GitHub(HostedGitHub):
        full_name = "test/repo"

        async def paged_items(self, path, *, key):
            name = parse_qs(urlparse(path).query).get("check_name", [None])[0]
            if key == "check_runs" and name != "unit":
                return [r for r in self.records if r["name"] == name]
            return await super().paged_items(path, key=key)

    client = GitHub()
    client.runs[e.source] = "success"
    # Neither the default proof nor an earlier step's proof is this step's proof.
    client.records = [
        {"id": n, "app": {"id": 101}, "head_sha": e.source, "name": name}
        for n, name in enumerate((ATTESTATION_CHECK_NAME,
                                 "Agent Queue Promotion Attestation (staging)"), 1)
    ]
    manifest = IntegrationTrustManifest(
        schema="aq.integration-trust.v1", canonical_repository_id="r", repository_id=123,
        full_name=client.full_name, ci_producer_app_id=15368, attestation_app_id=101,
        attestation_name=ATTESTATION_CHECK_NAME,
        promotion_attestation_names=(e.meta["step"]["gate"]["attestation"],),
        required_checks={"version": "checks-v1", "names": ["unit"]},
    )

    async def load_trust(state):
        assert state["candidate_sha"] == e.source and state["revision"] == 0
        return manifest, client

    orchestrator = SimpleNamespace(
        db=e.db, git=e.ops.git,
        integration_attestation_service=SimpleNamespace(
            _load_trust=load_trust, _subject_manifest=AsyncMock(return_value=manifest),
        ),
    )
    lanes = DaemonLanes(orchestrator, batches=DatabaseBatches(e.db))
    lanes.publish = AsyncMock(side_effect=e.service.publish)
    lanes.publish.qualified = e.service.publish_tag
    target = TrainTarget("p", "r", e.batch.target_ref, "promotion", step=e.meta["step"])
    lane = await lanes._promotion_lane(target, e.repo, e.repo.binding, e.ops, {
        "root": {"required_checks": {
            "version": "checks-v1", "names": ["unit"], "producer_id": "15368",
        }},
    }, e.snapshot)
    checks = await lane.checks.for_candidate(e.batch, e.source)
    assert (await checks.refresh(await lane.checks.head(e.batch, e.source))).green
    e.service = lane.service
    assert (await visit(e)).state == "delivered"
    assert not git(e.ops.git.remote_path, "for-each-ref", "refs/heads/aq/promote/")
    [record] = [r for r in client.records if r["id"] > 2]
    assert record["name"] == e.meta["step"]["gate"]["attestation"]
    proof = json.loads(record["output"]["text"])
    assert proof["step"] == "release" and proof["source_sha"] == e.source


async def test_publish_command_refuses_worker_then_settles_via_service(promotion):
    from src.commands.integration_commands import IntegrationCommandsMixin
    from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
    from src.database.tables import integration_batches
    from src.integration.train import TrainLane
    from src.integration.train_sources import DatabaseBatches
    from src.profiles.capabilities import DENY_ALL

    e = promotion
    handler = IntegrationCommandsMixin()
    handler.db = e.db
    lane = TrainLane(snapshot=e.snapshot, service=e.service, checks=e.checks)
    handler.orchestrator = SimpleNamespace(integration_train=SimpleNamespace(
        lane_for=AsyncMock(return_value=lane), batches=DatabaseBatches(e.db),
    ))
    worker = ExecutionPrincipal(kind=PrincipalKind.SESSION, policy=DENY_ALL,
                                session_id="worker", project_id="p")
    with principal_context(worker):
        result = await handler._cmd_integration_promotion_publish({"batch_id": e.batch.id})
    assert not result["success"] and result["outcome"] == "unauthorized"
    assert git(e.ops.git.remote_path, "rev-parse", "main") == e.base
    with principal_context(ExecutionPrincipal.service("integration-train")):
        result = await handler._cmd_integration_promotion_publish({"batch_id": e.batch.id})
    assert result["success"] and result["outcome"] == "delivered"
    async with e.db._engine.connect() as conn:
        assert await conn.scalar(select(tasks.c.status)) == "COMPLETED"
        assert await conn.scalar(select(integration_batches.c.lifecycle)) == "promoted"
        assert json.loads(await conn.scalar(select(task_context.c.content)))["tag_oid"]
