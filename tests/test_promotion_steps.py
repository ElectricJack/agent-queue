"""Real Git/PostgreSQL promotion crash matrix (dev-branch releases §3.3)."""

import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert, select, update

from src.database.tables import (
    agent_profiles, integration_batches, integration_review_evidence, projects, repos,
    task_context, task_metadata, tasks,
)
from src.git.manager import GitError
from src.git.github_contracts import GitHubAccessError
from src.integration.batches import Batch, BatchMember, BatchStore
from src.integration.git_truth import GitTruth
from src.integration.ci import ATTESTATION_CHECK_NAME, IntegrationTrustManifest
from src.integration.provenance import CompletionIdentity
from src.integration.promotion_steps import (
    PROMOTION_PUBLISH, FlowSchema, PromotionChecks, PromotionIntentInvalid, PromotionVisit,
    StepAdmission,
    StepPullRequestGate,
    frozen_required_checks, promotion_ref, settle_promotion,
)
from tests.test_integration_gitops import commit, git, setup as setup
from tests.test_integration_train_sources import HostedGitHub


class PromotionGitHub(HostedGitHub):
    """GitHub PR and review responses; check facts use the real HostedChecks path."""

    full_name = "test/repo"

    def __init__(self, source, ref, target):
        super().__init__()
        repo = {"id": 123, "full_name": self.full_name}
        self.pull = {
            "number": 42, "html_url": f"https://github.com/{self.full_name}/pull/42",
            "state": "open", "draft": False,
            "head": {"ref": ref.removeprefix("refs/heads/"), "sha": source, "repo": repo},
            "base": {"ref": target.removeprefix("refs/heads/"), "repo": repo},
        }
        self.reviews = [self.review(1, source)]
        self.admins = {"operator"}

    @staticmethod
    def review(number, sha, *, login="operator", state="APPROVED", kind="User"):
        return {"id": number, "commit_id": sha, "state": state,
                "user": {"login": login, "type": kind}}

    async def request_json(self, method, path, **kwargs):
        if method == "GET" and path.endswith("/pulls/42"):
            return copy.deepcopy(self.pull)
        if method == "GET" and "/collaborators/" in path:
            login = path.split("/collaborators/", 1)[1].split("/", 1)[0]
            return {"permission": "admin" if login in self.admins else "write",
                    "user": {"login": login, "type": "User"}}
        return await super().request_json(method, path, **kwargs)

    async def paged_list(self, path):
        assert path.endswith("/pulls/42/reviews?per_page=100")
        return copy.deepcopy(self.reviews)

    async def paged_items(self, path, *, key):
        from urllib.parse import parse_qs, urlparse

        name = parse_qs(urlparse(path).query).get("check_name", [None])[0]
        if key == "check_runs" and name and name != "unit":
            return [r for r in self.records if r["name"] == name]
        return await super().paged_items(path, key=key)


