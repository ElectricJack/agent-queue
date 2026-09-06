"""Crash-safe exact-SHA root-to-main promotion."""

from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
from types import SimpleNamespace
from pathlib import Path

import pytest
from sqlalchemy import func, insert, select, update

import src.git.manager as git_manager_module
import src.integration.main_promotion as main_promotion_module
from src.commands.integration_commands import IntegrationCommandsMixin
from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
from src.database import Database
from src.database.tables import (
    integration_batch_members,
    integration_batches,
    integration_branch_owners,
    integration_candidate_member_results,
    integration_candidate_publications,
    integration_candidate_ref_mutations,
    integration_candidate_revisions,
    integration_check_evidence,
    integration_promotion_intents,
    integration_repair_operations,
    integration_repair_stages,
    integration_review_evidence,
    integration_root_intent_members,
    integration_outbox,
    project_integration_leases,
    task_delivery_receipts,
)
from src.integration.main_promotion import (
    RootAttestationProof,
    RootPromotionInvariantError,
    RootPromotionService as _RootPromotionService,
)
from src.integration.candidates import CandidateBuildResult, CandidateService
from src.integration.models import BranchKey, Fence
from src.integration.ownership import BranchBusy, BranchOwnership
from src.integration.promotion import PromotionInvariantError, PromotionService
from src.integration.repair import RepairService
from src.git.manager import GitError, GitManager
from src.models import Project, RepoConfig, RepoSourceType
from src.profiles.capabilities import DENY_ALL


BASE = "a" * 40
HEAD = "b" * 40
BRANCH = "refs/heads/aq/integration/p-" + "1" * 32 + "/r-" + "2" * 32


class ExactAttestationResolver:
    def __init__(self):
        self.override = {}
        self.calls = 0

    async def __call__(self, subject):
        self.calls += 1
        return RootAttestationProof(
            **(subject.model_dump() | self.override),
            check_run_id=7001,
            external_id="aq-attestation-v1:" + "9" * 64,
        )


class RootPromotionService(_RootPromotionService):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("attestation_resolver", ExactAttestationResolver())
        super().__init__(*args, **kwargs)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _real_push_case(tmp_path: Path) -> tuple[Path, Path, str, str]:
    checkout = tmp_path / "deadline-checkout"
    target = tmp_path / "deadline-target.git"
    checkout.mkdir()
    _git(checkout, "init", "--initial-branch=main")
    _git(checkout, "config", "user.name", "Deadline Test")
    _git(checkout, "config", "user.email", "deadline@example.invalid")
    (checkout / "value.txt").write_text("base")
    _git(checkout, "add", "value.txt")
    _git(checkout, "commit", "-m", "base")
    base = _git(checkout, "rev-parse", "HEAD")
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(target)],
        check=True,
        capture_output=True,
    )
    _git(checkout, "push", str(target), f"{base}:refs/heads/main")
    (checkout / "value.txt").write_text("candidate")
    _git(checkout, "commit", "-am", "candidate")
    return checkout, target, base, _git(checkout, "rev-parse", "HEAD")


def _policy() -> dict:
    return {
        "root": {
            "required_checks": {
                "version": "checks-v1",
                "names": ["unit", "postgres"],
                "producer_id": "404",
            }
        }
    }


@pytest.fixture
async def prepared_db(tmp_path):
    database = Database(str(tmp_path / "main-promotion.db"))
    await database.initialize()
    await database.create_project(Project(id="p", name="project"))
    await database.create_repo(
        RepoConfig(
            id="repo",
            project_id="p",
            source_type=RepoSourceType.CLONE,
            url="https://github.com/acme/widgets.git",
            default_branch="main",
        )
    )
    await database.update_project(
        "p",
        hierarchical_integration_mode="train",
        integration_repository_id="repo",
    )
    store = tmp_path / "integration-repositories" / f"{hashlib.sha256(b'repo').hexdigest()}.git"
    store.parent.mkdir(parents=True)
    _git(store.parent, "init", "--bare", str(store))
    # The candidate pin is established by Task9b1.  Symbolic dummy OIDs are
    # sufficient for prepare because the service pins by an injected manager.
    async with database.immediate() as conn:
        await conn.execute(
            insert(integration_batches).values(
                id="batch", project_id="p", repository_id="repo", request_id="request",
                source_manifest_digest="sha256:" + "3" * 64, base_sha=BASE,
                lifecycle="sealing", current_revision=0, integration_branch=BRANCH,
                tested_candidate_sha=HEAD, ci_evidence_id="ci-green", policy_snapshot=_policy(),
                artifact_snapshot={}, cleanup_state="pending", created_at=1.0, updated_at=1.0,
            )
        )
        for ordinal in range(2):
            await conn.execute(
                insert(integration_review_evidence).values(
                    id=f"review-{ordinal}", source_task_id=f"root-{ordinal}",
                    repository_id="repo", source_base=BASE,
                    reviewed_head_sha=chr(ord("c") + ordinal) * 40,
                    reviewed_tree_sha=chr(ord("e") + ordinal) * 40,
                    reviewer_task_id=f"reviewer-{ordinal}", review_kind="leaf", generation=1,
                    verdict="approved", evidence={"approved": True}, created_at=1.0,
                )
            )
            await conn.execute(
                insert(integration_batch_members).values(
                    batch_id="batch", ordinal=ordinal, task_id=f"root-{ordinal}",
                    pr_url=f"https://github.com/acme/widgets/pull/{ordinal + 1}",
                    repository_id="repo", source_base_sha=BASE,
                    reviewed_head_sha=chr(ord("c") + ordinal) * 40,
                    reviewed_tree_sha=chr(ord("e") + ordinal) * 40,
                    review_evidence_id=f"review-{ordinal}", review_evidence={"approved": True},
                )
            )
        await conn.execute(
            update(integration_batches)
            .where(integration_batches.c.id == "batch")
            .values(lifecycle="testing")
        )
        await conn.execute(
            insert(integration_candidate_revisions).values(
                batch_id="batch", revision=0, construction_base_sha=BASE,
                next_member_ordinal=2, head_sha=HEAD, ci_evidence_id="ci-green", state="green",
                created_at=2.0, updated_at=2.0,
            )
        )
        for ordinal in range(2):
            await conn.execute(
                insert(integration_candidate_member_results).values(
                    batch_id="batch", revision=0, member_ordinal=ordinal,
                    input_head_sha=chr(ord("c") + ordinal) * 40,
                    input_tree_sha=chr(ord("e") + ordinal) * 40,
                    generated_squash_sha=chr(ord("7") + ordinal) * 40,
                    result="applied", created_at=2.0, updated_at=2.0,
                )
            )
        await conn.execute(
            insert(integration_check_evidence).values(
                id="ci-green", operation_id="root-op", batch_id="batch", candidate_revision=0,
                producer_id="404", workflow_id="aggregate:1", run_id="attestation:1", attempt=0,
                required_check_version="checks-v1", checks={"unit": "success", "postgres": "success"},
                conclusion="success", classification="conclusive", observed_at=3.0,
            )
        )
        await conn.execute(
            insert(integration_repair_operations).values(
                id="root-op", target_kind="batch", batch_id="batch", episode_id="batch",
                active_stage=0, state="active", policy_snapshot=_policy(), artifact_snapshot={},
                required_check_version="checks-v1", created_at=1.0, updated_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_repair_stages).values(
                operation_id="root-op", ordinal=0, intelligence_class="primary",
                policy={"seconds": 60, "attempts": 2}, starting_sha=BASE, attempts=0,
                deadline_at=500.0, deadline_event_id="deadline", state="awaiting_completion",
            )
        )
        await conn.execute(
            insert(project_integration_leases).values(
                project_id="p", repository_id="repo", batch_id="batch", owner_id="lease-owner",
                fence_token=3, heartbeat_at=1.0, expires_at=1000.0,
            )
        )
        await conn.execute(
            insert(integration_branch_owners).values(
                id="branch-owner-row", repository_id="repo", ref=BRANCH, owner_id="root-op",
                owner_role="collector", fence_token=7, handoff_state="reserved",
                created_at=1.0, updated_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_candidate_publications).values(
                batch_id="batch", revision=0, state="pr_published", repository_id="repo",
                repository_numeric_id=99, repository_full_name="acme/widgets", base_ref="main",
                head_ref=BRANCH.removeprefix("refs/heads/"), head_sha=HEAD,
                expected_old_sha="0" * 40, idempotency_key="publication", pr_number=9,
                pr_url="https://github.com/acme/widgets/pull/9", created_at=2.0, updated_at=2.0,
            )
        )
    yield database, tmp_path
    await database.close()


