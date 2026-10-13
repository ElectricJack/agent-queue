"""Generated train/promotion/retirement histories over real Git and PostgreSQL.

The CI profile bounds expensive Git examples; --hypothesis-profile=deep explores
up to 100 histories per property locally. Each example resets refs, gate observations
and mutable database records, so Hypothesis shrinking never inherits a prior run.
"""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from hypothesis import HealthCheck, example, given, settings, strategies as st
from sqlalchemy import delete, update

from src.database.tables import branch_retirements, integration_batches, task_metadata
from src.integration.batches import Batch, BatchMember, BatchService, candidate_ref
from src.integration.checks import ChecksResult, ChecksState, CommitCheck, Conclusion, RequiredChecks
from src.integration.promotion_steps import PROMOTION_PUBLISH, PromotionVisit, promotion_ref
from src.integration.train import CandidateChecks, TrainLane
from src.models import TaskStatus
from tests.test_branch_retirement import (
    BRANCH,
    request as retirement_request,
    rows as retirement_rows,
)
from tests import test_branch_retirement
from tests.test_integration_batches import batch_env as batch_env
from tests.test_integration_gitops import commit, git, setup as setup
from tests.test_integration_train import Batches, Repair, ROOT, Targets, train
from tests.test_promotion_steps import promotion as promotion


retirement_setup = test_branch_retirement.setup
# These properties explicitly reset mutable fixture state before every example.
property_settings = settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
PAYLOADS = st.lists(st.integers(min_value=0, max_value=1000), min_size=1, max_size=3)
HISTORY = st.lists(
    st.sampled_from(["restart", "move_target", "repair", "lost_response"]), max_size=3,
)


@pytest.fixture
def batch_examples(batch_env):
    """A fresh immutable batch per example, sharing only the disposable substrate."""
    _, ops, repo, base, _, store, _, _, green, approved, snapshot, service = batch_env
    serial = 0

    async def fresh(payloads, *, conflict=False):
        nonlocal serial
        serial += 1
        git(ops.git.remote_path, "update-ref", ROOT.target_ref, base)
        ops.git.ambiguous = ops.git.fail_push = ops.git.unreadable = False
        green.clear()
        approved["value"] = True
        batch = Batch(f"property-{serial}", "p", "r", ROOT.target_ref, created_at=1700000000)
        members = tuple(
            BatchMember(f"task-{index}", commit(repo.store, {
                "shared.txt" if conflict else f"member-{index}.txt": f"{index}: {value}\n",
            }, base=base), base, index)
            for index, value in enumerate(payloads)
        )
        await store.freeze(batch, members, trees={
            m.task_id: git(repo.store, "rev-parse", f"{m.source_sha}^{{tree}}") for m in members
        })
        return SimpleNamespace(
            ops=ops, repo=repo, base=base, store=store, batch=batch, members=members,
            green=green, snapshot=lambda: snapshot(batch),
            service=BatchService(store, ops, publish=service.publish, eligible=service.eligible,
                                 gate=service.gate, attest=service.attest),
        )

    return fresh


def restart_batch(env):
    old = env.service
    env.service = BatchService(env.store, env.ops, publish=old.publish, eligible=old.eligible,
                               gate=old.gate, attest=old.attest)


async def visit(env):
    before = git(env.ops.git.remote_path, "rev-parse", ROOT.target_ref)
    observed = await env.service.visit(env.batch, env.members, await env.snapshot())
    after = git(env.ops.git.remote_path, "rev-parse", ROOT.target_ref)
    assert git(env.repo.store, "merge-base", "--is-ancestor", before, after) == ""
    assert await env.store.members(env.batch.id) == env.members
    if after != before:
        assert after in env.green  # the exact published head was green
    if observed.state == "delivered":
        for member in env.members:
            assert git(env.repo.store, "merge-base", "--is-ancestor", member.source_sha, after) == ""
    return observed