@pytest.fixture
async def promotion(setup, request):
    db, ops, _, _, repo, base, source, _ = setup
    # Only replace GitHub credential selection: all object/ref operations use real Git.
    destination = ops.git._apush_destination

    async def local_destination(path, remote, **kwargs):
        return await destination(path, remote, repository_url=str(ops.git.remote_path))

    ops.git._apush_destination = local_destination
    qualified_push = ops.git.apush_qualified_ref

    async def local_qualified_push(path, *, repository=None, **kwargs):
        if repository is not None:
            assert repository == repo.binding
        return await qualified_push(path, **kwargs)

    ops.git.apush_qualified_ref = local_qualified_push
    step = FlowSchema.validate([{
        "id": "release", "source": "dev", "target": "main",
        "versioning": {"kind": "semver_tag", "source": "pyproject"},
    }], default_branch="dev").flow[0]
    config = copy.deepcopy(getattr(request, "param", {}))
    intent = config.pop("intent", {})
    step["gate"].update(config.pop("gate", {}))
    step.update(config)
    batch = Batch("promotion-batch", "p", "r", "refs/heads/main", created_at=1700000000)
    member = BatchMember("promotion-task", source, base)
    meta = {
        "request_id": "promotion:r:release:1.2.3", "repository_id": "r",
        "target_ref": batch.target_ref, "source_sha": source, "base_sha": base,
        "step": step, "version": None if step["versioning"]["kind"] == "none" else "1.2.3",
        "checks_version": "checks-v1", "check_names": ["unit"],
        "requested_at": batch.created_at,
        "requester": {"identity": "human:local-operator", "github_login": "operator"},
        "notes_sha256": None,
        "pr_number": 42, "pr_url": "https://github.com/test/repo/pull/42",
    }
    meta.update(intent)
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
    exact = SimpleNamespace(read=AsyncMock(side_effect=lambda _: SimpleNamespace(green=gate.green)),
                            refresh=AsyncMock())
    client = PromotionGitHub(source, promotion_ref(step, meta), batch.target_ref)
    pr_gate = StepPullRequestGate(client, repo.binding)
    checks = PromotionChecks(admission, AsyncMock(return_value=exact),
                             pull_request=lambda _: pr_gate)
    # D3's request mechanism owns this create-only source ref and opening its PR.
    git(repo.store, "push", "origin", f"{source}:{promotion_ref(step, meta)}")
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

    async def delete_ref(repo, ref, *, expected_old_oid, authorize):
        assert await authorize()
        await ops.git.adelete_repository_ref(str(repo.store), repository=repo.binding,
                                            branch=ref.removeprefix("refs/heads/"),
                                            expected_old_oid=expected_old_oid)

    service = PromotionVisit(
        store, ops, admission=admission, checks=checks, publish=publish,
        publish_tag=publish_tag, delete_ref=delete_ref,
        attest=AsyncMock(return_value="published"), snapshot=snapshot,
    )
    return SimpleNamespace(
        db=db, ops=ops, repo=repo, base=base, source=source, batch=batch, member=member,
        meta=meta, store=store, admission=admission, checks=checks, gate=gate,
        snapshot=snapshot, service=service, github=client,
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
        assert meta == e.meta
        recorded = json.loads(await conn.scalar(select(task_metadata.c.value).where(
            task_metadata.c.key == "promotion_result")))
        assert recorded["tag_oid"] == result.detail["tag_oid"]
    # Restart after both writes, before/after settlement needs no further publication.
    pushes = e.ops.git.pushes
    assert (await visit(e)).state == "delivered"
    assert e.ops.git.pushes == pushes


async def test_second_settlement_with_another_tag_is_refused_and_first_kept(promotion):
    e = promotion
    result = await visit(e)
    assert result.state == "delivered", result
    await settle_promotion(e.db, e.batch, result)
    await settle_promotion(e.db, e.batch, result)  # the same settlement again is a no-op

    async def recorded():
        async with e.db._engine.connect() as conn:
            return (await conn.execute(select(task_metadata.c.value).where(
                task_metadata.c.key == "promotion_result"))).scalars().all()

    [first] = await recorded()
    forged = SimpleNamespace(state="delivered", candidate_sha=result.candidate_sha,
                             detail={**result.detail, "tag_oid": "f" * 40})
    with pytest.raises(PromotionIntentInvalid, match="differs from its recorded result"):
        await settle_promotion(e.db, e.batch, forged)
    assert await recorded() == [first]
    assert json.loads(first)["tag_oid"] == result.detail["tag_oid"] != "f" * 40


async def test_pinned_pr_waits_for_source_checks_then_resumes(promotion):
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


@pytest.mark.parametrize("field", ["head_sha", "head_ref", "head_repository", "base_ref",
                                  "base_repository", "url", "draft", "closed"])
async def test_pr_identity_and_state_hold_a_green_promotion(promotion, field):
    e = promotion
    pull = e.github.pull
    if field == "head_sha":
        pull["head"]["sha"] = e.base
    elif field == "head_ref":
        pull["head"]["ref"] = "dev"
    elif field == "head_repository":
        pull["head"]["repo"] = {"id": 999, "full_name": "fork/repo"}
    elif field == "base_ref":
        pull["base"]["ref"] = "elsewhere"
    elif field == "base_repository":
        pull["base"]["repo"] = {"id": 999, "full_name": "fork/repo"}
    elif field == "url":
        pull["html_url"] = "https://github.com/fork/repo/pull/42"
    elif field == "draft":
        pull["draft"] = True
    else:
        pull["state"] = "closed"
    result = await visit(e)
    assert result.state == "held"
    expected = {"draft": "promotion_pr_draft", "closed": "promotion_pr_closed"}
    assert result.detail["reason"] == expected.get(field, "promotion_pr_identity_mismatch")
    assert git(e.ops.git.remote_path, "rev-parse", "main") == e.base
    assert not git(e.ops.git.remote_path, "for-each-ref", "refs/tags/")
    e.service.attest.assert_not_awaited()


@pytest.mark.parametrize("review", ["missing", "other_head", "bot", "writer", "dismissed",
                                   "changes_requested", "newer_other_head"])
async def test_operator_gate_requires_current_undismissed_human_review(promotion, review):
    e = promotion
    make = e.github.review
    if review == "missing":
        e.github.reviews = []
    elif review == "other_head":
        e.github.reviews = [make(1, e.base)]
    elif review == "bot":
        e.github.reviews = [make(1, e.source, kind="Bot")]
    elif review == "writer":
        e.github.reviews = [make(1, e.source, login="writer")]
    else:
        e.github.reviews.append(make(
            2, e.base if review == "newer_other_head" else e.source,
            state={"dismissed": "DISMISSED", "changes_requested": "CHANGES_REQUESTED"}
                  .get(review, "APPROVED"),
        ))
    result = await visit(e)
    assert result.state == "held"
    assert result.detail["reason"] == ("promotion_pr_changes_requested"
        if review == "changes_requested" else "promotion_operator_approval_missing")
    assert git(e.ops.git.remote_path, "rev-parse", "main") == e.base


@pytest.mark.parametrize("promotion", [{"gate": {"operator_logins": ["Second-Admin"]}}],
                         indirect=True)
async def test_operator_allowlist_narrows_admin_approval_without_granting_permission(promotion):
    e = promotion
    assert (await visit(e)).detail["reason"] == "promotion_operator_approval_missing"
    e.github.reviews = [e.github.review(2, e.source, login="second-admin")]
    assert (await visit(e)).detail["reason"] == "promotion_operator_approval_missing"
    assert git(e.ops.git.remote_path, "rev-parse", "main") == e.base
    e.github.admins.add("second-admin")
    assert (await visit(e)).state == "delivered"


@pytest.mark.parametrize("promotion", [{"gate": {"operator_logins": []}}], indirect=True)
async def test_empty_operator_allowlist_denies_all_admins(promotion):
    e = promotion
    assert (await visit(e)).detail["reason"] == "promotion_operator_approval_missing"
    e.service.attest.assert_not_awaited()


@pytest.mark.parametrize("failure", ["hidden", "forbidden", "network", "malformed", "no_login"])
async def test_operator_permission_lookup_failure_is_a_named_blocker(promotion, failure):
    e = promotion
    request = e.github.request_json

    async def unavailable(method, path, **kwargs):
        if "/collaborators/" in path:
            if failure in {"hidden", "forbidden"}:
                raise GitHubAccessError(
                    "not_found_or_hidden" if failure == "hidden" else "forbidden", "denied",
                )
            if failure == "network":
                raise OSError("permission lookup unavailable")
            return None if failure == "malformed" else {"permission": "admin", "user": {}}
        return await request(method, path, **kwargs)

    e.github.request_json = unavailable
    result = await visit(e)
    assert result.state == "held"
    assert result.detail["reason"] == "promotion_operator_permission_unavailable"
    assert git(e.ops.git.remote_path, "rev-parse", "main") == e.base
    assert not git(e.ops.git.remote_path, "for-each-ref", "refs/tags/")
    e.service.attest.assert_not_awaited()


def fail_permission_lookup(e, *logins):
    request = e.github.request_json

    async def unavailable(method, path, **kwargs):
        if any(f"/collaborators/{login}/" in path for login in logins):
            raise OSError("permission lookup unavailable")
        return await request(method, path, **kwargs)

    e.github.request_json = unavailable


async def test_changes_requested_by_a_non_operator_does_not_block(promotion):
    e = promotion
    e.github.reviews.append(e.github.review(2, e.source, login="outsider",
                                            state="CHANGES_REQUESTED"))
    assert (await visit(e)).state == "delivered"


@pytest.mark.parametrize("promotion", [{"gate": {"operator_logins": ["operator"]}}],
                         indirect=True)
async def test_changes_requested_by_an_admin_outside_the_allowlist_does_not_block(promotion):
    e = promotion
    e.github.admins.add("other-admin")
    e.github.reviews.append(e.github.review(2, e.source, login="other-admin",
                                            state="CHANGES_REQUESTED"))
    assert (await visit(e)).state == "delivered"


async def test_unverifiable_changes_request_fails_closed(promotion):
    e = promotion
    e.github.reviews.append(e.github.review(2, e.source, login="unknown",
                                            state="CHANGES_REQUESTED"))
    fail_permission_lookup(e, "unknown")
    result = await visit(e)
    assert result.detail["reason"] == "promotion_operator_permission_unavailable"
    e.service.attest.assert_not_awaited()


async def test_failed_lookup_for_one_approver_does_not_hide_an_operator_approval(promotion):
    e = promotion
    e.github.reviews = [e.github.review(1, e.source, login="flaky"),
                        e.github.review(2, e.source)]
    fail_permission_lookup(e, "flaky")
    assert (await visit(e)).state == "delivered"


@pytest.mark.parametrize("promotion", [{"gate": {"approval": "requester"}, "intent": {
    "requester": {"identity": "session:requester", "github_login": "requester"},
}}], indirect=True)
async def test_requester_gate_blocks_only_on_the_requesters_own_changes_request(promotion):
    e = promotion
    make = e.github.review
    e.github.reviews = [make(1, e.source, login="requester"),
                        make(2, e.source, state="CHANGES_REQUESTED")]
    assert (await visit(e)).state == "delivered"


@pytest.mark.parametrize("promotion", [{"gate": {"approval": "requester"}, "intent": {
    "requester": {"identity": "session:requester", "github_login": "requester"},
}}], indirect=True)
async def test_requester_changes_request_blocks_the_requester_gate(promotion):
    e = promotion
    e.github.reviews = [e.github.review(3, e.source, login="Requester",
                                        state="CHANGES_REQUESTED")]
    assert (await visit(e)).detail["reason"] == "promotion_pr_changes_requested"


async def test_operator_permission_is_observed_again_before_publication(promotion):
    e = promotion

    async def revoke_permission(*args):
        e.github.admins.clear()
        return "published"

    e.service.attest = revoke_permission
    result = await visit(e)
    assert result.state == "held"
    assert result.detail["reason"] == "promotion_operator_approval_missing"
    assert git(e.ops.git.remote_path, "rev-parse", "main") == e.base
    assert not git(e.ops.git.remote_path, "for-each-ref", "refs/tags/")


@pytest.mark.parametrize("endpoint", ["pr", "reviews", "permission"])
async def test_pr_gate_rate_limits_reach_the_shared_train_backoff(promotion, endpoint):
    from src.integration.train import IntegrationTrain, TrainLane, TrainTarget
    from src.integration.train_sources import DatabaseBatches

    e = promotion
    request = e.github.request_json
    limited = GitHubAccessError("rate_limited", "GitHub rate limited", retry_at=1200.0)
    permission_calls = []

    async def rate_limited(method, path, **kwargs):
        permission_calls.append(path)
        if ((endpoint == "pr" and path.endswith("/pulls/42"))
                or (endpoint == "permission" and "/collaborators/" in path)):
            raise limited
        return await request(method, path, **kwargs)

    e.github.request_json = rate_limited
    if endpoint == "reviews":
        e.github.paged_list = AsyncMock(side_effect=limited)
    target = TrainTarget("p", "r", e.batch.target_ref, "promotion", e.meta["step"])
    train = IntegrationTrain(
        targets=SimpleNamespace(targets=AsyncMock(return_value=[target])),
        batches=DatabaseBatches(e.db),
        lane_for=AsyncMock(return_value=TrainLane(
            snapshot=e.snapshot, service=e.service, checks=e.checks,
        )),
        repair=SimpleNamespace(), clock=lambda: 1000.0,
    )
    assert (await train.tick())["started"]
    await train.drain()
    row, = train.status()
    assert row["detail"]["reason"] == "rate_limited"
    assert row["detail"]["retry_at"] == 1200.0
    calls = len(permission_calls)
    assert (await train.tick(1001.0))["deferred"]
    await train.drain()
    assert len(permission_calls) == calls
    assert git(e.ops.git.remote_path, "rev-parse", "main") == e.base
    assert not git(e.ops.git.remote_path, "for-each-ref", "refs/tags/")
    e.service.attest.assert_not_awaited()


@pytest.mark.parametrize("promotion", [{"gate": {"approval": "requester"}, "intent": {
    "requester": {"identity": "session:requester", "github_login": "requester"},
}}], indirect=True)
async def test_requester_gate_does_not_accept_another_operators_approval(promotion):
    e = promotion
    assert (await visit(e)).detail["reason"] == "promotion_requester_approval_missing"
    e.github.reviews = [e.github.review(2, e.source, login="requester")]
    assert (await visit(e)).state == "delivered"


@pytest.mark.parametrize("promotion", [{"gate": {"approval": "requester"}, "intent": {
    "requester": {"identity": "session:requester"},
}}], indirect=True)
async def test_requester_without_bound_github_identity_is_held(promotion):
    assert (await visit(promotion)).detail["reason"] == "promotion_requester_identity_missing"


@pytest.mark.parametrize("promotion", [{"type": "continuous", "gate": {"approval": "none"}}],
                         indirect=True)
async def test_none_approval_needs_green_pr_without_a_human_review(promotion):
    e = promotion
    e.github.paged_list = AsyncMock(side_effect=AssertionError("no human review required"))
    e.gate.green = False
    assert (await visit(e)).state == "testing"
    e.gate.green = True
    assert (await visit(e)).state == "delivered"


@pytest.mark.parametrize("promotion", [{"intent": {"pr_number": None, "pr_url": None}}],
                         indirect=True)
async def test_green_source_without_a_request_pr_is_held(promotion):
    result = await visit(promotion)
    assert result.state == "held" and result.detail["reason"] == "promotion_pr_missing"


async def test_gate_refresh_writes_review_evidence_only_on_change(promotion):
    e = promotion

    async def rows():
        async with e.db._engine.connect() as conn:
            return (await conn.execute(select(integration_review_evidence).where(
                integration_review_evidence.c.source_task_id == e.member.task_id,
                integration_review_evidence.c.review_kind == "promotion_pr",
            ).order_by(integration_review_evidence.c.created_at))).mappings().all()

    e.github.reviews = []
    for _ in range(3):
        await e.checks.refresh_gate(e.batch, e.source)
    [first] = await rows()
    assert first["verdict"] == "rejected"
    assert first["evidence"]["reason"] == "promotion_operator_approval_missing"
    e.github.reviews = [e.github.review(1, e.source)]
    await e.checks.refresh_gate(e.batch, e.source)
    await e.checks.refresh_gate(e.batch, e.source)
    # Append-only: the change adds one row and the earlier observation stays.
    [kept, second] = await rows()
    assert kept["id"] == first["id"] and second["verdict"] == "approved"
    assert second["evidence"]["reason"] is None


async def test_review_change_during_attestation_withdraws_publication(promotion):
    e = promotion

    async def attest(*args):
        e.github.reviews.append(e.github.review(2, e.source, state="CHANGES_REQUESTED"))
        return "published"

    e.service.attest = attest
    result = await visit(e)
    assert result.state == "held" and result.detail["reason"] == "promotion_pr_changes_requested"
    assert git(e.ops.git.remote_path, "rev-parse", "main") == e.base
    assert not git(e.ops.git.remote_path, "for-each-ref", "refs/tags/")


async def test_cleanup_failure_does_not_gate_delivery_and_replay_retries(promotion):
    e = promotion
    original = e.service.delete_ref
    e.service.delete_ref = AsyncMock(side_effect=GitError("cleanup outage"))
    assert (await visit(e)).state == "delivered"
    assert git(e.ops.git.remote_path, "rev-parse", promotion_ref(e.meta["step"], e.meta)) == e.source
    async with e.db._engine.connect() as conn:
        assert await conn.scalar(select(integration_batches.c.cleanup_state)) == "pending"
    e.service.delete_ref = original
    assert (await visit(e)).state == "delivered"
    assert not git(e.ops.git.remote_path, "for-each-ref", "refs/heads/aq/promote/")
    async with e.db._engine.connect() as conn:
        assert await conn.scalar(select(integration_batches.c.cleanup_state)) == "complete"


async def test_settled_cleanup_is_retried_by_later_lane_visits(promotion):
    from src.integration.train import TrainTarget
    from src.integration.train_sources import DatabaseBatches

    e = promotion
    original = e.service.delete_ref
    e.service.delete_ref = AsyncMock(side_effect=GitError("cleanup outage"))
    result = await visit(e)
    assert result.state == "delivered"
    await DatabaseBatches(e.db).settle(e.batch, result)
    e.service.delete_ref = original
    e.service.attest.reset_mock()
    e.github.pull["state"] = "closed"
    target = TrainTarget("p", "r", e.batch.target_ref, "promotion", step=e.meta["step"])
    await e.service.reconcile_cleanup(target)
    assert not git(e.ops.git.remote_path, "for-each-ref", "refs/heads/aq/promote/")
    e.service.attest.assert_not_awaited()
    async with e.db._engine.connect() as conn:
        assert await conn.scalar(select(integration_batches.c.cleanup_state)) == "complete"
        assert await conn.scalar(select(tasks.c.status)) == "COMPLETED"


async def test_cleanup_preserves_a_changed_request_ref(promotion):
    e = promotion
    ref = promotion_ref(e.meta["step"], e.meta)
    git(e.ops.git.remote_path, "update-ref", ref, e.base)
    assert (await visit(e)).state == "delivered"
    assert git(e.ops.git.remote_path, "rev-parse", ref) == e.base
    async with e.db._engine.connect() as conn:
        assert await conn.scalar(select(integration_batches.c.cleanup_state)) == "pending"


async def test_frozen_requester_notes_and_extra_intent_fields_cannot_change(promotion):
    e = promotion
    for changed in ({"requester": {"identity": "someone else"}},
                    {"notes_sha256": "a" * 64}, {"approved_by": "operator"}):
        original = copy.deepcopy(e.meta)
        await update_meta(e, **changed)
        with pytest.raises(PromotionIntentInvalid):
            await e.admission.load(e.batch, (e.member,))
        await update_meta(e, **original)
        e.meta = original
        # Remove any extra field the attempted change added.
        async with e.db._engine.begin() as conn:
            await conn.execute(update(task_context).values(content=json.dumps(original)))


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


async def test_missing_tag_recovers_after_target_advances_and_pr_closes(promotion):
    e = promotion
    original = e.service.publish_tag
    e.service.publish_tag = AsyncMock(side_effect=GitError("lost connection before tag write"))
    assert (await visit(e)).state == "unknown"
    assert git(e.ops.git.remote_path, "rev-parse", "main") == e.source
    tree = git(e.repo.store, "rev-parse", f"{e.source}^{{tree}}")
    advanced = git(e.repo.store, "commit-tree", tree, "-p", e.source, "-m", "later target work")
    git(e.repo.store, "push", "origin", f"{advanced}:refs/heads/main")
    e.github.pull["state"] = "closed"
    e.github.reviews = []
    e.service.publish_tag = original
    e.service.attest = AsyncMock(side_effect=AssertionError("must not repeat attestation"))
    assert (await visit(e)).state == "delivered"
    assert git(e.ops.git.remote_path, "rev-parse", "main") == advanced
    assert git(e.ops.git.remote_path, "rev-parse", "v1.2.3^{}") == e.source


@pytest.mark.parametrize("pr_state", ["open", "closed"])
async def test_source_reaching_target_another_way_never_earns_a_tag(promotion, pr_state):
    e = promotion
    tree = git(e.repo.store, "rev-parse", f"{e.source}^{{tree}}")
    advanced = git(e.repo.store, "commit-tree", tree, "-p", e.source, "-m", "manual push")
    git(e.repo.store, "push", "origin", f"{advanced}:refs/heads/main")
    e.github.pull["state"] = pr_state
    result = await visit(e)
    assert result.state == "held" and result.detail["reason"] == "promotion_recovery_unproven"
    assert not git(e.ops.git.remote_path, "for-each-ref", "refs/tags/")
    e.service.attest.assert_not_awaited()


async def test_publish_record_precedes_the_target_write_and_names_the_request(promotion):
    e = promotion
    seen = []
    original = e.service.publish

    async def publish(*args, **kwargs):
        async with e.db._engine.connect() as conn:
            seen.append(await conn.scalar(select(task_metadata.c.value).where(
                task_metadata.c.task_id == e.member.task_id,
                task_metadata.c.key == PROMOTION_PUBLISH,
            )))
        await original(*args, **kwargs)

    e.service.publish = publish
    assert (await visit(e)).state == "delivered"
    record = json.loads(seen[0])
    assert record == {"request_id": e.meta["request_id"], "batch_id": e.batch.id,
                      "source_sha": e.source, "target_ref": e.batch.target_ref,
                      "expected_old_oid": e.base}


async def test_publish_record_for_another_request_does_not_prove_recovery(promotion):
    e = promotion
    async with e.db._engine.begin() as conn:
        await conn.execute(insert(task_metadata).values(
            task_id=e.member.task_id, key=PROMOTION_PUBLISH, value=json.dumps({
                "request_id": "another-request", "batch_id": e.batch.id,
                "source_sha": e.source, "target_ref": e.batch.target_ref,
            }),
        ))
    git(e.repo.store, "push", "origin", f"{e.source}:refs/heads/main")
    result = await visit(e)
    assert result.state == "held" and result.detail["reason"] == "promotion_recovery_unproven"
    assert not git(e.ops.git.remote_path, "for-each-ref", "refs/tags/")


async def test_equivalent_patch_does_not_substitute_for_the_pinned_source(promotion):
    e = promotion
    tree = git(e.repo.store, "rev-parse", f"{e.source}^{{tree}}")
    equivalent = git(e.repo.store, "commit-tree", tree, "-p", e.base, "-m", "equivalent source")
    git(e.repo.store, "push", "origin", f"{equivalent}:refs/heads/main")
    snapshot = await e.snapshot()
    assert await snapshot.contains_source(e.member.task_id, e.source, e.base) is True
    result = await visit(e)
    assert result.state == "held" and result.detail["reason"] == "promotion_not_fast_forward"
    assert git(e.ops.git.remote_path, "rev-parse", "main") == equivalent
    assert not git(e.ops.git.remote_path, "for-each-ref", "refs/tags/")


async def test_managed_publication_leases_cover_branch_tag_and_cleanup(promotion):
    from src.integration.train_sources import LeasedPublish

    e = promotion
    publisher = LeasedPublish(e.db, e.ops.git)
    e.service.publish = publisher
    e.service.publish_tag = publisher.qualified
    e.service.delete_ref = publisher.delete
    assert (await visit(e)).state == "delivered"
    assert git(e.ops.git.remote_path, "rev-parse", "main") == e.source
    assert git(e.ops.git.remote_path, "rev-parse", "v1.2.3^{}") == e.source
    assert not git(e.ops.git.remote_path, "for-each-ref", "refs/heads/aq/promote/")


async def test_busy_managed_target_defers_branch_and_tag(promotion):
    from src.integration.models import BranchKey
    from src.integration.train_sources import LeasedPublish

    e = promotion
    publisher = LeasedPublish(e.db, e.ops.git)
    fence = await publisher.locks.acquire(BranchKey(repository_id="r", branch=e.batch.target_ref),
                                          "another-publisher", role="integration")
    e.service.publish = publisher
    e.service.publish_tag = publisher.qualified
    try:
        assert (await visit(e)).state != "delivered"
        assert git(e.ops.git.remote_path, "rev-parse", "main") == e.base
        assert not git(e.ops.git.remote_path, "for-each-ref", "refs/tags/")
    finally:
        await publisher.locks.release(fence)
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
        git(e.ops.git.remote_path, "update-ref", "-d", identity.ref)
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


@pytest.mark.parametrize("current_names", [["unit"], ["unit", "lint"]])
async def test_daemon_lane_reuses_source_checks_and_isolates_step_proofs(
    promotion, current_names,
):
    from src.integration.train import TrainTarget
    from src.integration.train_sources import DaemonLanes, DatabaseBatches

    e = promotion

    client = e.github
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
        # Trust that gained a check after the request still runs the frozen set.
        required_checks={"version": "checks-v1", "names": current_names},
    )

    async def load_trust(state):
        assert state["candidate_sha"] == e.source and state["revision"] == 0
        return manifest, client

    orchestrator = SimpleNamespace(
        db=e.db, git=e.ops.git,
        integration_attestation_service=SimpleNamespace(
            # The check set is frozen in the request; S's own tree never selects it.
            _load_trust=load_trust,
            _subject_manifest=AsyncMock(side_effect=AssertionError("lane read S's manifest")),
        ),
    )
    lanes = DaemonLanes(orchestrator, batches=DatabaseBatches(e.db))
    lanes.publish = AsyncMock(side_effect=e.service.publish)
    lanes.publish.qualified = e.service.publish_tag
    lanes.publish.delete = e.service.delete_ref
    target = TrainTarget("p", "r", e.batch.target_ref, "promotion", step=e.meta["step"])
    lane = await lanes._promotion_lane(target, e.repo, e.repo.binding, e.ops, {
        "root": {"required_checks": {
            "version": "checks-v1", "names": ["unit"], "producer_id": "15368",
        }},
    }, e.snapshot)
    checks = await lane.checks.for_candidate(e.batch, e.source)
    assert (await checks.refresh(await lane.checks.head(e.batch, e.source))).green
    e.service = lane.service
    observed = await visit(e)
    assert observed.state == "delivered", observed
    assert not git(e.ops.git.remote_path, "for-each-ref", "refs/heads/aq/promote/")
    [record] = [r for r in client.records if r["id"] > 2]
    assert record["name"] == e.meta["step"]["gate"]["attestation"]
    proof = json.loads(record["output"]["text"])
    assert proof["step"] == "release" and proof["source_sha"] == e.source