class PinningGit:
    trusted_local = True

    def __init__(self):
        self.pins: list[tuple[str, str]] = []

    async def arun_git_result(self, args, **_kwargs):
        if args[:1] == ["rev-parse"]:
            return SimpleNamespace(returncode=0, stdout=HEAD + "\n", stderr="")
        if args[:1] == ["update-ref"]:
            self.pins.append((args[1], args[2]))
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(args)


class FakeAppClient:
    def __init__(self, remote=BASE, *, descendant_of_head=False):
        self.remote = remote
        self.descendant_of_head = descendant_of_head
        self.reads = 0
        self.token_calls = 0
        self.repository = SimpleNamespace(
            forge_host="github.com", repository_id=99, full_name="acme/widgets"
        )

    async def exact_head_ref(self, branch):
        assert branch == "main"
        self.reads += 1
        return self.remote

    async def installation_token(self):
        self.token_calls += 1
        return "dummy-installation-token"


class PushGit(PinningGit):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.pushes: list[dict] = []

    async def arun_git_result(self, args, **_kwargs):
        if args[:2] == ["merge-base", "--is-ancestor"]:
            return SimpleNamespace(
                returncode=0
                if (args[2], args[3]) == (BASE, HEAD)
                or (
                    self.app.descendant_of_head
                    and (args[2], args[3]) == (HEAD, self.app.remote)
                )
                else 1,
                stdout="",
                stderr="",
            )
        if args[:2] == ["cat-file", "-e"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return await super().arun_git_result(args, **_kwargs)

    async def afetch_exact_oid_with_app_auth(self, _store, **kwargs):
        return kwargs["oid"]

    async def apush_oid_with_app_auth(self, _store, **kwargs):
        assert kwargs["tip_oid"] == HEAD
        assert kwargs["branch"] == "main"
        assert kwargs["expected_old_oid"] == BASE
        assert kwargs["token"] == "dummy-installation-token"
        if self.app.remote != BASE:
            raise RuntimeError("expected old changed")
        self.pushes.append(kwargs)
        self.app.remote = HEAD


class InFlightPushGit(PushGit):
    def __init__(self, app):
        super().__init__(app)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.attempts = 0

    async def apush_oid_with_app_auth(self, *args, **kwargs):
        self.attempts += 1
        self.entered.set()
        await self.release.wait()
        return await super().apush_oid_with_app_auth(*args, **kwargs)


class ClockAdvancingApp(FakeAppClient):
    def __init__(self, clock, token_time):
        super().__init__()
        self.clock = clock
        self.token_time = token_time

    async def installation_token(self):
        self.clock[0] = self.token_time
        return await super().installation_token()


class ClockAdvancingGit(PushGit):
    def __init__(self, app, clock, ancestry_time):
        super().__init__(app)
        self.clock = clock
        self.ancestry_time = ancestry_time

    async def arun_git_result(self, args, **kwargs):
        if args[:2] == ["merge-base", "--is-ancestor"]:
            self.clock[0] = self.ancestry_time
        return await super().arun_git_result(args, **kwargs)


class RealDeadlinePushGit(PushGit):
    """Route the root boundary through the real isolated App-auth transport."""

    def __init__(self, app, checkout, target, base, tip):
        super().__init__(app)
        self.checkout = checkout
        self.target = target
        self.base = base
        self.tip = tip
        self.transport = GitManager()
        isolated_push = self.transport._apush_oid_with_app_auth_to_url

        async def local_isolated_push(checkout_path, **kwargs):
            kwargs["destination_url"] = self.target.as_uri()
            return await isolated_push(checkout_path, **kwargs)

        self.transport._apush_oid_with_app_auth_to_url = local_isolated_push

    async def apush_oid_with_app_auth(self, _store, **kwargs):
        deadline = kwargs["authority_deadline"]
        result = await self.transport.apush_oid_with_app_auth(
            str(self.checkout),
            repository=kwargs["repository"],
            token=kwargs["token"],
            tip_oid=self.tip,
            branch=kwargs["branch"],
            expected_old_oid=self.base,
            authority_deadline=deadline,
        )
        self.pushes.append(kwargs)
        self.app.remote = HEAD
        return HEAD if result == self.tip else result


@pytest.mark.asyncio
async def test_root_prepare_derives_exact_green_authority_and_reserves_all_members(prepared_db):
    db, data_dir = prepared_db
    git = PinningGit()
    service = RootPromotionService(db, data_dir=data_dir, git_manager=git, clock=lambda: 10.0)

    first = await service.prepare("batch", 0)
    replay = await service.prepare("batch", 0)

    assert first == replay
    assert first.outcome == "prepared"
    assert first.receipt_ids == tuple(
        RootPromotionService._receipt_id("batch", 0, ordinal) for ordinal in range(2)
    )
    assert git.pins == [(f"refs/aq/root-promotions/{first.intent_id}", HEAD)]
    async with db._engine.connect() as conn:
        intent = (await conn.execute(select(integration_promotion_intents))).mappings().one()
        reservations = (
            await conn.execute(
                select(integration_root_intent_members).order_by(
                    integration_root_intent_members.c.member_ordinal
                )
            )
        ).mappings().all()
        mutation = (await conn.execute(select(integration_candidate_ref_mutations))).mappings().one()
        lifecycle = await conn.scalar(select(integration_batches.c.lifecycle))
    assert intent["intent_kind"] == "root"
    assert (intent["root_batch_id"], intent["root_candidate_revision"]) == ("batch", 0)
    assert (intent["project_lease_owner_id"], intent["project_lease_fence_token"]) == (
        "lease-owner", 3
    )
    assert (intent["branch_fence_owner_id"], intent["branch_fence_token"]) == ("root-op", 7)
    assert [row["receipt_id"] for row in reservations] == list(first.receipt_ids)
    assert intent["receipt_id"] == reservations[0]["receipt_id"]
    assert mutation["purpose"] == "root_main"
    assert mutation["expected_old_sha"] == BASE
    assert mutation["desired_sha"] == HEAD
    assert mutation["prewrite_at"] is None
    assert lifecycle == "promoting"


@pytest.mark.asyncio
async def test_live_root_claim_blocks_owner_handoff_and_repair_expiry(prepared_db):
    db, data_dir = prepared_db
    prepared = await RootPromotionService(
        db, data_dir=data_dir, git_manager=PinningGit(), clock=lambda: 10.0
    ).prepare("batch", 0)
    assert prepared.outcome == "prepared"

    fence = Fence(
        target=BranchKey(repository_id="repo", branch=BRANCH),
        owner_id="root-op",
        token=7,
    )
    with pytest.raises(BranchBusy, match="live external mutation claim"):
        await BranchOwnership(db).transfer(fence, "next-root-op", "collector")

    async with db.immediate() as conn:
        await conn.execute(
            update(integration_repair_stages)
            .where(integration_repair_stages.c.operation_id == "root-op")
            .values(deadline_at=11.0)
        )
        await conn.execute(update(project_integration_leases).values(expires_at=11.0))
    expired = await RepairService(db).expire("root-op", 0, now=200.0)
    assert expired["outcome"] == "not_due"
    assert expired["action"] == "wait"


@pytest.mark.asyncio
async def test_root_promotion_command_denies_session_and_allows_service(prepared_db):
    db, _data_dir = prepared_db

    class StubPromotion:
        def __init__(self):
            self.calls = []

        async def promote(self, batch_id, revision):
            self.calls.append((batch_id, revision))
            return SimpleNamespace(
                outcome="promoted",
                model_dump=lambda **_kwargs: {
                    "outcome": "promoted",
                    "batch_id": batch_id,
                    "revision": revision,
                    "intent_id": "intent",
                    "receipt_ids": ("receipt",),
                    "head_sha": HEAD,
                },
            )

    class Handler(IntegrationCommandsMixin):
        pass

    promotion = StubPromotion()
    handler = Handler()
    handler.db = db
    handler.orchestrator = SimpleNamespace(root_promotion_service=promotion)
    session = ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        policy=DENY_ALL,
        session_id="worker",
        project_id="p",
    )
    with principal_context(session):
        denied = await handler._cmd_integration_promote_main(
            {"batch_id": "batch", "revision": 0}
        )
    assert denied["outcome"] == "unauthorized"
    assert promotion.calls == []

    with principal_context(ExecutionPrincipal.service("root-playbook")):
        allowed = await handler._cmd_integration_promote_main(
            {"batch_id": "batch", "revision": 0}
        )
    assert allowed["success"] is True
    assert promotion.calls == [("batch", 0)]