@property_settings
@given(payloads=PAYLOADS, history=HISTORY)
@example(payloads=[1, 2, 3], history=["repair", "move_target", "restart"])
async def test_delivered_members_remain_reachable_after_restarts_and_rebuilds(
    batch_examples, payloads, history,
):
    env = await batch_examples(payloads)
    observed = await visit(env)
    assert observed.state == "testing"
    for index, action in enumerate(history):
        if action == "restart":
            restart_batch(env)
        elif action == "move_target":
            # Green evidence for the old candidate must not bless the rebuilt SHA.
            env.green.add(observed.candidate_sha)
            target = commit(env.repo.store, {f"upstream-{index}.txt": "upstream\n"},
                            base=git(env.ops.git.remote_path, "rev-parse", ROOT.target_ref))
            git(env.repo.store, "push", "origin", f"{target}:{ROOT.target_ref}")
        elif action == "repair":
            repaired = commit(env.repo.store, {f"repair-{index}.txt": "repair\n"},
                              base=observed.candidate_sha)
            git(env.repo.store, "push", "origin", f"{repaired}:{candidate_ref(env.batch.id)}")
        else:
            env.ops.git.ambiguous = True
        observed = await visit(env)
    env.green.add(observed.candidate_sha)
    assert (await visit(env)).state == "delivered"
    env.ops.git.ambiguous = False
    restart_batch(env)
    pushes = env.ops.git.pushes
    assert (await visit(env)).state == "delivered"
    assert env.ops.git.pushes == pushes


class Verdicts:
    """External CI observations keyed by exact SHA, through the real lane gate."""

    def __init__(self):
        self.states = {}

    async def request(self, head):
        pass

    async def read(self, head):
        state = self.states.get(head.sha, ChecksState.PENDING)
        return ChecksResult(repository_id="r", sha=head.sha,
                            required=RequiredChecks(version="v1", names=("unit",), producer_id="1"),
                            state=state, checks=(CommitCheck(
                                repository_id="r", sha=head.sha, name="unit", producer_id="1",
                                required_check_version="v1", observed_at=1, classification="conclusive",
                                conclusion={ChecksState.RED: Conclusion.FAILURE,
                                            ChecksState.PENDING: Conclusion.PENDING,
                                            ChecksState.GREEN: Conclusion.SUCCESS}[state],
                            ),))

    refresh = read


@property_settings
@given(states=st.lists(st.sampled_from([ChecksState.RED, ChecksState.PENDING]),
                      min_size=1, max_size=4), withdraw=st.booleans())
@example(states=[ChecksState.RED, ChecksState.PENDING, ChecksState.RED], withdraw=True)
async def test_red_or_pending_batches_never_publish_even_after_gate_withdrawal(
    batch_examples, states, withdraw,
):
    env = await batch_examples([1, 2])
    checks = Verdicts()
    candidates = CandidateChecks.fixed(checks)
    env.service.gate = candidates.gate
    lane = TrainLane(snapshot=env.snapshot, service=env.service, checks=candidates)
    worker = train(Targets(ROOT), Batches({ROOT.key: (env.batch, env.members)}),
                   {ROOT.key: lane}, Repair())
    first = await worker.visit(ROOT)
    sha = first.candidate_sha
    for state in states:
        checks.states[sha] = state
        result = await worker.visit(ROOT)
        assert result.state in {"testing", "repair"}, result
        assert git(env.ops.git.remote_path, "rev-parse", ROOT.target_ref) == env.base
    checks.states[sha] = ChecksState.GREEN
    if withdraw:
        async def attest(*args):
            checks.states[sha] = ChecksState.RED
            return "published"

        env.service.attest = attest
        assert (await worker.visit(ROOT)).state != "delivered"
        assert git(env.ops.git.remote_path, "rev-parse", ROOT.target_ref) == env.base
    env.service.attest = AsyncMock(return_value="published")
    checks.states[sha] = ChecksState.GREEN
    assert (await worker.visit(ROOT)).state == "delivered"
    for member in env.members:
        assert git(env.repo.store, "merge-base", "--is-ancestor", member.source_sha, sha) == ""