async def test_daemon_lane_reuses_subject_trust_until_its_key_or_ttl_changes():
    """grand-lantern-78.4: a held promotion does not refetch S's manifest every visit."""
    from src.integration.train_sources import PROMOTION_RESOLUTION_TTL_SECONDS, DaemonLanes

    clock, loads = [100.0], []

    async def load_trust(state):
        loads.append(state)
        return f"trust-{len(loads)}", "client"

    attestation = SimpleNamespace(_load_trust=load_trust)
    lanes = DaemonLanes(SimpleNamespace(db=None, git=None), batches=None,
                        clock=lambda: clock[0])
    batch = SimpleNamespace(id="b-1")
    state = {"candidate_sha": "a" * 40, "revision": 0, "policy_snapshot": {"root": {}}}
    assert await lanes._promotion_trust(attestation, batch, state) == ("trust-1", "client")
    for _ in range(2):
        # Each use keeps the entry; S is immutable, so only memory bounds it.
        clock[0] += PROMOTION_RESOLUTION_TTL_SECONDS - 1
        assert (await lanes._promotion_trust(attestation, batch, dict(state)))[0] == "trust-1"
    assert len(loads) == 1
    # A repair revision or a policy change reloads, and so does an idle lane.
    changed = {**state, "policy_snapshot": {"root": {"required_checks": {"names": ["lint"]}}}}
    assert (await lanes._promotion_trust(attestation, batch, {**state, "revision": 1}))[0] \
        == "trust-2"
    assert (await lanes._promotion_trust(attestation, batch, changed))[0] == "trust-3"
    clock[0] += PROMOTION_RESOLUTION_TTL_SECONDS
    assert (await lanes._promotion_trust(attestation, batch, changed))[0] == "trust-4"

    async def refuse(state):
        raise PromotionIntentInvalid("subject trust manifest names another identity")

    with pytest.raises(PromotionIntentInvalid):
        await lanes._promotion_trust(SimpleNamespace(_load_trust=refuse),
                                     SimpleNamespace(id="b-2"), state)
    assert set(lanes._promotion_trust_cache) == {"b-1"}