@pytest.mark.asyncio
async def test_root_prepare_rejects_stale_green_and_crossed_authority(prepared_db):
    db, data_dir = prepared_db
    service = RootPromotionService(
        db, data_dir=data_dir, git_manager=PinningGit(), clock=lambda: 10.0
    )
    async with db.immediate() as conn:
        await conn.execute(
            update(integration_candidate_revisions)
            .where(integration_candidate_revisions.c.batch_id == "batch")
            .values(ci_evidence_id="wrong")
        )
    result = await service.prepare("batch", 0)
    assert result.outcome == "ci_missing"
    async with db._engine.connect() as conn:
        assert await conn.scalar(select(func.count()).select_from(integration_promotion_intents)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "override",
    [
        None,
        {"required_check_version": "stale-checks"},
        {"repository_numeric_id": 100},
        {"repository_full_name": "other/widgets"},
        {"candidate_sha": "c" * 40},
        {"revision": 1},
    ],
    ids=("missing", "stale", "repository-id", "repository-name", "sha", "revision"),
)
async def test_root_prepare_requires_exact_server_attestation_before_claim(
    prepared_db, override
):
    db, data_dir = prepared_db
    app = FakeAppClient()
    git = PushGit(app)
    resolver = None
    if override is not None:
        resolver = ExactAttestationResolver()
        resolver.override = override
    result = await _RootPromotionService(
        db,
        data_dir=data_dir,
        git_manager=git,
        app_client=app,
        attestation_resolver=resolver,
        clock=lambda: 10.0,
    ).promote("batch", 0)
    assert result.outcome == "configuration_blocked"
    assert git.pins == [] and git.pushes == [] and app.reads == 0
    async with db._engine.connect() as conn:
        assert await conn.scalar(
            select(func.count()).select_from(integration_promotion_intents)
        ) == 0
        assert await conn.scalar(
            select(func.count()).select_from(integration_candidate_ref_mutations)
        ) == 0