@property_settings
@given(payloads=st.lists(st.integers(0, 1000), min_size=2, max_size=3),
       repairs=st.lists(st.booleans(), min_size=1, max_size=3))
@example(payloads=[1, 2, 3], repairs=[True, False, True])
async def test_partial_conflict_repairs_never_silently_drop_frozen_members(
    batch_examples, payloads, repairs,
):
    env = await batch_examples(payloads, conflict=True)
    observed = await visit(env)
    assert observed.state == "conflict"
    for index, restart in enumerate(repairs):
        # A plausible green worker checkpoint resolves content, but still omits
        # the conflicting source's ancestry. The train must keep owing that member.
        partial = commit(env.repo.store, {f"repair-{index}.txt": "fixed\n"},
                         base=observed.candidate_sha)
        git(env.repo.store, "push", "origin", f"{partial}:{candidate_ref(env.batch.id)}")
        env.green.add(partial)
        if restart:
            restart_batch(env)
        observed = await visit(env)
        assert observed.state == "conflict", observed
        assert git(env.ops.git.remote_path, "rev-parse", ROOT.target_ref) == env.base
    # A completed resolution includes every source parent and the chosen content.
    git(env.repo.store, "checkout", "--detach", observed.candidate_sha)
    for member in env.members:
        git(env.repo.store, "merge", "--no-ff", "-s", "ours", "-m", "resolve member",
            member.source_sha)
    resolved = commit(env.repo.store, {"shared.txt": "resolved\n"})
    git(env.repo.store, "push", "origin", f"{resolved}:{candidate_ref(env.batch.id)}")
    env.green.add(resolved)
    assert (await visit(env)).state == "delivered"
    assert git(env.repo.store, "show", f"{resolved}:shared.txt") == "resolved"


@pytest.mark.parametrize("promotion", [{"versioning": {"kind": "none"}}], indirect=True)
@property_settings
@given(start=st.sampled_from(["base", "source", "ahead", "diverged"]),
       history=st.lists(st.sampled_from(["pending", "revoke", "green", "advance", "restart",
                                        "lost_response"]), max_size=4))
@example(start="base", history=["pending", "revoke", "lost_response", "green"])
@example(start="source", history=["advance", "restart", "green"])
async def test_promotion_is_monotonic_and_never_rolls_back_a_newer_target(promotion, start, history):
    e = promotion
    # Reset all prior-example observations, including durable publication proof.
    e.ops.git.ambiguous = False
    e.gate.green = True
    e.github.reviews = [e.github.review(1, e.source)]
    ref = promotion_ref(e.meta["step"], e.meta)
    git(e.ops.git.remote_path, "update-ref", ref, e.source)
    async with e.db.immediate() as conn:
        await conn.execute(delete(task_metadata).where(task_metadata.c.key == PROMOTION_PUBLISH))
        await conn.execute(update(integration_batches).where(integration_batches.c.id == e.batch.id)
                           .values(cleanup_state="pending"))
    target = e.base if start in {"base", "diverged"} else e.source
    if start in {"ahead", "diverged"}:
        target = commit(e.repo.store, {"upstream.txt": "new target\n"}, base=target)
    git(e.repo.store, "push", "--force", "origin", f"{target}:{ROOT.target_ref}")
    for index, action in enumerate([*history, "green"]):
        if action == "advance":
            target = commit(e.repo.store, {f"advance-{index}.txt": "upstream\n"}, base=target)
            git(e.repo.store, "push", "origin", f"{target}:{ROOT.target_ref}")
        elif action == "restart":
            old = e.service
            e.service = PromotionVisit(e.store, e.ops, admission=e.admission, checks=e.checks,
                publish=old.publish, publish_tag=old.publish_tag, delete_ref=old.delete_ref,
                attest=old.attest, snapshot=e.snapshot)
        elif action == "pending":
            e.gate.green = False
        elif action == "revoke":
            e.github.reviews = []
        elif action == "green":
            e.gate.green = True
            e.github.reviews = [e.github.review(1, e.source)]
        else:
            e.ops.git.ambiguous = True
        before = target
        result = await e.service.visit(e.batch, (e.member,), await e.snapshot())
        target = git(e.ops.git.remote_path, "rev-parse", ROOT.target_ref)
        assert git(e.repo.store, "merge-base", "--is-ancestor", before, target) == ""
        if target != before:
            assert e.gate.green and e.github.reviews
            assert target == e.source  # promotions publish the pinned source unchanged
        if result.state == "delivered":
            assert git(e.repo.store, "merge-base", "--is-ancestor", e.source, target) == ""
        e.ops.git.ambiguous = False
    # A divergent target stays held; an already delivered source remains delivered.
    compatible = await e.ops.is_ancestor(e.repo, e.source, target)
    assert (result.state == "delivered") == compatible


