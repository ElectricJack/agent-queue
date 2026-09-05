"""Prepared child-to-parent promotion and crash recovery."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import func, insert, select
from unittest.mock import AsyncMock
from sqlalchemy.exc import DBAPIError, IntegrityError

from src.database import Database
from src.database.tables import (
    integration_outbox,
    task_branch_origins,
    task_delivery_receipts,
)
from src.git.manager import GitManager
from src.integration.models import BranchKey, PromotionInput
from src.integration.ownership import BranchOwnership
from src.models import Project, RepoConfig, RepoSourceType, Task


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "promotion.db"))
    await database.initialize()
    await database.create_project(Project(id="project", name="Promotion project"))
    await database.create_repo(
        RepoConfig(
            id="repo",
            project_id="project",
            source_type=RepoSourceType.CLONE,
            url=str(tmp_path / "origin.git"),
        )
    )
    await database.create_task(
        Task(
            id="parent",
            project_id="project",
            title="Parent",
            description="",
            repo_id="repo",
            branch_name="aq/parent",
        )
    )
    await database.create_task(
        Task(
            id="child",
            project_id="project",
            title="Child",
            description="",
            parent_task_id="parent",
            repo_id="repo",
            branch_name="aq/child",
        )
    )
    yield database
    await database.close()


def _review(*, evidence_id: str, generation: int, verdict: str = "approved") -> dict:
    return {
        "id": evidence_id,
        "source_task_id": "child",
        "repository_id": "repo",
        "source_base": "a" * 40,
        "reviewed_head_sha": "b" * 40,
        "reviewed_tree_sha": "c" * 40,
        "reviewer_task_id": "review",
        "reviewer_session_attempt_id": "attempt",
        "review_kind": "leaf",
        "generation": generation,
        "verdict": verdict,
        "evidence": {"checks": ["focused"]},
        "created_at": float(generation),
    }


async def test_review_evidence_uses_latest_generation_and_fails_closed(db):
    await db.append_integration_review_evidence(_review(evidence_id="approved", generation=1))
    found = await db.get_applicable_integration_review_evidence(
        source_task_id="child",
        repository_id="repo",
        source_base="a" * 40,
        reviewed_head_sha="b" * 40,
        current_generation=1,
    )
    assert found and found["id"] == "approved"

    await db.append_integration_review_evidence(
        _review(evidence_id="rejected", generation=2, verdict="rejected")
    )
    assert (
        await db.get_applicable_integration_review_evidence(
            source_task_id="child",
            repository_id="repo",
            source_base="a" * 40,
            reviewed_head_sha="b" * 40,
            current_generation=2,
        )
        is None
    )


async def test_intent_reservation_reuses_domain_and_blocks_other_target_work(db):
    values = {
        "domain_key": "domain",
        "operation_key": "activation-1",
        "project_id": "project",
        "receipt_id": "receipt-1",
        "source_task_id": "child",
        "target_task_id": "parent",
        "source_head": "b" * 40,
        "source_base": "a" * 40,
        "repository_id": "repo",
        "origin_url": "/remote.git",
        "target_branch": "aq/parent",
        "expected_target": "d" * 40,
        "fence_owner_id": "collector",
        "fence_token": 1,
        "review_evidence": _review(evidence_id="approved", generation=1),
        "authors": [{"name": "Author", "email": "author@example.test"}],
        "provenance": {"principal": "service:collector"},
        "commit_metadata": {"message": "message"},
        "created_at": 1.0,
    }
    first = await db.reserve_integration_promotion_intent(values)
    again = await db.reserve_integration_promotion_intent(values | {"receipt_id": "other"})
    assert first["id"] == again["id"]
    assert again["receipt_id"] == "receipt-1"

    with pytest.raises(ValueError, match="unresolved promotion"):
        await db.reserve_integration_promotion_intent(
            values
            | {
                "domain_key": "other-domain",
                "source_task_id": "other-child",
                "receipt_id": "receipt-2",
            }
        )


def _git(args: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


@pytest.fixture
async def promotion_case(db, tmp_path):
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    _git(["init", "--bare", "--initial-branch=main", str(origin)])
    _git(["clone", str(origin), str(work)])
    _git(["config", "user.name", "Seed"], work)
    _git(["config", "user.email", "seed@example.test"], work)
    (work / "shared.txt").write_text("base\n")
    _git(["add", "shared.txt"], work)
    _git(["commit", "-m", "base"], work)
    base = _git(["rev-parse", "HEAD"], work)
    _git(["push", "origin", "main"], work)
    _git(["push", "origin", f"{base}:refs/heads/aq/parent"], work)

    _git(["switch", "-c", "aq/child"], work)
    _git(["config", "user.name", "Bob Builder"], work)
    _git(["config", "user.email", "bob@example.test"], work)
    (work / "child.txt").write_text("one\n")
    _git(["add", "child.txt"], work)
    _git(["commit", "-m", "first\n\nCo-authored-by: Carol Coder <carol@example.test>"], work)
    _git(["config", "user.name", "Alice Author"], work)
    _git(["config", "user.email", "alice@example.test"], work)
    (work / "child.txt").write_text("one\ntwo\n")
    _git(["commit", "-am", "second"], work)
    head = _git(["rev-parse", "HEAD"], work)
    tree = _git(["rev-parse", "HEAD^{tree}"], work)
    _git(["push", "origin", "aq/child"], work)

    async with db.immediate() as conn:
        await conn.execute(
            insert(task_branch_origins).values(
                id="origin",
                task_id="child",
                repository_id="repo",
                parent_task_id="parent",
                parent_repository_id="repo",
                parent_ref="aq/parent",
                base_sha=base,
                creation_generation=7,
                reserved=True,
                materialized=True,
                created_at=1.0,
                materialized_at=1.0,
            )
        )
    await db.append_integration_review_evidence(
        {
            "id": "review-evidence",
            "source_task_id": "child",
            "repository_id": "repo",
            "source_base": base,
            "reviewed_head_sha": head,
            "reviewed_tree_sha": tree,
            "reviewer_task_id": "review-task",
            "reviewer_session_attempt_id": None,
            "review_kind": "leaf",
            "generation": 0,
            "verdict": "approved",
            "evidence": {"checks": ["focused"]},
            "created_at": 2.0,
        }
    )
    fence = await BranchOwnership(db).acquire(
        BranchKey(repository_id="repo", branch="aq/parent"),
        "collector",
        "collector",
    )
    request = PromotionInput(
        operation_key="activation",
        source_task_id="child",
        source_head=head,
        source_base=base,
        expected_target=base,
        fence=fence,
    )
    return {
        "origin": origin,
        "work": work,
        "base": base,
        "head": head,
        "tree": tree,
        "fence": fence,
        "request": request,
        "data_dir": tmp_path / "data",
    }


async def test_clean_promotion_is_retained_attributed_pushed_and_reconciled(db, promotion_case):
    from src.integration.promotion import PromotionService

    case = promotion_case
    service = PromotionService(db, data_dir=case["data_dir"], git_manager=GitManager())

    prepared = await service.prepare(case["request"])
    assert prepared.prepared_sha
    retained = next((case["data_dir"] / "integration-repositories").glob("*.git"))
    assert (
        _git(["rev-parse", f"refs/aq/integration-intents/{prepared.intent_id}"], retained)
        == prepared.prepared_sha
    )
    assert _git(["show", "-s", "--format=%P", prepared.prepared_sha], retained) == case["base"]
    assert _git(["show", "-s", "--format=%ae", prepared.prepared_sha], retained) == (
        "alice@example.test"
    )
    message = _git(["show", "-s", "--format=%B", prepared.prepared_sha], retained)
    assert f"AQ-Receipt: {prepared.receipt_id}" in message
    assert "Co-authored-by: Bob Builder <bob@example.test>" in message
    assert "Co-authored-by: Carol Coder <carol@example.test>" in message

    pushed = await service.push(prepared.intent_id, case["fence"])
    recovered = await service.reconcile(prepared.intent_id)
    again = await service.reconcile(prepared.intent_id)
    assert pushed == recovered == again
    assert recovered.receipt_id == prepared.receipt_id
    assert recovered.prepared_sha == prepared.prepared_sha
    assert (
        _git(["ls-remote", "--heads", "origin", "refs/heads/aq/parent"], case["work"]).split()[0]
        == prepared.prepared_sha
    )

    async with db._engine.connect() as conn:
        assert await conn.scalar(select(func.count()).select_from(task_delivery_receipts)) == 1
        assert await conn.scalar(select(func.count()).select_from(integration_outbox)) == 2


async def test_late_push_marker_cannot_regress_a_committed_intent(db, promotion_case):
    from src.integration.promotion import PromotionService

    case = promotion_case
    service = PromotionService(db, data_dir=case["data_dir"], git_manager=GitManager())
    prepared = await service.prepare(case["request"])
    await service.push(prepared.intent_id, case["fence"])

    await db.mark_integration_promotion_pushed(prepared.intent_id, {"remote": "late"})
    intent = await db.get_integration_promotion_intent(prepared.intent_id)
    await db.mark_integration_promotion_prepared(
        prepared.intent_id,
        prepared_sha=intent["prepared_sha"],
        recovery_ref=intent["recovery_ref"],
    )

    intent = await db.get_integration_promotion_intent(prepared.intent_id)
    assert intent["state"] == "committed"


class InjectedCrash(RuntimeError):
    pass


class CrashOnce:
    def __init__(self, phase: str):
        self.phase = phase
        self.seen = False

    def __call__(self, phase: str) -> None:
        if phase == self.phase and not self.seen:
            self.seen = True
            raise InjectedCrash(phase)


@pytest.mark.parametrize(
    "phase",
    [
        "after_object",
        "after_recovery_ref",
        "after_prepare",
        "before_push",
        "after_push",
        "before_outbox_ack",
    ],
)
async def test_crash_retries_make_one_squash_and_one_receipt(db, promotion_case, phase):
    from src.integration.promotion import PromotionService

    case = promotion_case
    crashing = PromotionService(
        db,
        data_dir=case["data_dir"],
        git_manager=GitManager(),
        crash_hook=CrashOnce(phase),
    )
    if phase in {"after_object", "after_recovery_ref", "after_prepare"}:
        with pytest.raises(InjectedCrash, match=phase):
            await crashing.prepare(case["request"])
        prepared = await PromotionService(
            db, data_dir=case["data_dir"], git_manager=GitManager()
        ).prepare(case["request"])
    else:
        prepared = await crashing.prepare(case["request"])

    if phase not in {"after_object", "after_recovery_ref", "after_prepare"}:
        with pytest.raises(InjectedCrash, match=phase):
            await crashing.push(prepared.intent_id, case["fence"])

    recovered_service = PromotionService(db, data_dir=case["data_dir"], git_manager=GitManager())
    if phase == "before_push":
        recovered = await recovered_service.push(prepared.intent_id, case["fence"])
    elif phase in {"after_object", "after_recovery_ref", "after_prepare"}:
        recovered = await recovered_service.push(prepared.intent_id, case["fence"])
    else:
        recovered = await recovered_service.reconcile(prepared.intent_id)
    again = await recovered_service.reconcile(prepared.intent_id)

    assert recovered == again
    remote_tip = _git(
        ["ls-remote", "--heads", "origin", "refs/heads/aq/parent"], case["work"]
    ).split()[0]
    audit = case["data_dir"] / "integration-repositories"
    retained = next(audit.glob("*.git"))
    assert _git(["rev-list", "--count", f"{case['base']}..{remote_tip}"], retained) == "1"
    async with db._engine.connect() as conn:
        assert await conn.scalar(select(func.count()).select_from(task_delivery_receipts)) == 1
        assert await conn.scalar(select(func.count()).select_from(integration_outbox)) == 2


async def test_conflict_records_inputs_and_never_creates_a_receipt(db, promotion_case):
    from src.integration.promotion import PromotionConflict, PromotionService

    case = promotion_case
    work = case["work"]
    (work / "shared.txt").write_text("child version\n")
    _git(["add", "shared.txt"], work)
    _git(["commit", "-m", "child conflict"], work)
    source = _git(["rev-parse", "HEAD"], work)
    source_tree = _git(["rev-parse", "HEAD^{tree}"], work)
    _git(["push", "origin", "aq/child"], work)

    _git(["switch", "-c", "aq/parent", case["base"]], work)
    (work / "shared.txt").write_text("parent version\n")
    _git(["commit", "-am", "parent conflict"], work)
    target = _git(["rev-parse", "HEAD"], work)
    _git(["push", "origin", "aq/parent"], work)
    await db.append_integration_review_evidence(
        {
            "id": "conflict-review",
            "source_task_id": "child",
            "repository_id": "repo",
            "source_base": case["base"],
            "reviewed_head_sha": source,
            "reviewed_tree_sha": source_tree,
            "reviewer_task_id": "review-task",
            "reviewer_session_attempt_id": None,
            "review_kind": "leaf",
            "generation": 0,
            "verdict": "approved",
            "evidence": {"checks": ["focused"]},
            "created_at": 3.0,
        }
    )
    request = case["request"].model_copy(update={"source_head": source, "expected_target": target})

    with pytest.raises(PromotionConflict) as caught:
        await PromotionService(db, data_dir=case["data_dir"], git_manager=GitManager()).prepare(
            request
        )

    intent = await db.get_integration_promotion_intent(caught.value.value.intent_id)
    # The identity is reserved before construction, but no delivery receipt
    # exists for a conflict.
    assert caught.value.value.receipt_id == intent["receipt_id"]
    assert intent["state"] == "conflict"
    assert intent["prepared_sha"] is None
    assert intent["conflict_diagnostics"]["base"] == case["base"]
    assert intent["conflict_diagnostics"]["source"] == source
    assert intent["conflict_diagnostics"]["target"] == target
    assert "shared.txt" in intent["conflict_diagnostics"]["paths"]
    async with db._engine.connect() as conn:
        assert await conn.scalar(select(func.count()).select_from(task_delivery_receipts)) == 0


async def test_moved_source_is_rejected_even_when_old_review_is_approved(db, promotion_case):
    from src.integration.promotion import PromotionService, PromotionSourceMoved

    case = promotion_case
    (case["work"] / "later.txt").write_text("moved\n")
    _git(["add", "later.txt"], case["work"])
    _git(["commit", "-m", "move source"], case["work"])
    _git(["push", "origin", "aq/child"], case["work"])

    with pytest.raises(PromotionSourceMoved, match="source branch moved"):
        await PromotionService(db, data_dir=case["data_dir"], git_manager=GitManager()).prepare(
            case["request"]
        )


async def test_divergent_target_blocks_push_and_reconcile(db, promotion_case):
    from src.integration.promotion import (
        PromotionInvariantError,
        PromotionService,
        PromotionTargetMoved,
    )

    case = promotion_case
    service = PromotionService(db, data_dir=case["data_dir"], git_manager=GitManager())
    prepared = await service.prepare(case["request"])
    _git(["switch", "-c", "competing", case["base"]], case["work"])
    (case["work"] / "competing.txt").write_text("other\n")
    _git(["add", "competing.txt"], case["work"])
    _git(["commit", "-m", "competing"], case["work"])
    _git(["push", "origin", "HEAD:refs/heads/aq/parent"], case["work"])

    with pytest.raises(PromotionTargetMoved):
        await service.push(prepared.intent_id, case["fence"])
    with pytest.raises(PromotionInvariantError, match="diverged"):
        await service.reconcile(prepared.intent_id)


async def test_concurrent_same_domain_reuses_deterministic_preparation(db, promotion_case):
    from src.integration.promotion import PromotionService

    case = promotion_case
    service = PromotionService(db, data_dir=case["data_dir"], git_manager=GitManager())
    first, second = await asyncio.gather(
        service.prepare(case["request"]),
        service.prepare(case["request"].model_copy(update={"operation_key": "replay"})),
    )
    assert first == second


async def test_retained_ref_survives_disposable_source_checkout_cleanup(db, promotion_case):
    from src.integration.promotion import PromotionService

    case = promotion_case
    service = PromotionService(db, data_dir=case["data_dir"], git_manager=GitManager())
    prepared = await service.prepare(case["request"])
    shutil.rmtree(case["work"])

    result = await service.push(prepared.intent_id, case["fence"])
    retained = next((case["data_dir"] / "integration-repositories").glob("*.git"))
    assert result.prepared_sha == _git(
        ["rev-parse", f"refs/aq/integration-intents/{prepared.intent_id}"], retained
    )


async def test_review_generation_comes_from_source_checkpoint_not_branch_origin(db, promotion_case):
    from src.database.tables import task_integration_checkpoints
    from src.integration.promotion import PromotionService, PromotionSourceMoved

    case = promotion_case
    async with db.immediate() as conn:
        await conn.execute(
            insert(task_integration_checkpoints).values(
                task_id="child",
                repository_id="repo",
                branch="aq/child",
                generation=1,
                state="working",
                version=1,
                updated_at=4.0,
            )
        )
    with pytest.raises(PromotionSourceMoved, match="review evidence"):
        await PromotionService(db, data_dir=case["data_dir"], git_manager=GitManager()).prepare(
            case["request"]
        )


async def test_repository_origin_change_blocks_a_prepared_push(db, promotion_case):
    from src.integration.promotion import PromotionInvariantError, PromotionService

    case = promotion_case
    service = PromotionService(db, data_dir=case["data_dir"], git_manager=GitManager())
    prepared = await service.prepare(case["request"])
    await db.update_repo("repo", url="/different/origin.git")

    with pytest.raises(PromotionInvariantError, match="repository identity changed"):
        await service.push(prepared.intent_id, case["fence"])


async def test_review_evidence_rows_are_append_only(db):
    from src.database.tables import integration_review_evidence
    from sqlalchemy import delete, update

    await db.append_integration_review_evidence(_review(evidence_id="immutable", generation=0))
    async with db.immediate() as conn:
        with pytest.raises((IntegrityError, DBAPIError)):
            await conn.execute(
                update(integration_review_evidence)
                .where(integration_review_evidence.c.id == "immutable")
                .values(verdict="rejected")
            )
    async with db.immediate() as conn:
        with pytest.raises((IntegrityError, DBAPIError)):
            await conn.execute(
                delete(integration_review_evidence).where(
                    integration_review_evidence.c.id == "immutable"
                )
            )


async def _seed_handler_delivery(handler) -> dict:
    from src.integration.models import PromotionValue

    await handler.db.create_project(Project(id="p", name="Project"))
    await handler.db.create_repo(
        RepoConfig(
            id="repo",
            project_id="p",
            source_type=RepoSourceType.CLONE,
            url="/configured/origin.git",
        )
    )
    await handler.db.create_task(
        Task(
            id="parent",
            project_id="p",
            title="Parent",
            description="",
            repo_id="repo",
            branch_name="aq/parent",
        )
    )
    await handler.db.create_task(
        Task(
            id="child",
            project_id="p",
            title="Child",
            description="",
            parent_task_id="parent",
            repo_id="repo",
            branch_name="aq/child",
        )
    )
    value = PromotionValue(intent_id="intent", receipt_id="receipt", prepared_sha="d" * 40)
    service = type("FakePromotion", (), {})()
    service.prepare = AsyncMock(return_value=value)
    service.push = AsyncMock(return_value=value)
    service.reconcile = AsyncMock(return_value=value)
    handler.orchestrator.promotion_service = service
    return {
        "operation_key": "activation",
        "source_task_id": "child",
        "source_head": "b" * 40,
        "source_base": "a" * 40,
        "expected_target": "c" * 40,
        "fence": {
            "target": {"repository_id": "repo", "branch": "aq/parent"},
            "owner_id": "collector",
            "token": 1,
        },
    }


async def test_local_handler_invokes_injected_promotion_service(command_handler_factory):
    handler = await command_handler_factory()
    args = await _seed_handler_delivery(handler)

    result = await handler.execute("delivery_promote", args)

    assert result == {
        "success": True,
        "outcome": "promoted",
        "intent_id": "intent",
        "receipt_id": "receipt",
        "prepared_sha": "d" * 40,
    }


async def test_session_cannot_promote_even_when_capability_audit_would_allow(
    command_handler_factory,
):
    from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
    from src.profiles.capabilities import CapabilityPolicy

    handler = await command_handler_factory()
    args = await _seed_handler_delivery(handler)
    handler.config.security.capability_enforcement = "audit"
    session = ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        policy=CapabilityPolicy.from_namespaces(
            aq_commands=["delivery_promote"], derived_from_legacy=True
        ),
        project_id="p",
        task_id="child",
        session_id="worker",
    )

    with principal_context(session):
        result = await handler.execute("delivery_promote", args)

    assert result["outcome"] == "unauthorized"
    handler.orchestrator.promotion_service.prepare.assert_not_awaited()


async def test_playbook_project_scope_cannot_be_mixed_with_another_promotion(
    command_handler_factory,
):
    from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
    from src.profiles.capabilities import CapabilityPolicy

    handler = await command_handler_factory()
    args = await _seed_handler_delivery(handler)
    await handler.db.create_project(Project(id="other", name="Other"))
    playbook = ExecutionPrincipal(
        kind=PrincipalKind.PLAYBOOK,
        policy=CapabilityPolicy.from_namespaces(aq_commands=["delivery_promote"]),
        project_id="other",
    )

    with principal_context(playbook):
        result = await handler.execute("delivery_promote", args)

    assert result["outcome"] == "unauthorized"
    handler.orchestrator.promotion_service.prepare.assert_not_awaited()