@pytest.mark.parametrize("frozen", [
    {"check_names": None}, {"check_names": "unit"}, {"check_names": []},
    {"checks_version": ""},
])
def test_frozen_check_set_refuses_request_without_a_valid_one(frozen):
    meta = {"checks_version": "checks-v1", "check_names": ["unit"]}
    assert frozen_required_checks(meta).names == ("unit",)
    with pytest.raises(PromotionIntentInvalid):
        frozen_required_checks({**meta, **frozen})


async def test_locally_gated_step_publishes_without_hosted_attestation(promotion):
    e = promotion
    # A step the local runner gates has no hosted proof to attest, yet its PR
    # identity and review gate still hold it.
    e.service.attest = None
    e.github.pull["draft"] = True
    held = await visit(e)
    assert held.state == "held" and held.detail["reason"] == "promotion_pr_draft"
    e.github.pull["draft"] = False
    assert (await visit(e)).state == "delivered"
    assert git(e.ops.git.remote_path, "rev-parse", "main") == e.source
    assert git(e.ops.git.remote_path, "rev-parse", "refs/tags/v1.2.3^{}") == e.source


def local_step_lanes(e, monkeypatch):
    from src.integration.train_sources import DaemonLanes, DatabaseBatches

    manifest = IntegrationTrustManifest(
        schema="aq.integration-trust.v1", canonical_repository_id="r", repository_id=123,
        full_name=e.github.full_name, ci_producer_app_id=15368, attestation_app_id=101,
        attestation_name=ATTESTATION_CHECK_NAME,
        promotion_attestation_names=(e.meta["step"]["gate"]["attestation"],),
        required_checks={"version": "checks-v1", "names": ["unit"]},
    )
    # No attestation service: a local step reads its manifest from the store.
    lanes = DaemonLanes(SimpleNamespace(db=e.db, git=e.ops.git),
                        batches=DatabaseBatches(e.db))
    monkeypatch.setattr(lanes, "_retained_manifest", AsyncMock(return_value=manifest))
    monkeypatch.setattr(e.ops.git, "_github_client", lambda binding: e.github, raising=False)
    return lanes