@property_settings
@given(payloads=PAYLOADS, history=st.lists(st.sampled_from(["retry", "restart"]), max_size=3))
@example(payloads=[1, 2, 3], history=["retry", "restart"])
async def test_retirement_preserves_unique_work_until_a_restorable_audit_exists(
    retirement_setup, payloads, history, tmp_path,
):
    db, transport, service, checkout, remote, _ = retirement_setup
    transport.fail_delete = False
    transport.before_delete = None
    async with db.immediate() as conn:
        await conn.execute(delete(branch_retirements))
    await db.update_task("retired", status=TaskStatus.COMPLETED)
    base = git(remote, "rev-parse", "main")
    head = base
    for index, value in enumerate(payloads):
        head = commit(checkout, {f"unique-{index}.txt": f"{value}\n"}, base=head)
    git(checkout, "push", "--force", "origin", f"{head}:refs/heads/{BRANCH}")
    git(checkout, "update-ref", "refs/heads/" + BRANCH, head)
    git(checkout, "checkout", "--detach", base)
    # Ordinary completed unique work is retained by the daily retirement backstop.
    assert not await service.reconcile_project("p")
    assert git(remote, "rev-parse", BRANCH) == head
    await retirement_request(db)

    async def audit_precedes_delete(branch, sha):
        audit = next(row for row in await retirement_rows(db) if row["branch"] == BRANCH)
        backup = audit["evidence"]["remote"]
        assert sha == backup["sha"] == head
        assert Path(backup["bundle"]).is_file()
        assert head in git(checkout, "bundle", "list-heads", backup["bundle"])

    transport.before_delete = audit_precedes_delete
    for action in history:
        transport.fail_delete = True
        await service.drain_due()
        assert git(remote, "rev-parse", BRANCH) == head
        if action == "restart":
            service = type(service)(db, data_dir=tmp_path / "data", git_manager=transport,
                github_client_factory=service.github_client_factory,
                github_repository_binding_resolver=service.github_repository_binding_resolver)
        async with db.immediate() as conn:
            await conn.execute(update(branch_retirements).values(next_attempt_at=0))
    transport.fail_delete = False
    outcomes = await service.drain_due()
    assert outcomes and all(state == "complete" for state, _ in outcomes), outcomes
    assert await transport.arev_parse(str(remote), BRANCH) is None
    audit = next(row for row in await retirement_rows(db) if row["branch"] == BRANCH)
    backup = audit["evidence"]["remote"]["bundle"]
    # Remote bundles are incremental against the audited default-branch base.
    # Fetch only that public history into a fresh store, then restore unique work.
    with TemporaryDirectory(dir=tmp_path) as directory:
        restore = Path(directory) / "restore.git"
        git(tmp_path, "init", "--bare", str(restore))
        git(restore, "fetch", "--no-tags", str(remote), "refs/heads/main:refs/heads/main")
        assert git(restore, "rev-parse", "main") == audit["evidence"]["remote"]["backup_base_sha"]
        missing = await transport.arun_git_result(["cat-file", "-e", head], cwd=str(restore))
        assert missing.returncode != 0  # unique objects come only from the bundle
        git(restore, "bundle", "unbundle", backup)
        for index, value in enumerate(payloads):
            assert git(restore, "show", f"{head}:unique-{index}.txt") == str(value)
    assert not await service.drain_due()