@pytest.mark.asyncio
async def test_attestation_is_frozen_and_revalidated_before_prewrite(prepared_db):
    db, data_dir = prepared_db
    app = FakeAppClient()
    git = PushGit(app)
    resolver = ExactAttestationResolver()
    service = _RootPromotionService(
        db,
        data_dir=data_dir,
        git_manager=git,
        app_client=app,
        attestation_resolver=resolver,
        clock=lambda: 10.0,
    )
    prepared = await service.prepare("batch", 0)
    assert prepared.outcome == "prepared"
    resolver.override = {"candidate_sha": "c" * 40}

    blocked = await service.reconcile(prepared.intent_id)
    assert blocked.outcome == "configuration_blocked"
    assert git.pushes == [] and app.reads == 1
    async with db._engine.connect() as conn:
        intent = (
            await conn.execute(select(integration_promotion_intents))
        ).mappings().one()
        mutation = (
            await conn.execute(select(integration_candidate_ref_mutations))
        ).mappings().one()
    assert intent["provenance"]["attestation"]["candidate_sha"] == HEAD
    assert mutation["prewrite_at"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", [False, True], ids=("missing", "mismatched"))
async def test_root_command_cannot_bypass_attestation_proof(prepared_db, mismatch):
    db, data_dir = prepared_db
    app = FakeAppClient()
    git = PushGit(app)
    resolver = None
    if mismatch:
        resolver = ExactAttestationResolver()
        resolver.override = {"operation_id": "another-operation"}
    root = _RootPromotionService(
        db,
        data_dir=data_dir,
        git_manager=git,
        app_client=app,
        attestation_resolver=resolver,
        clock=lambda: 10.0,
    )

    class Handler(IntegrationCommandsMixin):
        pass

    handler = Handler()
    handler.db = db
    handler.orchestrator = SimpleNamespace(root_promotion_service=root)
    with principal_context(ExecutionPrincipal.service("root-playbook")):
        result = await handler._cmd_integration_promote_main(
            {"batch_id": "batch", "revision": 0}
        )
    assert result["outcome"] == "configuration_blocked"
    assert git.pushes == [] and app.reads == 0
    async with db._engine.connect() as conn:
        assert await conn.scalar(
            select(func.count()).select_from(integration_candidate_ref_mutations)
        ) == 0


@pytest.mark.asyncio
async def test_root_prepare_empty_replays_without_durable_side_effects(tmp_path):
    db = Database(str(tmp_path / "empty.db"))
    await db.initialize()
    await db.create_project(Project(id="p", name="project"))
    await db.create_repo(
        RepoConfig(id="repo", project_id="p", source_type=RepoSourceType.CLONE,
                   url="https://github.com/acme/widgets.git", default_branch="main")
    )
    await db.update_project("p", hierarchical_integration_mode="train",
                            integration_repository_id="repo")
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_batches).values(
                id="batch", project_id="p", repository_id="repo", request_id="request",
                source_manifest_digest="sha256:" + "3" * 64, base_sha=None,
                lifecycle="empty", current_revision=0, integration_branch=None,
                policy_snapshot=_policy(), artifact_snapshot={}, cleanup_state="complete",
                created_at=1.0, updated_at=1.0,
            )
        )
    try:
        service = RootPromotionService(db, data_dir=tmp_path, git_manager=PinningGit(), clock=lambda: 10.0)
        result = await service.prepare("batch", 0)
        assert result.outcome == "already_promoted"
        assert result.intent_id is None and result.receipt_ids == ()
        async with db._engine.connect() as conn:
            assert await conn.scalar(select(func.count()).select_from(integration_promotion_intents)) == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_child_finalizer_hard_rejects_root_intent():
    class NeverFinalize:
        async def finalize_integration_promotion(self, *_args, **_kwargs):
            raise AssertionError("child finalizer touched root state")

    service = PromotionService.__new__(PromotionService)
    service.db = NeverFinalize()

    with pytest.raises(PromotionInvariantError, match="root"):
        await service._finalize(
            {"id": "root-intent", "intent_kind": "root"}, "b" * 40
        )


@pytest.mark.asyncio
async def test_exact_tested_sha_main_push_finalizes_every_member_without_post_ci(prepared_db):
    db, data_dir = prepared_db
    app = FakeAppClient()
    git = PushGit(app)
    result = await RootPromotionService(
        db, data_dir=data_dir, git_manager=git, app_client=app, clock=lambda: 10.0
    ).promote("batch", 0)

    assert result.outcome == "promoted"
    assert result.head_sha == HEAD
    assert len(result.receipt_ids) == 2
    assert app.remote == HEAD and len(git.pushes) == 1 and app.token_calls == 1
    async with db._engine.connect() as conn:
        intent = (await conn.execute(select(integration_promotion_intents))).mappings().one()
        mutation = (await conn.execute(select(integration_candidate_ref_mutations))).mappings().one()
    assert intent["state"] == "committed"
    assert mutation["state"] == "applied"


@pytest.mark.asyncio
async def test_expired_inflight_prewrite_is_blocked_until_remote_proves_result(prepared_db):
    db, data_dir = prepared_db
    app = FakeAppClient()
    git = InFlightPushGit(app)
    first_task = asyncio.create_task(
        RootPromotionService(
            db, data_dir=data_dir, git_manager=git, app_client=app, clock=lambda: 10.0
        ).promote("batch", 0)
    )
    await asyncio.wait_for(git.entered.wait(), timeout=1.0)
    second_task = asyncio.create_task(
        RootPromotionService(
            db, data_dir=data_dir, git_manager=git, app_client=app, clock=lambda: 146.0
        ).promote("batch", 0)
    )
    try:
        done, _pending = await asyncio.wait({second_task}, timeout=0.25)
        assert second_task in done
        blocked = second_task.result()
        assert blocked.outcome == "reconciliation_blocked"
        assert git.attempts == 1
    finally:
        git.release.set()
        await asyncio.gather(first_task, second_task, return_exceptions=True)

    recovered = await first_task
    assert recovered.outcome == "promoted"
    assert len(git.pushes) == 1