async def test_local_ci_step_gate_runs_the_manifest_selected_checks(promotion, monkeypatch):
    from src.integration.checks import LocalChecks, RequestedChecks
    from src.integration.models import IntegrationCIPolicy
    from src.integration.train import TrainTarget
    from src.integration.train_sources import RETAINED_CANDIDATE_PREFIX

    e = promotion
    lanes = local_step_lanes(e, monkeypatch)
    target = TrainTarget("p", "r", e.batch.target_ref, "promotion", step=e.meta["step"])
    ci = IntegrationCIPolicy(source="local", commands={"unit": "aq test tests/test_x.py"})
    lane = await lanes._promotion_lane(target, e.repo, e.repo.binding, e.ops, {}, e.snapshot,
                                       ci=ci)
    assert lane.service.attest is None
    checks = await lane.checks.for_candidate(e.batch, e.source)
    assert isinstance(checks, RequestedChecks) and isinstance(checks.provider, LocalChecks)
    assert checks.required.names == ("unit",) and checks.required.version == "checks-v1"
    assert checks.provider.producer.plan.commands == ("aq test tests/test_x.py",)
    assert git(e.repo.store, "rev-parse", RETAINED_CANDIDATE_PREFIX + e.batch.id) == e.source


async def test_local_ci_step_without_a_command_for_a_selected_check_is_refused(
    promotion, monkeypatch,
):
    from src.integration.models import IntegrationCIPolicy
    from src.integration.promotion_steps import PromotionIntentInvalid
    from src.integration.train import TrainTarget

    e = promotion
    lanes = local_step_lanes(e, monkeypatch)
    target = TrainTarget("p", "r", e.batch.target_ref, "promotion", step=e.meta["step"])
    ci = IntegrationCIPolicy(source="local", commands={"lint": "ruff check src"})
    lane = await lanes._promotion_lane(target, e.repo, e.repo.binding, e.ops, {}, e.snapshot,
                                       ci=ci)
    with pytest.raises(PromotionIntentInvalid, match="ci_command_missing:unit"):
        await lane.checks.for_candidate(e.batch, e.source)


@pytest.mark.parametrize("field,value", [
    ("canonical_repository_id", "elsewhere"), ("repository_id", 999),
    ("full_name", "elsewhere/repo"), ("ci_producer_app_id", 999),
    ("attestation_app_id", 999), ("attestation_name", "another check"),
    (None, None),
])
async def test_local_promotion_manifest_enforces_repository_and_app_identity(
    promotion, field, value,
):
    from src.integration.ci import SubjectTrustError, TRUST_MANIFEST_PATH
    from src.integration.train_sources import DaemonLanes, DatabaseBatches

    e = promotion
    manifest = IntegrationTrustManifest(
        schema="aq.integration-trust.v1", canonical_repository_id=e.repo.repository_id,
        repository_id=123, full_name=e.github.full_name, ci_producer_app_id=15368,
        attestation_app_id=101, attestation_name=ATTESTATION_CHECK_NAME,
        required_checks={"version": "checks-v1", "names": ["unit"]},
    ).model_dump(mode="json", by_alias=True)
    if field:
        manifest[field] = value
    sha = commit(e.repo.store, {TRUST_MANIFEST_PATH: json.dumps(manifest)}, base=e.source)
    e.ops.git._github_client = lambda binding: e.github
    lanes = DaemonLanes(SimpleNamespace(db=e.db, git=e.ops.git), batches=DatabaseBatches(e.db))
    policy = {"root": {"required_checks": {"producer_id": "15368"}}}
    if field:
        with pytest.raises(SubjectTrustError) as refused:
            await lanes._retained_manifest(e.repo, sha, binding=e.repo.binding, policy=policy)
        assert refused.value.cause == "identity_mismatch"
        assert field in refused.value.fields
    else:
        result = await lanes._retained_manifest(e.repo, sha, binding=e.repo.binding, policy=policy)
        assert result.repository_id == 123


async def test_local_promotion_manifest_has_the_hosted_size_limit(promotion):
    from src.integration.attestation import _MAX_TRUST_BYTES
    from src.integration.ci import SubjectTrustError, TRUST_MANIFEST_PATH
    from src.integration.train_sources import DaemonLanes, DatabaseBatches

    e = promotion
    sha = commit(e.repo.store, {TRUST_MANIFEST_PATH: " " * _MAX_TRUST_BYTES + "{}"}, base=e.source)
    lanes = DaemonLanes(SimpleNamespace(db=e.db, git=e.ops.git), batches=DatabaseBatches(e.db))
    with pytest.raises(SubjectTrustError) as refused:
        await lanes._retained_manifest(e.repo, sha, binding=e.repo.binding, policy={})
    assert refused.value.cause == "too_large"


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
        assert json.loads(await conn.scalar(select(task_context.c.content))) == e.meta
        assert json.loads(await conn.scalar(select(task_metadata.c.value).where(
            task_metadata.c.key == "promotion_result")))["tag_oid"]