@pytest.mark.asyncio
async def test_owned_unmarked_claim_renews_full_horizon_before_prewrite(prepared_db):
    db, data_dir = prepared_db
    app = FakeAppClient()
    git = PushGit(app)
    now = [10.0]
    service = RootPromotionService(
        db,
        data_dir=data_dir,
        git_manager=git,
        app_client=app,
        clock=lambda: now[0],
    )
    prepared = await service.prepare("batch", 0)
    assert prepared.outcome == "prepared"
    now[0] = 30.0

    result = await service.reconcile(prepared.intent_id)
    assert result.outcome == "promoted"
    async with db._engine.connect() as conn:
        claim = (
            await conn.execute(select(integration_candidate_ref_mutations))
        ).mappings().one()
    assert claim["prewrite_at"] == 30.0
    assert claim["expires_at"] >= 165.0


@pytest.mark.asyncio
@pytest.mark.parametrize("limiting_horizon", ["claim", "lease"])
async def test_prewrite_rechecks_horizon_after_ancestry_and_token_refresh(
    prepared_db, limiting_horizon
):
    db, data_dir = prepared_db
    now = [10.0]
    app = ClockAdvancingApp(now, token_time=21.0)
    git = ClockAdvancingGit(app, now, ancestry_time=15.0)
    service = RootPromotionService(
        db,
        data_dir=data_dir,
        git_manager=git,
        app_client=app,
        clock=lambda: now[0],
    )
    prepared = await service.prepare("batch", 0)
    async with db.immediate() as conn:
        if limiting_horizon == "claim":
            await conn.execute(update(project_integration_leases).values(expires_at=1000.0))
        else:
            await conn.execute(
                update(integration_candidate_ref_mutations).values(expires_at=1000.0)
            )
            await conn.execute(update(project_integration_leases).values(expires_at=145.0))

    result = await service.reconcile(prepared.intent_id)

    assert result.outcome == "wait"
    assert git.pushes == [] and app.token_calls == 1
    async with db._engine.connect() as conn:
        claim = (
            await conn.execute(select(integration_candidate_ref_mutations))
        ).mappings().one()
        intent = (
            await conn.execute(select(integration_promotion_intents))
        ).mappings().one()
    assert claim["state"] == "reserved" and claim["prewrite_at"] is None
    assert "dummy-installation-token" not in repr((claim, intent))


@pytest.mark.asyncio
async def test_prewrite_accepts_exact_post_refresh_push_horizon(prepared_db):
    db, data_dir = prepared_db
    now = [10.0]
    app = ClockAdvancingApp(now, token_time=20.0)
    git = ClockAdvancingGit(app, now, ancestry_time=15.0)
    service = RootPromotionService(
        db,
        data_dir=data_dir,
        git_manager=git,
        app_client=app,
        clock=lambda: now[0],
    )
    prepared = await service.prepare("batch", 0)
    async with db.immediate() as conn:
        await conn.execute(update(project_integration_leases).values(expires_at=145.0))

    result = await service.reconcile(prepared.intent_id)

    assert result.outcome == "promoted"
    assert len(git.pushes) == 1 and app.token_calls == 1
    assert isinstance(git.pushes[0]["authority_deadline"], float)
    async with db._engine.connect() as conn:
        claim = (
            await conn.execute(select(integration_candidate_ref_mutations))
        ).mappings().one()
    assert claim["prewrite_at"] == 20.0


@pytest.mark.asyncio
async def test_prewrite_horizon_is_measured_after_hierarchy_lock(prepared_db):
    db, data_dir = prepared_db
    now = [10.0]
    app = ClockAdvancingApp(now, token_time=20.0)
    git = ClockAdvancingGit(app, now, ancestry_time=15.0)
    service = RootPromotionService(
        db,
        data_dir=data_dir,
        git_manager=git,
        app_client=app,
        clock=lambda: now[0],
    )
    prepared = await service.prepare("batch", 0)
    original_lock = db.lock_hierarchy_project

    async def advancing_lock(conn, project_id):
        await original_lock(conn, project_id)
        if now[0] == 20.0:
            now[0] = 21.0

    db.lock_hierarchy_project = advancing_lock

    result = await service.reconcile(prepared.intent_id)

    assert result.outcome == "wait" and git.pushes == []
    async with db._engine.connect() as conn:
        claim = (
            await conn.execute(select(integration_candidate_ref_mutations))
        ).mappings().one()
    assert claim["prewrite_at"] is None


@pytest.mark.asyncio
async def test_prewrite_deadline_consumes_dispatch_delay_in_real_app_transport(
    prepared_db, tmp_path, monkeypatch
):
    db, data_dir = prepared_db
    checkout, target, real_base, real_tip = _real_push_case(tmp_path)
    app = FakeAppClient()
    git = RealDeadlinePushGit(app, checkout, target, real_base, real_tip)
    original_import = git.transport._run_isolated_import_git

    async def delayed_import(args, **kwargs):
        await asyncio.sleep(0.02)
        return await original_import(args, **kwargs)

    monkeypatch.setattr(git.transport, "_run_isolated_import_git", delayed_import)
    monkeypatch.setattr(main_promotion_module, "APP_AUTH_PUSH_TIMEOUT_SECONDS", 0.4)
    monkeypatch.setattr(main_promotion_module, "APP_AUTH_PUSH_CLEANUP_MARGIN_SECONDS", 0.1)
    monkeypatch.setattr(git_manager_module, "APP_AUTH_PUSH_TIMEOUT_SECONDS", 0.4)
    monkeypatch.setattr(git_manager_module, "APP_AUTH_PUSH_CLEANUP_MARGIN_SECONDS", 0.1)
    prewrite_seen_at = None

    async def delayed_dispatch(phase):
        nonlocal prewrite_seen_at
        if phase == "after_prewrite_marker":
            prewrite_seen_at = asyncio.get_running_loop().time()
            await asyncio.sleep(0.1)

    result = await RootPromotionService(
        db,
        data_dir=data_dir,
        git_manager=git,
        app_client=app,
        crash_hook=delayed_dispatch,
        clock=lambda: 10.0,
    ).promote("batch", 0)

    assert result.outcome == "promoted"
    assert len(git.pushes) == 1
    deadline = git.pushes[0]["authority_deadline"]
    assert prewrite_seen_at is not None
    assert 0 < deadline - prewrite_seen_at <= 0.4
    assert _git(target, "rev-parse", "refs/heads/main") == real_tip


@pytest.mark.asyncio
async def test_exhausted_prewrite_deadline_never_starts_real_remote_child(
    prepared_db, tmp_path, monkeypatch
):
    db, data_dir = prepared_db
    checkout, target, real_base, real_tip = _real_push_case(tmp_path)
    app = FakeAppClient()
    git = RealDeadlinePushGit(app, checkout, target, real_base, real_tip)
    remote_starts = 0
    original_spawn = asyncio.create_subprocess_exec

    async def recording_spawn(program, *args, **kwargs):
        nonlocal remote_starts
        if "push" in args:
            remote_starts += 1
        return await original_spawn(program, *args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", recording_spawn)
    monkeypatch.setattr(main_promotion_module, "APP_AUTH_PUSH_TIMEOUT_SECONDS", 0.08)
    monkeypatch.setattr(main_promotion_module, "APP_AUTH_PUSH_CLEANUP_MARGIN_SECONDS", 0.05)
    monkeypatch.setattr(git_manager_module, "APP_AUTH_PUSH_TIMEOUT_SECONDS", 0.08)
    monkeypatch.setattr(git_manager_module, "APP_AUTH_PUSH_CLEANUP_MARGIN_SECONDS", 0.05)

    async def exhaust_before_dispatch(phase):
        if phase == "after_prewrite_marker":
            await asyncio.sleep(0.1)

    with pytest.raises(GitError, match="authority deadline expired"):
        await RootPromotionService(
            db,
            data_dir=data_dir,
            git_manager=git,
            app_client=app,
            crash_hook=exhaust_before_dispatch,
            clock=lambda: 10.0,
        ).promote("batch", 0)

    assert remote_starts == 0
    assert git.pushes == []
    assert _git(target, "rev-parse", "refs/heads/main") == real_base
    async with db._engine.connect() as conn:
        claim = (
            await conn.execute(select(integration_candidate_ref_mutations))
        ).mappings().one()
    assert claim["state"] == "reserved" and claim["prewrite_at"] == 10.0


@pytest.mark.asyncio
async def test_two_concurrent_root_activations_make_one_main_write(prepared_db):
    db, data_dir = prepared_db
    app = FakeAppClient()
    git = PushGit(app)
    first = RootPromotionService(
        db, data_dir=data_dir, git_manager=git, app_client=app, clock=lambda: 10.0
    )
    second = RootPromotionService(
        db, data_dir=data_dir, git_manager=git, app_client=app, clock=lambda: 10.0
    )

    results = await asyncio.gather(
        first.promote("batch", 0), second.promote("batch", 0)
    )

    assert len(git.pushes) == 1
    assert {result.outcome for result in results} <= {
        "promoted",
        "already_promoted",
        "wait",
    }
    assert any(result.outcome == "promoted" for result in results)


@pytest.mark.asyncio
async def test_lost_push_response_reconciles_applied_without_second_push(prepared_db):
    db, data_dir = prepared_db
    app = FakeAppClient()
    git = PushGit(app)

    async def crash_after(phase):
        if phase == "after_external_push":
            raise RuntimeError("lost response")

    with pytest.raises(RuntimeError, match="lost response"):
        await RootPromotionService(
            db, data_dir=data_dir, git_manager=git, app_client=app,
            crash_hook=crash_after, clock=lambda: 10.0,
        ).promote("batch", 0)
    assert app.remote == HEAD and len(git.pushes) == 1

    result = await RootPromotionService(
        db, data_dir=data_dir, git_manager=git, app_client=app, clock=lambda: 11.0
    ).promote("batch", 0)
    assert result.outcome == "promoted"
    assert len(git.pushes) == 1


@pytest.mark.asyncio
async def test_post_main_recovery_uses_frozen_proof_without_attestation_observation(prepared_db):
    db, data_dir = prepared_db
    app = FakeAppClient()
    git = PushGit(app)
    resolver = ExactAttestationResolver()

    async def crash_after(phase):
        if phase == "after_external_push":
            raise RuntimeError("lost response")

    with pytest.raises(RuntimeError, match="lost response"):
        await _RootPromotionService(
            db,
            data_dir=data_dir,
            git_manager=git,
            app_client=app,
            attestation_resolver=resolver,
            crash_hook=crash_after,
            clock=lambda: 10.0,
        ).promote("batch", 0)
    calls_before_recovery = resolver.calls

    async def forbidden_after_main(_subject):
        raise AssertionError("post-main attestation observation")

    recovered = await _RootPromotionService(
        db,
        data_dir=data_dir,
        git_manager=git,
        app_client=app,
        attestation_resolver=forbidden_after_main,
        clock=lambda: 11.0,
    ).promote("batch", 0)
    assert recovered.outcome == "promoted"
    assert resolver.calls == calls_before_recovery
    assert len(git.pushes) == 1


@pytest.mark.asyncio
async def test_candidate_service_cannot_consume_root_claim_after_main_push(prepared_db):
    db, data_dir = prepared_db
    app = FakeAppClient()
    git = PushGit(app)

    async def crash_after_push(phase):
        if phase == "after_external_push":
            raise RuntimeError("root stopped before proof")

    with pytest.raises(RuntimeError, match="root stopped before proof"):
        await RootPromotionService(
            db,
            data_dir=data_dir,
            git_manager=git,
            app_client=app,
            crash_hook=crash_after_push,
            clock=lambda: 10.0,
        ).promote("batch", 0)
    assert len(git.pushes) == 1

    candidate = CandidateService(
        db,
        data_dir=data_dir,
        git_manager=git,
        app_client=app,
        forge_provider=object(),
        clock=lambda: 11.0,
    )
    built = await candidate.build("batch")
    rebuilt = await candidate.rebuild("batch", 0, HEAD)
    assert built.outcome == "wait"
    assert rebuilt.outcome == "wait"
    async with db._engine.connect() as conn:
        mutation = (
            await conn.execute(select(integration_candidate_ref_mutations))
        ).mappings().one()
        intent = (
            await conn.execute(select(integration_promotion_intents))
        ).mappings().one()
    assert mutation["purpose"] == "root_main" and mutation["state"] == "reserved"
    assert intent["state"] == "prepared"

    recovered = await RootPromotionService(
        db,
        data_dir=data_dir,
        git_manager=git,
        app_client=app,
        clock=lambda: 12.0,
    ).promote("batch", 0)
    assert recovered.outcome == "promoted"
    assert len(recovered.receipt_ids) == 2
    assert len(git.pushes) == 1


@pytest.mark.asyncio
async def test_root_reconcile_repairs_applied_claim_with_prepared_intent(prepared_db):
    db, data_dir = prepared_db
    app = FakeAppClient(HEAD)
    git = PushGit(app)
    prepared = await RootPromotionService(
        db, data_dir=data_dir, git_manager=git, app_client=app, clock=lambda: 10.0
    ).prepare("batch", 0)
    async with db.immediate() as conn:
        await conn.execute(
            update(integration_candidate_ref_mutations).values(
                state="applied", remote_sha=HEAD, updated_at=11.0
            )
        )

    recovered = await RootPromotionService(
        db, data_dir=data_dir, git_manager=git, app_client=app, clock=lambda: 12.0
    ).reconcile(prepared.intent_id)
    assert recovered.outcome == "promoted"
    assert len(recovered.receipt_ids) == 2 and git.pushes == []
    async with db._engine.connect() as conn:
        assert await conn.scalar(select(integration_promotion_intents.c.state)) == "committed"


@pytest.mark.asyncio
async def test_obsolete_unattempted_intent_supersedes_but_ambiguous_write_blocks(prepared_db):
    db, data_dir = prepared_db
    app = FakeAppClient()
    git = PushGit(app)
    service = RootPromotionService(
        db, data_dir=data_dir, git_manager=git, app_client=app, clock=lambda: 10.0
    )
    prepared = await service.prepare("batch", 0)
    async with db.immediate() as conn:
        await conn.execute(update(integration_batches).values(current_revision=1))
    superseded = await RootPromotionService(
        db, data_dir=data_dir, git_manager=git, app_client=app, clock=lambda: 146.0
    ).reconcile(prepared.intent_id)
    assert superseded.outcome == "base_moved"
    async with db._engine.connect() as conn:
        assert await conn.scalar(select(integration_promotion_intents.c.state)) == "superseded"
        assert await conn.scalar(select(integration_candidate_ref_mutations.c.state)) == "superseded"


@pytest.mark.asyncio
async def test_current_moved_main_expires_then_public_rebuild_creates_next_revision(prepared_db):
    db, data_dir = prepared_db
    moved = "d" * 40
    app = FakeAppClient(moved)
    git = PushGit(app)
    first = RootPromotionService(
        db, data_dir=data_dir, git_manager=git, app_client=app, clock=lambda: 10.0
    )
    waiting = await first.promote("batch", 0)
    assert waiting.outcome == "wait"

    released = await RootPromotionService(
        db, data_dir=data_dir, git_manager=git, app_client=app, clock=lambda: 146.0
    ).reconcile(waiting.intent_id)
    assert released.outcome == "base_moved"

    class RebuildProbe(CandidateService):
        async def build(self, batch_id):
            async with self.db._engine.connect() as conn:
                revision = await conn.scalar(
                    select(integration_batches.c.current_revision).where(
                        integration_batches.c.id == batch_id
                    )
                )
            return CandidateBuildResult(
                outcome="built", batch_id=batch_id, revision=int(revision)
            )

    rebuilt = await RebuildProbe(
        db,
        data_dir=data_dir,
        git_manager=git,
        app_client=app,
        forge_provider=object(),
        clock=lambda: 146.0,
    ).rebuild("batch", 0, moved)
    assert rebuilt.outcome == "built" and rebuilt.revision == 1
    async with db._engine.connect() as conn:
        revisions = (
            await conn.execute(
                select(
                    integration_candidate_revisions.c.revision,
                    integration_candidate_revisions.c.state,
                ).order_by(integration_candidate_revisions.c.revision)
            )
        ).all()
        members = (
            await conn.execute(
                select(
                    integration_batch_members.c.ordinal,
                    integration_batch_members.c.task_id,
                    integration_batch_members.c.review_evidence_id,
                ).order_by(integration_batch_members.c.ordinal)
            )
        ).all()
        stage = (
            await conn.execute(select(integration_repair_stages))
        ).mappings().one()
    assert revisions == [(0, "superseded"), (1, "constructing")]
    assert members == [(0, "root-0", "review-0"), (1, "root-1", "review-1")]
    assert stage["attempts"] == 0 and stage["deadline_at"] == 500.0
    assert stage["current_subject"] == {
        "kind": "batch",
        "revision": 1,
        "candidate_sha": moved,
    }
    assert not git.pushes


@pytest.mark.asyncio
async def test_obsolete_supersession_cannot_erase_a_successor_claim(prepared_db):
    db, data_dir = prepared_db
    service = RootPromotionService(
        db, data_dir=data_dir, git_manager=PinningGit(), clock=lambda: 146.0
    )
    prepared = await RootPromotionService(
        db, data_dir=data_dir, git_manager=PinningGit(), clock=lambda: 10.0
    ).prepare("batch", 0)
    intent = await service._intent(prepared.intent_id)
    stale_claim = await service._mutation(prepared.intent_id)
    async with db.immediate() as conn:
        await conn.execute(update(integration_batches).values(current_revision=1))
        await conn.execute(
            update(integration_candidate_ref_mutations).values(
                nonce="successor", expires_at=1000.0, updated_at=20.0
            )
        )

    with pytest.raises(RootPromotionInvariantError, match="cannot be superseded"):
        await service._supersede_unattempted(intent, stale_claim)
    async with db._engine.connect() as conn:
        assert await conn.scalar(select(integration_candidate_ref_mutations.c.state)) == "reserved"
        assert await conn.scalar(select(integration_promotion_intents.c.state)) == "prepared"


@pytest.mark.asyncio
async def test_crossed_lease_and_branch_fences_cannot_take_over_main_claim(prepared_db):
    db, data_dir = prepared_db
    app = FakeAppClient()
    git = PushGit(app)
    prepared = await RootPromotionService(
        db, data_dir=data_dir, git_manager=git, app_client=app, clock=lambda: 10.0
    ).prepare("batch", 0)
    async with db.immediate() as conn:
        await conn.execute(update(project_integration_leases).values(fence_token=7, expires_at=2000.0))
        await conn.execute(update(integration_branch_owners).values(fence_token=8))
    result = await RootPromotionService(
        db, data_dir=data_dir, git_manager=git, app_client=app, clock=lambda: 146.0
    ).reconcile(prepared.intent_id)
    assert result.outcome == "wait"
    assert not git.pushes


@pytest.mark.asyncio
async def test_obsolete_crash_during_write_is_ambiguous_not_superseded(prepared_db):
    db, data_dir = prepared_db
    app = FakeAppClient()
    git = PushGit(app)

    async def crash(phase):
        if phase == "after_prewrite_marker":
            raise RuntimeError("stop")

    with pytest.raises(RuntimeError, match="stop"):
        await RootPromotionService(
            db, data_dir=data_dir, git_manager=git, app_client=app,
            crash_hook=crash, clock=lambda: 10.0,
        ).promote("batch", 0)
    async with db.immediate() as conn:
        await conn.execute(update(integration_batches).values(current_revision=1))
    result = await RootPromotionService(
        db, data_dir=data_dir, git_manager=git, app_client=app, clock=lambda: 146.0
    ).reconcile(RootPromotionService._identity("batch", 0)["intent_id"])
    assert result.outcome == "reconciliation_blocked"
    async with db._engine.connect() as conn:
        assert await conn.scalar(select(integration_promotion_intents.c.state)) == "prepared"


@pytest.mark.asyncio
async def test_authenticated_descendant_main_finalizes_original_without_push(prepared_db):
    db, data_dir = prepared_db
    descendant = "d" * 40
    app = FakeAppClient(descendant, descendant_of_head=True)
    git = PushGit(app)
    service = RootPromotionService(
        db, data_dir=data_dir, git_manager=git, app_client=app, clock=lambda: 10.0
    )
    prepared = await service.prepare("batch", 0)
    result = await service.reconcile(prepared.intent_id)
    assert result.outcome == "promoted"
    assert not git.pushes and result.head_sha == HEAD
    async with db._engine.connect() as conn:
        assert await conn.scalar(select(integration_batches.c.final_main_sha)) == descendant


@pytest.mark.asyncio
async def test_authenticated_descendant_is_imported_before_real_ancestry_proof(prepared_db):
    db, data_dir = prepared_db
    store = data_dir / "integration-repositories" / f"{hashlib.sha256(b'repo').hexdigest()}.git"
    empty_tree = _git(store, "mktree")
    commit_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }

    def commit(message, *parents):
        args = ["git", "commit-tree", empty_tree]
        for parent in parents:
            args.extend(["-p", parent])
        return subprocess.run(
            [*args, "-m", message],
            cwd=store,
            env=commit_env,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    base = commit("base")
    head = commit("candidate", base)
    _git(store, "update-ref", "refs/heads/main", head)
    origin = data_dir / "origin.git"
    _git(data_dir, "clone", "--bare", str(store), str(origin))
    descendant = subprocess.run(
        ["git", "commit-tree", empty_tree, "-p", head, "-m", "descendant"],
        cwd=origin,
        env=commit_env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    _git(origin, "update-ref", "refs/heads/main", descendant)
    assert subprocess.run(
        ["git", "cat-file", "-e", f"{descendant}^{{commit}}"], cwd=store
    ).returncode != 0

    async with db.immediate() as conn:
        # Rebind the generic fixture to the real object graph before the root
        # intent exists; publication immutability itself is covered elsewhere.
        await conn.exec_driver_sql("DROP TRIGGER trg_candidate_publication_identity")
        await conn.execute(
            update(integration_candidate_revisions).values(
                construction_base_sha=base, head_sha=head
            )
        )
        await conn.execute(
            update(integration_batches).values(tested_candidate_sha=head)
        )
        await conn.execute(
            update(integration_candidate_publications).values(head_sha=head)
        )

    class LocalExactFetchGit(GitManager):
        async def afetch_exact_oid_with_app_auth(self, destination_git_dir, **kwargs):
            return await self._afetch_exact_oid_with_app_auth_to_url(
                destination_git_dir,
                destination_url=origin.as_uri(),
                token=kwargs["token"],
                oid=kwargs["oid"],
                destination_ref=kwargs["destination_ref"],
            )

    app = FakeAppClient(descendant)
    result = await RootPromotionService(
        db,
        data_dir=data_dir,
        git_manager=LocalExactFetchGit(),
        app_client=app,
        clock=lambda: 10.0,
    ).promote("batch", 0)
    assert result.outcome == "promoted"
    assert _git(
        store,
        "rev-parse",
        f"refs/aq/root-main-observed/{result.intent_id}^{{commit}}",
    ) == descendant


@pytest.mark.asyncio
async def test_mid_receipt_crash_rolls_back_entire_root_finalization(prepared_db):
    db, data_dir = prepared_db
    app = FakeAppClient()
    git = PushGit(app)

    async def crash(phase):
        if phase == "after_root_receipt:0":
            raise RuntimeError("mid-finalize")

    with pytest.raises(RuntimeError, match="mid-finalize"):
        await RootPromotionService(
            db, data_dir=data_dir, git_manager=git, app_client=app,
            crash_hook=crash, clock=lambda: 10.0,
        ).promote("batch", 0)
    async with db._engine.connect() as conn:
        assert await conn.scalar(select(func.count()).select_from(integration_root_intent_members)) == 2
        assert await conn.scalar(select(func.count()).select_from(task_delivery_receipts)) == 0
        assert await conn.scalar(select(func.count()).select_from(integration_outbox)) == 0
        assert await conn.scalar(select(integration_promotion_intents.c.state)) == "pushed"
    recovered = await RootPromotionService(
        db, data_dir=data_dir, git_manager=git, app_client=app, clock=lambda: 11.0
    ).promote("batch", 0)
    assert recovered.outcome == "promoted"
    assert len(git.pushes) == 1


@pytest.mark.asyncio
async def test_real_git_ancestry_proof_uses_exact_candidate_graph(tmp_path):
    store = tmp_path / "graph.git"
    _git(tmp_path, "init", "--bare", str(store))
    empty_tree = subprocess.run(
        ["git", "mktree"], cwd=store, input="", text=True,
        check=True, capture_output=True,
    ).stdout.strip()
    env = {
        "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }
    base = subprocess.run(
        ["git", "commit-tree", empty_tree, "-m", "base"], cwd=store,
        env={**os.environ, **env}, text=True, check=True, capture_output=True,
    ).stdout.strip()
    head = subprocess.run(
        ["git", "commit-tree", empty_tree, "-p", base, "-m", "head"], cwd=store,
        env={**os.environ, **env}, text=True, check=True, capture_output=True,
    ).stdout.strip()
    service = RootPromotionService(None, data_dir=tmp_path, git_manager=GitManager())
    assert await service._is_ancestor(store, base, head) is True
    assert await service._is_ancestor(store, head, base) is False
