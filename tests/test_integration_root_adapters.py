"""Root cutover races, restart/rollback, exact records and trusted publication."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert, select, update

from src.commands.contracts.integration import register_integration_contracts
from src.commands.contracts.registry import ContractRegistry
from src.commands.integration_commands import IntegrationCommandsMixin
from src.commands.principal import ExecutionPrincipal, current_principal, principal_context
from src.database import tables as t
from src.integration.engine import EngineRefused, RootEngineOwnership
from src.integration.models import BranchKey, Fence
from src.integration.root_adapters import RootPrimitiveAdapters
from src.integration.root_runtime import RootObserver, RootSubjectRuntime, root_runtime_for
from src.integration.subjects import (
    AdmissionPredicate,
    CIEvidence,
    CIObserveArgs,
    CIRequestArgs,
    CIState,
    CleanupArgs,
    Decision,
    EjectArgs,
    HeadIdentity,
    JournalMode,
    MemberRef,
    MergeMembersArgs,
    PolicyArtifactPin,
    Primitive,
    PrimitiveOutcome,
    PrimitivePorts,
    PublishArgs,
    ReceiptKind,
    RecordAttemptArgs,
    RecordDecisionArgs,
    RecordReceiptArgs,
    SealArgs,
    Subject,
    SubjectKind,
    SubjectPhase,
    SubjectSchedule,
    WriterBudget,
    WriterFileArgs,
    WriterLease,
    WriterRole,
    WriterStatus,
)
from src.playbooks.integration_policy import IntegrationPolicyFacts
from tests.pg_trigger_helpers import suspended_trigger
from tests.test_integration_main_promotion import (
    BASE,
    BRANCH,
    HEAD,
    FakeAppClient,
    PushGit,
    RootPromotionService,
)
from tests.test_integration_main_promotion import (
    prepared_db as prepared_fixture,
)

prepared_db = prepared_fixture

PIN = PolicyArtifactPin(playbook_id="root-test", artifact_sha256="sha256:" + "1" * 64)


@pytest.fixture
async def root(prepared_db):
    db, data_dir = prepared_db
    async with db.immediate() as conn:
        await conn.execute(
            insert(t.playbook_artifacts).values(
                artifact_sha256=PIN.artifact_sha256,
                playbook_id=PIN.playbook_id,
                source_digest="sha256:" + "2" * 64,
                contract_fingerprint="sha256:" + "3" * 64,
                compiler_build="test",
                path="/test/root.json",
                created_at=1,
            )
        )
    subject = Subject(
        id="subject",
        project_id="p",
        repository_id="repo",
        kind=SubjectKind.ROOT_BATCH,
        subject_key="root_batch:repo:request",
        policy=PIN,
        batch_id="batch",
        phase=SubjectPhase.PROMOTABLE,
        target_ref="refs/heads/main",
        head_sha=HEAD,
        base_sha=BASE,
        generation=0,
        schedule=SubjectSchedule.progress(now=10, max_wait_seconds=600),
        created_at=1,
        updated_at=1,
    )
    await db.ensure_integration_subject(subject.to_row())
    return db, data_dir, subject


async def activate(db, subject):
    await RootEngineOwnership(db, clock=lambda: 10).transfer(
        "repo",
        engine="reconciler",
        expected_versions={subject.id: subject.version},
        reason="scenario cutover",
        evidence=("shadow comparison", "real scenario", "operator approval"),
    )
    return Subject.from_row(await db.get_integration_subject(subject.id))


def facts(subject, *, ci=CIState.GREEN, writer=None, budget=None, holds=()):
    return IntegrationPolicyFacts(
        subject_id=subject.id,
        subject_version=subject.version,
        kind=subject.kind,
        phase=subject.phase,
        observed_at=10,
        head=subject.head,
        candidate=HeadIdentity(
            repository_id="repo", ref=BRANCH, sha=HEAD, generation=0, base_sha=BASE
        ),
        default_branch_head=BASE,
        ci=(
            CIEvidence(
                head_sha=HEAD, state=ci, evidence_id="ci-green", producer="404", observed_at=3
            ),
        ),
        writer=writer or subject.writer,
        budget=budget or subject.budget,
        holds=holds,
        publisher_fence=Fence(
            target=BranchKey(repository_id="repo", branch="refs/heads/main"),
            owner_id="root-reconciler:repo",
            token=1,
        ),
    )


async def prewrite(db, subject, request):
    proposed = Decision(
        subject_id=subject.id,
        subject_version=subject.version,
        policy=subject.policy,
        rule="test-policy",
        facts_digest=facts(subject).digest(),
        request=request,
    )
    await db.append_integration_subject_journal(
        {
            "subject_id": subject.id,
            "entry_kind": "decision",
            "mode": "active",
            "idempotency_key": f"test-decision:{subject.version}:{request.primitive}",
            "policy_artifact_sha256": PIN.artifact_sha256,
            "subject_version": subject.version,
            "phase": subject.phase.value,
            "head_sha": subject.head_sha,
            "generation": subject.generation,
            "primitive": request.primitive.value,
            "rule": proposed.rule,
            "facts_digest": proposed.facts_digest,
            "payload": {"decision": proposed.model_dump(mode="json")},
            "recorded_at": 10,
        }
    )


async def test_policy_ejection_admits_only_the_exact_task_local_command(root, monkeypatch):
    db, _, subject = root
    subject = await activate(db, subject)
    args = EjectArgs(member_task_id="root-0", reason="budget exhausted")
    await prewrite(db, subject, args)
    handler = IntegrationCommandsMixin()
    handler.db = db
    validated = []

    async def eject(batch_id, **kwargs):
        assert kwargs["operator_id"] == "service:root-reconciler"
        async with db.immediate() as conn:
            batch = await db.get_integration_batch(batch_id)
            validated.append(await kwargs["policy_ejection"].validate_on(
                db, conn, batch, task_id=kwargs["task_id"], reason=kwargs["reason"],
            ))
        return {"outcome": "ejected"}

    control = SimpleNamespace(eject=AsyncMock(side_effect=eject))
    from src.integration.gates import RootMemberEjection
    monkeypatch.setattr(RootMemberEjection, "eject", control.eject)

    async def execute(name, payload):
        assert name == "integration_eject"
        assert current_principal().describe() == "service:root-reconciler"
        for changed in (
            {"batch_id": "other"}, {"task_id": "root-1"}, {"reason": "other"},
        ):
            assert (await handler._cmd_integration_eject(payload | changed))["outcome"] == (
                "unauthorized"
            )
        # No operator controls, even with the active policy scope.
        assert (await handler._integration_operator_for_batch("batch"))[1] is not None
        inherited = await asyncio.create_task(handler._cmd_integration_eject(payload))
        assert inherited["outcome"] == "unauthorized"
        return await handler._cmd_integration_eject(payload)

    observer = SimpleNamespace(observe=AsyncMock(return_value=facts(subject)))
    ports = RootPrimitiveAdapters(db, SimpleNamespace(execute=execute), observer).bind(
        PrimitivePorts()
    )
    with principal_context(ExecutionPrincipal.service("outer-service")):
        result = await ports.invoke(subject, args)
        assert current_principal().describe() == "service:outer-service"
    assert result.outcome == "ejected"
    assert validated == [{
        "subject_id": subject.id, "subject_version": subject.version, "rule": "test-policy",
        "policy_artifact_sha256": PIN.artifact_sha256, "playbook_id": PIN.playbook_id,
        "decision_seq": (await db.list_integration_subject_journal(subject.id))[-1]["seq"],
        "facts_digest": facts(subject).digest(),
    }]
    control.eject.assert_awaited_once()
    for service in ("root-reconciler", "other-service"):
        with principal_context(ExecutionPrincipal.service(service)):
            result = await handler._cmd_integration_eject({
                "batch_id": "batch", "task_id": args.member_task_id, "reason": args.reason,
                "policy_decision": validated[0],
            })
            assert result["outcome"] == "unauthorized"
    assert control.eject.await_count == 1


@pytest.mark.parametrize("changed", ["version", "engine", "generation", "rule", "artifact"])
async def test_policy_ejection_rechecks_durable_authority_in_its_transaction(root, changed, monkeypatch):
    db, _, subject = root
    subject = await activate(db, subject)
    args = EjectArgs(member_task_id="root-0", reason="budget exhausted")
    await prewrite(db, subject, args)
    handler = IntegrationCommandsMixin()
    handler.db = db

    async def eject(batch_id, **kwargs):
        async with db.immediate() as conn:
            if changed in {"version", "engine"}:
                await conn.execute(update(t.integration_subjects).values(**{
                    changed: subject.version + 1 if changed == "version" else "legacy",
                }))
            elif changed == "generation":
                await conn.execute(update(t.integration_batches).values(current_revision=1))
            else:
                journal = (await conn.execute(select(t.integration_subject_journal).where(
                    t.integration_subject_journal.c.primitive == "eject",
                ))).mappings().one()
                payload = journal["payload"]
                if changed == "artifact":
                    payload["decision"]["policy"]["artifact_sha256"] = "sha256:" + "2" * 64
                async with suspended_trigger(
                    conn, table="integration_subject_journal",
                    name="integration_subject_journal_append_only",
                ):
                    await conn.execute(update(t.integration_subject_journal).where(
                        t.integration_subject_journal.c.seq == journal["seq"],
                    ).values(**({"rule": "other"} if changed == "rule" else {"payload": payload})))
            # Read on the same connection to see the uncommitted generation change.
            batch = (await conn.execute(select(t.integration_batches))).mappings().one()
            await kwargs["policy_ejection"].validate_on(
                db, conn, batch, task_id=kwargs["task_id"], reason=kwargs["reason"],
            )
        pytest.fail("changed policy authority must refuse before mutation/audit")

    from src.integration.gates import RootMemberEjection
    monkeypatch.setattr(RootMemberEjection, "eject", AsyncMock(side_effect=eject))

    async def execute(name, payload):
        return await handler._cmd_integration_eject(payload)

    ports = RootPrimitiveAdapters(
        db, SimpleNamespace(execute=execute),
        SimpleNamespace(observe=AsyncMock(return_value=facts(subject))),
    ).bind(PrimitivePorts())
    result = await ports.invoke(subject, args)
    assert result.outcome == "unknown"
    assert "changed" in result.reason or "mismatch" in result.reason
    async with db._engine.connect() as conn:
        assert (await conn.execute(select(t.events).where(
            t.events.c.event_type == "integration.batch_ejected",
        ))).first() is None


def publish_args(subject):
    return PublishArgs(fence=facts(subject).publisher_fence, expected_old_sha=BASE, new_sha=HEAD)




async def test_transfer_preview_is_read_only_and_requires_no_active_loop(root):
    db, _, subject = root
    before = await db.get_integration_subject(subject.id)
    result = await RootEngineOwnership(db).transfer(
        "repo", engine="reconciler", expected_versions={}, reason="", dry_run=True
    )
    assert result["outcome"] == "preview"
    assert result["expected_versions"] == {subject.id: subject.version}
    assert result["current_engines"] == {subject.id: "reconciler"}
    assert await db.get_integration_subject(subject.id) == before
    assert await db.list_integration_subject_journal(subject.id) == []


async def test_command_transfer_requires_operator_and_active_enablement_and_audits_principal(root):
    from src.commands.integration_commands import IntegrationCommandsMixin
    from src.commands.principal import ExecutionPrincipal, principal_context

    db, _, subject = root
    handler = IntegrationCommandsMixin()
    handler.db = db
    handler.config = SimpleNamespace(integration=SimpleNamespace(reconciler_active=False))
    with principal_context(ExecutionPrincipal.service("root loop")):
        result = await handler._cmd_integration_engine_transfer(
            {"repository_id": "repo", "engine": "reconciler"}
        )
    assert result["outcome"] == "unauthorized"
    assert (
        await handler._cmd_integration_engine_transfer(
            {"repository_id": "repo", "engine": "reconciler"}
        )
    )["outcome"] == "preview"
    args = {
        "repository_id": "repo",
        "engine": "reconciler",
        "dry_run": False,
        "expected_versions": {subject.id: subject.version},
        "reason": "approved",
        "evidence": ["shadow week", "scenarios", "approval"],
    }
    assert (await handler._cmd_integration_engine_transfer(args))["outcome"] == "refused"
    handler.config.integration.reconciler_active = True
    assert (await handler._cmd_integration_engine_transfer(args))["outcome"] == "transferred"
    journal = await db.list_integration_subject_journal(subject.id)
    assert journal[0]["payload"]["operator_id"] == "human:local-operator"


async def test_seal_preserves_the_complete_namespaced_request_key(root):
    db, _, subject = root
    exclusions = [{"task_id": "source", "reason": "source_ci_pending"}]
    commands = SimpleNamespace(execute=AsyncMock(return_value={
        "outcome": "empty", "exclusions": exclusions,
    }))
    adapters = RootPrimitiveAdapters(db, commands, None)
    subject = subject.model_copy(update={"subject_key": "root_batch:repo:integration-sweep:p:4"})
    result = await adapters.seal(subject, SealArgs(admission=AdmissionPredicate()))
    assert result.outcome == "empty"
    assert result.detail["exclusions"] == exclusions
    assert commands.execute.await_args.args[1]["request_id"] == "integration-sweep:p:4"


async def test_hosted_request_seeds_observation_before_the_next_policy_visit(root):
    db, _, subject = root
    commands = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                {"outcome": "already_built", "head_sha": HEAD, "revision": 0},
                {"outcome": "pending"},
            ]
        )
    )
    adapters = RootPrimitiveAdapters(db, commands, None)
    result = await adapters.request_ci(subject, CIRequestArgs(head=facts(subject).candidate))
    assert result.outcome == "already_running"
    assert [c.args[0] for c in commands.execute.await_args_list] == [
        "integration_build_candidate",
        "integration_ci_evidence",
    ]


@pytest.mark.parametrize(
    "changes",
    [
        {"intelligence_class": "wrong"},
        {"budget_seconds": 999},
        {"attempt_limit": 99},
        {"scope_members": ("root-0",)},
    ],
)
async def test_writer_preflight_never_starts_an_unrepresentable_contract(root, changes):
    from tests.test_integration_repair import _policy

    db, _, subject = root
    commands = SimpleNamespace(execute=AsyncMock())
    adapters = RootPrimitiveAdapters(db, commands, None)
    adapters._rows = AsyncMock(
        return_value=(
            None,
            None,
            [],
            {
                "id": "root-op",
                "policy_snapshot": _policy(),
            },
        )
    )
    boundary = _policy()["root"]
    values = dict(
        role=WriterRole.REPAIR,
        ordinal=0,
        intelligence_class=boundary["primary_intelligence_class"],
        budget_seconds=boundary["repair"]["primary_seconds"],
        attempt_limit=boundary["repair"]["primary_attempts"],
    )
    result = await adapters.file_writer(subject, WriterFileArgs(**{**values, **changes}))
    assert result.outcome == "configuration_blocked"
    commands.execute.assert_not_awaited()


async def test_existing_writer_replay_dispatches_without_restarting_its_clock(root):
    from tests.test_integration_repair import _policy

    db, _, subject = root
    policy = _policy()
    boundary = policy["root"]
    async with db.immediate() as conn:
        await conn.execute(
            update(t.integration_repair_stages)
            .where(t.integration_repair_stages.c.operation_id == "root-op")
            .values(
                policy=boundary["repair"],
                intelligence_class=boundary["primary_intelligence_class"],
                started_at=1,
                deadline_at=1 + boundary["repair"]["primary_seconds"],
            )
        )
    commands = SimpleNamespace(execute=AsyncMock(return_value={"repair_task_id": "repair"}))
    observed = facts(subject, writer=WriterLease(status=WriterStatus.FILED, task_id="repair"))
    adapters = RootPrimitiveAdapters(
        db, commands, SimpleNamespace(observe=AsyncMock(return_value=observed))
    )
    adapters._rows = AsyncMock(
        return_value=(
            None,
            None,
            [],
            {
                "id": "root-op",
                "policy_snapshot": policy,
            },
        )
    )
    args = WriterFileArgs(
        role=WriterRole.REPAIR,
        ordinal=0,
        intelligence_class=boundary["primary_intelligence_class"],
        budget_seconds=boundary["repair"]["primary_seconds"],
        attempt_limit=boundary["repair"]["primary_attempts"],
    )
    result = await adapters.file_writer(subject, args)
    assert result.outcome == "exists"
    assert commands.execute.await_args.args[0] == "integration_repair_dispatch"
    assert commands.execute.await_count == 1


async def test_cleanup_refuses_a_retention_contract_the_legacy_policy_cannot_honor(root):
    db, _, subject = root
    commands = SimpleNamespace(execute=AsyncMock())
    adapters = RootPrimitiveAdapters(db, commands, None)
    result = await adapters.cleanup(subject, CleanupArgs(retain_failed_seconds=1))
    assert result.outcome == "unknown"
    assert result.reason == "cleanup_contract_mismatch"
    commands.execute.assert_not_awaited()


@pytest.mark.parametrize(
    "cleanup,release,expected",
    [
        ("complete", "released", "clean"),
        ("already_complete", "already_released", "clean"),
        ("complete", "wait", "pending"),
        ("advanced", "released", "pending"),
        # Cleanup is best-effort: an answer the primitive cannot read still
        # releases the request and lease the next batch needs.
        ("invariant_error", "released", "pending"),
        ("conflict", "already_released", "pending"),
        ("complete", "invariant_error", "unknown"),
        ("invariant_error", "stale", "unknown"),
    ],
)
async def test_cleanup_releases_the_request_and_lease_before_closing(
    root, cleanup, release, expected
):
    db, _, subject = root
    commands = SimpleNamespace(
        execute=AsyncMock(side_effect=[{"outcome": cleanup}, {"outcome": release}])
    )
    result = await RootPrimitiveAdapters(db, commands, None).cleanup(subject, CleanupArgs())
    assert result.outcome == expected
    # Primitive 20 folds release in, independent of cleanup progress.
    assert [call.args[0] for call in commands.execute.await_args_list] == [
        "integration_cleanup",
        "integration_release",
    ]


async def test_an_unreadable_cleanup_answer_keeps_only_the_cleanup_pending(root):
    """A promoted batch whose cleanup count mismatches must still deliver.

    Returning ``unknown`` before release held the project lease and the sweep
    request behind delivered work, so no next batch could ever be sealed.
    """
    db, _, subject = root
    commands = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                {
                    "success": False,
                    "outcome": "invariant_error",
                    "item_count": 5,
                    "completed_count": 5,
                },
                {"success": True, "outcome": "released"},
            ]
        )
    )
    result = await RootPrimitiveAdapters(db, commands, None).cleanup(subject, CleanupArgs())
    assert result.outcome == "pending"
    assert result.detail == {"cleanup_outcome": "invariant_error"}
    assert [call.args[0] for call in commands.execute.await_args_list] == [
        "integration_cleanup",
        "integration_release",
    ]


async def test_shared_ci_registration_keeps_the_root_exclusion_and_prewrite(root):
    db, _, subject = root
    subject = await activate(db, subject)
    called = AsyncMock(
        return_value=PrimitiveOutcome(primitive=Primitive.CI_REQUEST, outcome="requested")
    )

    class SharedCI:
        def bind(self, ports):
            ports.bind(Primitive.CI_REQUEST, called)

    adapters = RootPrimitiveAdapters(
        db,
        None,
        SimpleNamespace(observe=AsyncMock(return_value=facts(subject))),
        ci_adapters=SharedCI(),
    )
    ports = adapters.bind(PrimitivePorts())
    args = CIRequestArgs(head=facts(subject).candidate)
    assert (await ports.invoke(subject, args)).reason == "decision_prewrite_missing"
    called.assert_not_awaited()
    await prewrite(db, subject, args)
    assert (await ports.invoke(subject, args)).outcome == "requested"
    called.assert_awaited_once_with(subject, args)


async def test_activation_waits_for_inflight_reconciler_operation(root):
    db, _, subject = root
    entered, release = asyncio.Event(), asyncio.Event()

    async def current_operation():
        async with RootEngineOwnership(db).operation("repo", subject=subject):
            entered.set()
            await release.wait()

    operation = asyncio.create_task(current_operation())
    await entered.wait()
    transfer = asyncio.create_task(activate(db, subject))
    await asyncio.sleep(0.03)
    assert not transfer.done()
    assert (await db.get_integration_subject(subject.id))["engine"] == "reconciler"
    release.set()
    await asyncio.wait_for(asyncio.gather(operation, transfer), 3)


async def test_preflights_share_guard_but_main_publishers_refuse_contention_and_release_on_cancel(
    root,
):
    db, _, subject = root
    entered, release = asyncio.Event(), asyncio.Event()

    async def publisher():
        async with RootEngineOwnership(db).operation("repo", subject=subject, publisher=True):
            entered.set()
            await release.wait()

    # Spawn outside the scope: a different publisher never inherits authority.
    first = asyncio.create_task(publisher())
    try:
        await asyncio.wait_for(entered.wait(), 30)
        async with RootEngineOwnership(db).operation("repo", subject=subject):
            pass  # read-only preflights may share the engine lock during a main push
        with pytest.raises(EngineRefused, match="another repository publisher"):
            async with RootEngineOwnership(db).operation("repo", subject=subject, publisher=True):
                pytest.fail("second publisher entered")
    finally:
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
    # Cancellation releases both session locks, including before a cutover.
    async with RootEngineOwnership(db).operation("repo", subject=subject, publisher=True):
        pass
    await asyncio.wait_for(activate(db, subject), 30)


async def test_shared_cleanup_serializes_with_transfer_without_granting_root_authority(root):
    from src.integration.development import publisher_exclusion

    db, _, subject = root
    entered, release = asyncio.Event(), asyncio.Event()

    async def development():
        async with publisher_exclusion(db, "repo"):
            entered.set()
            await release.wait()

    task = asyncio.create_task(development())
    await entered.wait()
    transfer = asyncio.create_task(activate(db, subject))
    await asyncio.sleep(0.03)
    assert not transfer.done()
    release.set()
    await asyncio.wait_for(asyncio.gather(task, transfer), 3)
    async with publisher_exclusion(db, "repo"):
        pass  # Cleanup remains independent of root mutation authority.
    with pytest.raises(EngineRefused, match="active reconciler subject"):
        async with RootEngineOwnership(db).operation("repo"):
            pytest.fail("cleanup granted root authority")


async def test_ambiguous_development_publication_requires_existing_recovery_before_cutover(root):
    from src.integration.development import DevelopmentPrimitives

    db, _, subject = root
    async with db.immediate() as conn:
        await conn.execute(
            DevelopmentPrimitives._operation_insert(
                id="dev-push",
                project_id="p",
                repository_id="repo",
                target_ref="refs/heads/main",
                state="publishing",
                created_at=1,
            )
        )
    with pytest.raises(EngineRefused, match="unresolved development publication"):
        await activate(db, subject)
    assert (await db.get_integration_subject(subject.id))["engine"] == "reconciler"


async def test_legacy_repair_observation_refusal_preserves_the_existing_closed_contract(root):
    from src.integration.repair import RepairService

    db, _, subject = root
    await activate(db, subject)
    result = await RepairService(db).record_result("root-op", "ci-green")
    assert {key: result[key] for key in ("outcome", "action", "attempts")} == {
        "outcome": "continue",
        "action": "stale",
        "attempts": 0,
    }
    assert (await RepairService(db).dispatch("root-op", 0))["outcome"] == "busy"
    async with db._engine.connect() as conn:
        assert (
            await conn.scalar(
                select(t.integration_repair_stages.c.attempts).where(
                    t.integration_repair_stages.c.operation_id == "root-op"
                )
            )
            == 0
        )




async def test_spawned_task_cannot_inherit_publisher_exclusion(root):
    db, _, subject = root
    subject = await activate(db, subject)
    async with RootEngineOwnership(db).operation("repo", subject=subject):

        async def escaped():
            async with RootEngineOwnership(db).operation("repo", subject=subject):
                pass

        with pytest.raises(EngineRefused, match="escaped"):
            await asyncio.create_task(escaped())


async def test_transfer_requires_complete_exact_versions_and_cutover_evidence(root):
    db, _, subject = root
    ownership = RootEngineOwnership(db)
    with pytest.raises(EngineRefused, match="evidence"):
        await ownership.transfer(
            "repo", engine="reconciler", expected_versions={"subject": 0}, reason="go"
        )
    with pytest.raises(EngineRefused, match="set/version"):
        await ownership.transfer(
            "repo",
            engine="reconciler",
            expected_versions={"subject": 99},
            reason="go",
            evidence=("approved",),
        )
    assert (await db.get_integration_subject(subject.id))["engine"] == "reconciler"


async def test_green_exact_publication_keeps_real_fence_and_prewrite(root):
    db, data_dir, subject = root
    subject = await activate(db, subject)
    app, calls = FakeAppClient(), []
    git = PushGit(app)
    service = RootPromotionService(
        db, data_dir=data_dir, git_manager=git, app_client=app, clock=lambda: 10
    )

    async def execute(name, payload):
        calls.append(name)
        journal = await db.list_integration_subject_journal(subject.id)
        assert any(j["entry_kind"] == "decision" for j in journal)
        assert any(j["outcome"] == "prewrite" for j in journal)
        result = await service.promote(payload["batch_id"], payload["revision"])
        return {"success": True, **result.model_dump(mode="json")}

    observer = SimpleNamespace(
        observe=AsyncMock(return_value=facts(subject)), observe_subject=AsyncMock()
    )
    ports = RootPrimitiveAdapters(db, SimpleNamespace(execute=execute), observer).bind(
        PrimitivePorts()
    )
    args = publish_args(subject)
    await prewrite(db, subject, args)
    result = await ports.invoke(subject, args)
    assert result.outcome == "published" and len(git.pushes) == 1 and app.remote == HEAD
    assert calls == ["integration_promote_main"]
    # A legacy reconciler racing/restarting after cutover cannot push again.
    assert (await service.promote("batch", 0)).outcome == "wait"
    assert len(git.pushes) == 1
    receipt = RecordReceiptArgs(
        kind=ReceiptKind.CODE,
        source_task_id="root-0",
        source_head_sha="c" * 40,
        target=HeadIdentity(
            repository_id="repo", ref=subject.target_ref, sha=HEAD, generation=0, base_sha=BASE
        ),
    )
    await prewrite(db, subject, receipt)
    assert (await ports.invoke(subject, receipt)).outcome == "recorded"
    assert (await ports.invoke(subject, receipt)).outcome == "exists"
    bad = receipt.model_copy(
        update={"target": receipt.target.model_copy(update={"base_sha": "f" * 40})}
    )
    await prewrite(db, subject, bad)
    # The same decision key is append-only: a different request cannot replace it.
    assert (await ports.invoke(subject, bad)).reason == "decision_prewrite_missing"
    assert len(await db.list_integration_subject_journal(subject.id, entry_kinds=["receipt"])) == 1


async def test_receipt_refuses_changed_base_even_before_publication(root):
    db, _, subject = root
    adapters = RootPrimitiveAdapters(db, None, None)
    args = RecordReceiptArgs(
        kind=ReceiptKind.CODE,
        source_task_id="root-0",
        source_head_sha="c" * 40,
        target=HeadIdentity(
            repository_id="repo", ref=subject.target_ref, sha=HEAD, generation=0, base_sha="f" * 40
        ),
    )
    assert (await adapters.receipt(subject, args)).reason == "receipt_identity_mismatch"
    assert not await db.list_integration_subject_journal(subject.id, entry_kinds=["receipt"])


async def test_record_decision_matches_the_exact_committed_rule_and_facts(root):
    db, _, subject = root
    subject = await activate(db, subject)
    await prewrite(db, subject, publish_args(subject))
    adapters = RootPrimitiveAdapters(db, None, None)
    args = RecordDecisionArgs(
        rule="test-policy",
        facts_digest=facts(subject).digest(),
        decided=Primitive.GIT_PUBLISH,
        mode=JournalMode.ACTIVE,
    )
    assert (await adapters.decision(subject, args)).outcome == "recorded"
    assert (
        await adapters.decision(subject, args.model_copy(update={"rule": "other"}))
    ).reason == "exact_decision_missing"


@pytest.mark.parametrize("state", [CIState.RED, CIState.INFRA, CIState.UNTRUSTED, CIState.PENDING])
async def test_no_non_green_head_can_publish(root, state):
    db, _, subject = root
    subject = await activate(db, subject)
    commands = SimpleNamespace(execute=AsyncMock())
    observer = SimpleNamespace(
        observe=AsyncMock(return_value=facts(subject, ci=state)), observe_subject=AsyncMock()
    )
    ports = RootPrimitiveAdapters(db, commands, observer).bind(PrimitivePorts())
    args = publish_args(subject)
    await prewrite(db, subject, args)
    result = await ports.invoke(subject, args)
    assert result.reason == "exact_trusted_green_required"
    commands.execute.assert_not_awaited()


@pytest.mark.parametrize(
    "changes",
    [
        {"new_sha": "f" * 40},
        {"expected_old_sha": "f" * 40},
        {"require_green": False},
        {
            "fence": Fence(
                target=BranchKey(repository_id="repo", branch=BRANCH), owner_id="root-op", token=7
            )
        },
    ],
)
async def test_publication_refuses_moved_identity_or_relabelled_candidate_fence(root, changes):
    db, _, subject = root
    subject = await activate(db, subject)
    observer = SimpleNamespace(
        observe=AsyncMock(return_value=facts(subject)), observe_subject=AsyncMock()
    )
    commands = SimpleNamespace(execute=AsyncMock())
    ports = RootPrimitiveAdapters(db, commands, observer).bind(PrimitivePorts())
    args = PublishArgs(**(publish_args(subject).model_dump() | changes))
    await prewrite(db, subject, args)
    result = await ports.invoke(subject, args)
    assert result.reason == "publication_identity_mismatch"
    commands.execute.assert_not_awaited()


async def test_ports_require_matching_committed_decision_before_remote_call(root):
    db, _, subject = root
    subject = await activate(db, subject)
    observer = SimpleNamespace(
        observe=AsyncMock(return_value=facts(subject)), observe_subject=AsyncMock()
    )
    commands = SimpleNamespace(execute=AsyncMock())
    result = (
        await RootPrimitiveAdapters(db, commands, observer)
        .bind(PrimitivePorts())
        .invoke(subject, publish_args(subject))
    )
    assert result.reason == "decision_prewrite_missing"
    commands.execute.assert_not_awaited()


@pytest.mark.parametrize(
    "state,classification,conclusion",
    [
        (CIState.INFRA, "conclusive", "cancelled"),
        (CIState.INFRA, "infra", "inconclusive"),
        (CIState.GREEN, "conclusive", "cancelled"),
        (CIState.UNTRUSTED, "conclusive", "success"),
    ],
)
async def test_cancelled_infra_and_untrusted_observations_are_not_attempts(
    root, state, classification, conclusion
):
    db, _, subject = root
    subject = await activate(db, subject)
    async with db.immediate() as conn:
        original = dict(
            (
                await conn.execute(
                    select(t.integration_check_evidence).where(
                        t.integration_check_evidence.c.id == "ci-green"
                    )
                )
            )
            .mappings()
            .one()
        )
        await conn.execute(
            insert(t.integration_check_evidence).values(
                **{
                    **original,
                    "id": "ci-test",
                    "run_id": "test-run",
                    "classification": classification,
                    "conclusion": conclusion,
                }
            )
        )
    budget = WriterBudget(
        ordinal=0,
        intelligence_class="primary",
        started_at=1,
        deadline_at=100,
        attempts=0,
        attempt_limit=3,
    )
    writer = WriterLease(
        status=WriterStatus.WORKING, task_id="repair", fence_token=2, last_push_at=3
    )
    observed = facts(subject, ci=state, writer=writer, budget=budget)
    observed = observed.model_copy(
        update={"ci": (observed.ci[0].model_copy(update={"evidence_id": "ci-test"}),)}
    )
    observer = SimpleNamespace(observe=AsyncMock(return_value=observed))
    adapters = RootPrimitiveAdapters(db, SimpleNamespace(execute=AsyncMock()), observer)
    head = facts(subject).candidate
    args = RecordAttemptArgs(head=head, ordinal=0, evidence_id="ci-test", conclusion="green")
    result = await adapters.attempt(subject, args)
    assert result.outcome == "not_an_attempt"
    assert not await db.list_integration_subject_journal(subject.id, entry_kinds=["attempt"])


def test_wiring_defaults_to_active_and_pausing_keeps_transfer_registered():
    from src.config import IntegrationConfig

    config = IntegrationConfig()
    assert config.reconciler_active and not config.reconciler_shadow
    config.reconciler_active = False
    assert root_runtime_for(SimpleNamespace(config=SimpleNamespace(integration=config))) is None
    registry = ContractRegistry()
    register_integration_contracts(registry)
    assert registry.get("integration_engine_transfer") is not None
    observer = SimpleNamespace(observe_subject=AsyncMock())
    ports = RootPrimitiveAdapters(None, None, observer).bind(PrimitivePorts())
    assert {Primitive.GIT_PUBLISH, Primitive.RECORD_ATTEMPT, Primitive.SEAL} <= ports.bound




async def test_runtime_seeds_the_next_request_with_durable_repository_ownership(root):
    db, _, subject = root
    await activate(db, subject)
    async with db.immediate() as conn:
        await conn.execute(
            update(t.projects)
            .where(t.projects.c.id == "p")
            .values(
                hierarchical_integration_policy={
                    "root": {"route": {"artifact": PIN.model_dump(mode="json")}}
                }
            )
        )
        await conn.execute(
            insert(t.project_integration_schedules).values(
                project_id="p",
                enabled=True,
                interval_seconds=60,
                next_due_at=20,
                outstanding_request_id="integration-sweep:p:2",
                outstanding_trigger="periodic",
                outstanding_requested_at=10,
                updated_at=10,
            )
        )
    policy = SimpleNamespace(policy_for=AsyncMock())
    runtime = RootSubjectRuntime(db, None, policy, PrimitivePorts())
    await runtime.seed(20)
    await runtime.seed(21)
    async with db._engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    select(t.integration_subjects).where(t.integration_subjects.c.id != subject.id)
                )
            )
            .mappings()
            .all()
        )
    assert len(rows) == 1
    new = Subject.from_row(rows[0])
    assert new.engine.value == "reconciler" and new.phase is SubjectPhase.ADMITTING
    assert new.subject_key == "root_batch:repo:integration-sweep:p:2" and new.policy == PIN


async def _outstanding(db, request_id):
    async with db.immediate() as conn:
        await conn.execute(
            update(t.project_integration_schedules)
            .where(t.project_integration_schedules.c.project_id == "p")
            .values(
                outstanding_request_id=request_id,
                outstanding_trigger=request_id and "periodic",
                outstanding_requested_at=request_id and 11,
                updated_at=11,
            )
        )


async def _schedule(db, request_id):
    async with db.immediate() as conn:
        await conn.execute(
            update(t.projects)
            .where(t.projects.c.id == "p")
            .values(
                hierarchical_integration_policy={
                    "root": {"route": {"artifact": PIN.model_dump(mode="json")}}
                }
            )
        )
        await conn.execute(
            insert(t.project_integration_schedules).values(
                project_id="p",
                enabled=True,
                interval_seconds=60,
                next_due_at=20,
                outstanding_request_id=request_id,
                outstanding_trigger="periodic",
                outstanding_requested_at=10,
                updated_at=10,
            )
        )


async def _root_subjects(db, *, besides):
    async with db._engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    select(t.integration_subjects).where(t.integration_subjects.c.id != besides)
                )
            )
            .mappings()
            .all()
        )
    return {row["subject_key"].rsplit(":", 1)[-1]: row for row in rows}


@pytest.mark.parametrize("path", ["restart", "replaced"])
async def test_runtime_supersedes_an_admitting_root_whose_request_is_no_longer_outstanding(
    root, path
):
    db, _, subject = root
    await activate(db, subject)
    await _schedule(db, "integration-sweep:p:2")
    runtime = RootSubjectRuntime(db, None, SimpleNamespace(policy_for=AsyncMock()), PrimitivePorts())
    await runtime.seed(20)
    stale = (await _root_subjects(db, besides=subject.id))["2"]
    if path == "restart":
        # The daemon released the stale request; nothing replaces it yet.
        await _outstanding(db, None)
        await runtime.seed(21)
        assert (await _root_subjects(db, besides=subject.id))["2"] == stale
    await _outstanding(db, "integration-sweep:p:3")
    await runtime.seed(22)
    await runtime.seed(23)

    rows = await _root_subjects(db, besides=subject.id)
    assert set(rows) == {"2", "3"}
    old, new = rows["2"], rows["3"]
    assert old["phase"] == "done" and old["next_due_at"] is None
    assert old["closed_reason"].startswith("superseded: ")
    assert "integration-sweep:p:3" in old["closed_reason"]
    assert old["version"] == stale["version"] + 1
    assert new["phase"] == "admitting" and new["engine"] == "reconciler"
    async with db._engine.connect() as conn:
        journal = (
            (
                await conn.execute(
                    select(t.integration_subject_journal).where(
                        t.integration_subject_journal.c.subject_id == old["id"]
                    )
                )
            )
            .mappings()
            .all()
        )
    assert [(row["primitive"], row["outcome"], row["phase"]) for row in journal] == [
        ("record_decision", "recorded", "admitting")
    ]
    assert journal[0]["payload"]["superseded_by"] == new["subject_key"]


async def test_runtime_never_supersedes_a_bound_admitting_root_and_never_raises(root):
    db, _, subject = root
    subject = await activate(db, subject)
    async with db.immediate() as conn:
        await db.update_integration_subject_on(
            conn,
            subject_id=subject.id,
            expected_version=subject.version,
            values={"phase": "admitting"},
            now=11,
        )
    await _schedule(db, "integration-sweep:p:2")
    runtime = RootSubjectRuntime(db, None, SimpleNamespace(policy_for=AsyncMock()), PrimitivePorts())
    await runtime.seed(20)
    row = await db.get_integration_subject(subject.id)
    assert row["phase"] == "admitting" and row["closed_reason"] is None
    assert await _root_subjects(db, besides=subject.id) == {}


async def test_runtime_recovers_a_lost_resolved_gate_wake(root):
    db, _, subject = root
    subject = await activate(db, subject)
    async with db.immediate() as conn:
        await conn.execute(
            insert(t.gates).values(
                id="root-gate",
                project_id="p",
                gate_type="human",
                title="Continue?",
                status="resolved",
                resolution="continue",
                created_at=1,
            )
        )
        await db.update_integration_subject_on(
            conn,
            subject_id=subject.id,
            expected_version=subject.version,
            values={"gate_id": "root-gate", "next_due_at": None},
            now=11,
        )
    runtime = RootSubjectRuntime(db, None, None, PrimitivePorts())
    await runtime.tick(20)
    row = await db.get_integration_subject(subject.id)
    assert row["next_due_at"] == 20 and row["gate_id"] == "root-gate"


@pytest.mark.parametrize("state", [CIState.GREEN, CIState.RED])
async def test_builder_head_ci_does_not_count_against_a_just_claimed_writer(root, state):
    from tests.test_integration_observe import (
        builder_mutation,
        claimed_candidate_writer,
        observe,
        with_rows,
    )

    db, _, subject = root
    subject = await activate(db, subject)
    data = with_rows(
        claimed_candidate_writer(),
        integration_candidate_ref_mutations=[builder_mutation()],
    )
    observed = await observe(data)
    assert observed.writer.status is WriterStatus.CLAIMED
    observer = SimpleNamespace(
        observe=AsyncMock(
            return_value=facts(subject, ci=state, writer=observed.writer, budget=observed.budget)
        )
    )
    commands = SimpleNamespace(
        execute=AsyncMock(return_value={"success": True, "outcome": state.value})
    )
    adapters = RootPrimitiveAdapters(db, commands, observer)
    head = facts(subject).candidate
    result = await adapters.observe_ci(subject, CIObserveArgs(head=head))
    assert result.outcome == state.value
    assert result.detail["subject_values"] == {}
    result = await adapters.attempt(
        subject,
        RecordAttemptArgs(
            head=head, ordinal=observed.budget.ordinal, evidence_id="ci-green", conclusion=state.value
        ),
    )
    assert result.outcome == "not_an_attempt"
    assert not await db.list_integration_subject_journal(subject.id, entry_kinds=["attempt"])


async def test_counted_attempt_replays_once_and_survives_uncommitted_projection(root):
    db, _, subject = root
    subject = await activate(db, subject)
    budget = WriterBudget(
        ordinal=0,
        intelligence_class="primary",
        started_at=1,
        deadline_at=500,
        attempts=0,
        attempt_limit=3,
    )
    writer = WriterLease(
        status=WriterStatus.WORKING, task_id="repair", fence_token=2, last_push_at=3
    )
    observer = SimpleNamespace(
        observe=AsyncMock(return_value=facts(subject, writer=writer, budget=budget))
    )
    adapters = RootPrimitiveAdapters(db, SimpleNamespace(execute=AsyncMock()), observer)
    args = RecordAttemptArgs(
        head=facts(subject).candidate, ordinal=0, evidence_id="ci-green", conclusion="green"
    )
    assert (await adapters.attempt(subject, args)).outcome == "counted"
    replay = await adapters.attempt(subject, args)
    assert (
        replay.outcome == "not_an_attempt"
        and replay.detail["subject_values"]["budget_attempts"] == 1
    )
    assert len(await db.list_integration_subject_journal(subject.id, entry_kinds=["attempt"])) == 1
    # The append committed but the subject CAS did not: the real observer must
    # recover the count from the journal rather than increment/replay it.
    async with db.immediate() as conn:
        await conn.execute(
            update(t.integration_repair_stages)
            .where(t.integration_repair_stages.c.operation_id == "root-op")
            .values(started_at=1)
        )
    observed = await RootObserver(db, None).observe(subject)
    assert observed.budget.attempts == 1
    assert (await db.get_integration_subject(subject.id))["budget_attempts"] == 0


async def _writer_facts(db, subject, status=WriterStatus.UNKNOWN):
    observed = facts(subject, writer=WriterLease(status=status, task_id="writer", fence_token=6))
    observed = observed.model_copy(update={"unknown": ("writer_stop_unproven:writer",)})
    return await RootObserver(db, None)._writer_facts(subject, observed)


async def _writer_task(db, status, *, session_state=None):
    async with db.immediate() as conn:
        await conn.execute(
            insert(t.tasks).values(
                id="writer", project_id="p", title="writer", description="", status=status,
                created_at=1, updated_at=1,
            )
        )
    if session_state:
        from src.models import SessionRecord

        await db.create_session(
            SessionRecord(
                id="writer-session", task_id="writer", project_id="p", profile_id="worker",
                harness="fake", provider="fake", name="writer-session", lifecycle="pool",
                state=session_state, work_dir="/w", epoch="e", instance_token="writer-token",
                started_at=5,
            )
        )


async def test_writer_holding_its_fenced_ref_is_never_projected(root):
    db, _, subject = root
    subject = await activate(db, subject)
    await _writer_task(db, "FAILED")
    async with db.immediate() as conn:
        await conn.execute(
            update(t.integration_branch_owners)
            .where(t.integration_branch_owners.c.id == "branch-owner-row")
            .values(owner_id="writer", owner_role="repair")
        )
    projected = await _writer_facts(db, subject)
    assert projected.writer.status is WriterStatus.UNKNOWN
    assert projected.unknown == ("writer_stop_unproven:writer",)


async def test_accepted_handoff_stops_the_writer_whose_fence_moved_to_the_collector(root):
    db, _, subject = root
    subject = await activate(db, subject)
    await _writer_task(db, "IN_PROGRESS", session_state="running")
    async with db.immediate() as conn:
        await conn.execute(
            insert(t.workspaces).values(
                id="w", project_id="p", workspace_path="/w", source_type="link", created_at=1
            )
        )
        await conn.execute(
            insert(t.integration_candidate_resolutions).values(
                id="accepted", batch_id="batch", revision=0, member_ordinal=0,
                operation_id="root-op", operation_episode_id="batch", stage_ordinal=0,
                stage_deadline_at=500, project_id="p", repair_task_id="writer",
                repair_session_id="writer-session", repair_session_instance_token="writer-token",
                repair_workspace_id="w", repair_workspace_path="/w", repository_id="repo",
                branch=BRANCH, target_branch=BRANCH + "-resolution", target_kind="qualified",
                fence_owner_id="writer", fence_token=6, handoff_owner_id="root-op",
                handoff_fence_token=7, partial_head_sha=BASE, source_base_sha=BASE,
                source_head_sha="c" * 40, resolved_head_sha=HEAD, resolved_tree_sha="e" * 40,
                repair_commit_shas=[HEAD], push_evidence={"remote_sha": HEAD}, state="accepted",
                created_at=8, updated_at=9,
            )
        )
    projected = await _writer_facts(db, subject, WriterStatus.WORKING)
    assert projected.writer.status is WriterStatus.STOPPED and projected.unknown == ()
    proof = projected.writer.stop_proof["stop_proof"]
    assert (proof["kind"], proof["successor_owner_id"], proof["successor_fence_token"]) == (
        "accepted_handoff",
        "root-op",
        7,
    )
    # A handoff to an owner that no longer holds the ref is not proof.
    async with db.immediate() as conn:
        await conn.execute(
            update(t.integration_branch_owners)
            .where(t.integration_branch_owners.c.id == "branch-owner-row")
            .values(owner_id="someone-else")
        )
    assert (await _writer_facts(db, subject)).writer.status is WriterStatus.UNKNOWN


@pytest.mark.parametrize("session", ["writer-session", "older-session"])
async def test_close_receipt_stops_only_the_latest_stopped_writer_session(root, session):
    db, _, subject = root
    subject = await activate(db, subject)
    await _writer_task(db, "COMPLETED", session_state="stopped")
    async with db.immediate() as conn:
        await conn.execute(
            insert(t.integration_outbox).values(
                id="closed", dedup_key="closed", project_id="p",
                event_type="integration.repair_delegate_closed",
                payload={"task_id": "writer", "session_id": session,
                         "instance_token": "writer-token", "fence_token": 6},
                available_at=9, created_at=9,
            )
        )
    projected = await _writer_facts(db, subject)
    if session == "writer-session":
        assert projected.writer.status is WriterStatus.STOPPED
        assert projected.writer.stop_proof["stop_proof"]["kind"] == "delegate_close"
    else:
        assert projected.writer.status is WriterStatus.UNKNOWN


async def test_never_claimed_retired_and_consumed_writers_are_no_longer_the_subjects(root):
    db, _, subject = root
    subject = await activate(db, subject)
    await _writer_task(db, "FAILED")
    projected = await _writer_facts(db, subject)
    assert projected.writer == WriterLease() and projected.unknown == ()
    # A writer that was ever claimed may hold unpushed work: stop proof only.
    async with db.immediate() as conn:
        await conn.execute(update(t.tasks).where(t.tasks.c.id == "writer").values(claim_epoch=1))
    projected = await _writer_facts(db, subject)
    assert projected.writer.status is WriterStatus.UNKNOWN and projected.unknown
    async with db.immediate() as conn:
        await conn.execute(
            update(t.tasks).where(t.tasks.c.id == "writer").values(status="IN_PROGRESS")
        )
    assert (await _writer_facts(db, subject)).writer.task_id == "writer"
    stopped = {"writer": {"task_id": "writer"}, "writer_status": "stopped"}
    for kind, outcome in (("decision", None), ("action", "merged")):
        await db.append_integration_subject_journal(
            {
                "subject_id": subject.id, "entry_kind": kind, "mode": "active",
                "idempotency_key": f"consumed:{kind}", "policy_artifact_sha256": PIN.artifact_sha256,
                "subject_version": subject.version, "phase": subject.phase.value,
                "head_sha": subject.head_sha, "generation": subject.generation,
                "primitive": "git_merge_members", "rule": "writer-stopped",
                "facts_digest": "sha256:" + "4" * 64, "outcome": outcome,
                "payload": {"facts": stopped} if kind == "decision" else {"result": {}},
                "recorded_at": 10,
            }
        )
    assert (await _writer_facts(db, subject)).writer == WriterLease()


async def test_rebuild_conflict_answers_conflict_and_adopts_its_generation(root):
    """Live 2026-10-04 d3fb5c5b: the rebuild filed the repair it reported as stale.

    Rebuilding a candidate whose base is no longer main supersedes the subject's
    revision, and a conflict in that merge is a repair the service dispatched
    itself.  The command can only report the revision change, so the adapter
    reads the filed member conflict and answers ``conflict``.
    """
    db, _, subject = root
    subject = await activate(db, subject)
    moved, rebuilt = "8" * 40, "9" * 40

    async def execute(name, args):
        async with db.immediate() as conn:
            await conn.execute(
                update(t.integration_batches)
                .where(t.integration_batches.c.id == "batch")
                .values(current_revision=1, lifecycle="repairing")
            )
            await conn.execute(
                insert(t.integration_candidate_revisions).values(
                    batch_id="batch", revision=1, construction_base_sha=moved,
                    next_member_ordinal=1, head_sha=rebuilt, state="constructing",
                    created_at=4.0, updated_at=4.0,
                )
            )
            await conn.execute(
                insert(t.integration_candidate_member_results).values(
                    batch_id="batch", revision=1, member_ordinal=0,
                    input_head_sha="c" * 40, input_tree_sha="e" * 40,
                    result="conflict", created_at=4.0, updated_at=4.0,
                )
            )
        return {
            "success": False,
            "outcome": "stale_revision",
            "error": "candidate revision changed during build",
        }

    observed = facts(subject).model_copy(update={"default_branch_head": moved})
    adapters = RootPrimitiveAdapters(
        db,
        SimpleNamespace(execute=AsyncMock(side_effect=execute)),
        SimpleNamespace(observe=AsyncMock(return_value=observed)),
    )
    args = MergeMembersArgs(
        target_ref=subject.target_ref,
        base_sha=moved,
        members=(
            MemberRef(task_id="root-0", head_sha="c" * 40, base_sha=BASE),
            MemberRef(task_id="root-1", head_sha="d" * 40, base_sha=BASE),
        ),
    )
    result = await adapters.merge(subject, args)
    assert result.outcome == "conflict"
    assert result.detail["member"] == "root-0"
    assert result.detail["files"] == []
    # The subject adopts the revision the rebuild left behind; a stale
    # generation is unknown on every later visit.
    assert result.detail["subject_values"] == {
        "head_sha": rebuilt,
        "base_sha": moved,
        "generation": 1,
    }


async def test_a_rebuild_that_filed_no_repair_still_answers_unknown(root):
    """Only a filed repair is a conflict; a bare revision change is not."""
    db, _, subject = root
    subject = await activate(db, subject)
    moved = "8" * 40
    commands = SimpleNamespace(
        execute=AsyncMock(return_value={
            "success": False,
            "outcome": "stale_revision",
            "error": "candidate revision changed during build",
        })
    )
    observed = facts(subject).model_copy(update={"default_branch_head": moved})
    adapters = RootPrimitiveAdapters(
        db, commands, SimpleNamespace(observe=AsyncMock(return_value=observed))
    )
    args = MergeMembersArgs(
        target_ref=subject.target_ref,
        base_sha=moved,
        members=(
            MemberRef(task_id="root-0", head_sha="c" * 40, base_sha=BASE),
            MemberRef(task_id="root-1", head_sha="d" * 40, base_sha=BASE),
        ),
    )
    result = await adapters.merge(subject, args)
    assert result.outcome == "unknown"
    assert result.reason == "candidate revision changed during build"


async def test_superseded_head_is_not_an_attempt(root):
    db, _, subject = root
    subject = await activate(db, subject)
    observer = SimpleNamespace(observe=AsyncMock(return_value=facts(subject)))
    adapters = RootPrimitiveAdapters(db, None, observer)
    args = RecordAttemptArgs(
        head=facts(subject).candidate.model_copy(update={"sha": "f" * 40}),
        ordinal=0,
        evidence_id="ci-green",
        conclusion="green",
    )
    assert (await adapters.attempt(subject, args)).outcome == "stale"
    assert not await db.list_integration_subject_journal(subject.id, entry_kinds=["attempt"])




async def test_service_owns_root_runtime_remote_pass_and_shutdown():
    from src.integration.service import IntegrationService

    calls, entered, release = [], asyncio.Event(), asyncio.Event()

    async def tick(now):
        calls.append(now)
        entered.set()
        await release.wait()

    runtime = SimpleNamespace(tick=tick, stop=AsyncMock())
    db = SimpleNamespace(
        **{
            name: AsyncMock(return_value=[])
            for name in (
                "due_integration_schedule_page",
                "due_integration_repair_stage_page",
                "pending_candidate_ci_page",
                "unresolved_integration_intent_page",
                "pending_integration_cleanup_page",
            )
        }
    )
    service = IntegrationService(
        db,
        SimpleNamespace(dispatch_due=AsyncMock()),
        subject_runtime=runtime,
        clock=lambda: 10,
    )
    await service.tick(10, background=True)
    await entered.wait()
    await service.tick(11, background=True)
    assert calls == [10]
    release.set()
    await service._reconciliation_task
    await service.stop()
    runtime.stop.assert_awaited_once()


async def test_policy_activation_uses_generation_cas_and_preserves_subject_pin(root):
    from src.integration.records import PolicyActivation
    from tests.test_integration_sealing import _policy

    db, _, subject = root
    activation = PolicyActivation(db)
    policy = _policy()
    for boundary in (policy["parent"], policy["root"]):
        boundary.pop("primary_profile_id", None)
        boundary.pop("verifier_profile_id", None)
        boundary["repair"].pop("debug_profile_id", None)
    response = await activation.configure(
        "p", updates={"hierarchical_integration_policy": policy},
        expected_generation=0, reason="reviewed next policy", operator_id="operator",
    )
    assert response["outcome"] == "configured" and response["generation"] == 1
    assert Subject.from_row(await db.get_integration_subject(subject.id)) == subject
    stale = await activation.configure(
        "p", updates={"hierarchical_integration_mode": "disabled"},
        expected_generation=0, reason="stale preview", operator_id="operator",
    )
    assert stale["outcome"] == "stale" and stale["generation"] == 1
    assert (await db.get_project("p")).hierarchical_integration_mode == "train"


async def test_policy_activation_refuses_repository_rebinding_with_a_live_subject(root):
    from src.integration.records import PolicyActivation
    from src.models import RepoConfig, RepoSourceType

    db, _, subject = root
    await db.create_repo(RepoConfig(id="other", project_id="p", source_type=RepoSourceType.CLONE,
                                   url="https://github.com/acme/other.git", default_branch="main"))
    result = await PolicyActivation(db).configure(
        "p", updates={"integration_repository_id": "other"},
        expected_generation=0, reason="change repository", operator_id="operator",
    )
    assert result["outcome"] == "busy"
    assert (await db.get_project("p")).integration_repository_id == "repo"
    assert Subject.from_row(await db.get_integration_subject(subject.id)) == subject


async def test_development_policy_activation_accepts_scoped_reviewed_route(root):
    from src.integration.development_runtime import project_development_pin
    from src.integration.records import PolicyActivation
    from tests.test_integration_sealing import _policy

    db, _, subject = root
    route = _policy()["root"]["route"]
    policy = {"validation": "none", "development": {"route": route}}
    result = await PolicyActivation(db).configure(
        "p", updates={"hierarchical_integration_mode": "development",
                      "hierarchical_integration_policy": policy},
        expected_generation=0, reason="reviewed development policy", operator_id="operator",
    )
    assert result["outcome"] == "configured"
    project = await db.get_project("p")
    pin = project_development_pin({"hierarchical_integration_policy":
                                  project.hierarchical_integration_policy})
    assert pin.artifact_sha256 == route["artifact"]["artifact_sha256"]
    assert Subject.from_row(await db.get_integration_subject(subject.id)) == subject


async def test_status_and_doctor_project_recorded_subject_facts_without_advancing(root, monkeypatch):
    from src.doctor.integration_checks import run_check
    from src.doctor.models import Severity
    from src.integration.status import IntegrationStatusService

    db, _, subject = root
    import importlib
    checks = importlib.import_module("src.doctor.integration_checks")
    monkeypatch.setattr(checks.time, "time", lambda: 1000)
    async with db.immediate() as conn:
        await conn.execute(update(t.integration_subjects).where(
            t.integration_subjects.c.id == subject.id).values(next_due_at=1, due_set_at=1))
    before = await db.get_integration_subject(subject.id)
    status = await IntegrationStatusService(db, delivery=AsyncMock()).status("p")
    assert status["projection_kind"] == "subjects"
    assert status["subjects"][0]["id"] == subject.id
    overdue = await run_check(db, "integration.subjects_overdue")
    assert overdue.severity == Severity.WARN
    assert [row["id"] for row in overdue.data["subjects"]] == [subject.id]
    assert await db.get_integration_subject(subject.id) == before
    async with db.immediate() as conn:
        await conn.execute(update(t.integration_subjects).where(
            t.integration_subjects.c.id == subject.id).values(
                next_due_at=None, wait_reason=None, gate_id="held-gate"))
    held_before = await db.get_integration_subject(subject.id)
    held = await run_check(db, "integration.subjects_held")
    assert held.severity == Severity.WARN
    assert held.data["subjects"][0]["gate_id"] == "held-gate"
    assert await db.get_integration_subject(subject.id) == held_before


async def test_development_configuration_binds_local_project_repository(reuse_database, tmp_path):
    from src.integration.records import PolicyActivation
    from src.models import Project

    db = await reuse_database("local-development-activation.db")
    remote = str(tmp_path / "remote.git")
    await db.create_project(Project(id="local", name="local", repo_url=remote,
                                   repo_default_branch="main"))
    result = await PolicyActivation(db).configure(
        "local", updates={"hierarchical_integration_mode": "development",
                          "integration_repository": {"id": "local-repo", "url": remote,
                                                     "default_branch": "main"},
                          "hierarchical_integration_policy": {"validation": "none"}},
        expected_generation=0, reason="configure disposable local project", operator_id="operator",
    )
    assert result["outcome"] == "configured"
    repository = await db.get_repo("local-repo")
    assert repository.project_id == "local" and repository.url == remote
    assert (await db.get_project("local")).integration_repository_id == "local-repo"


async def test_policy_activation_refuses_in_place_repository_change_with_live_subject(root):
    from src.integration.records import PolicyActivation

    db, _, subject = root
    await db.update_project("p", repo_url="https://github.com/acme/replacement.git",
                            repo_default_branch="release")
    before = await db.get_repo("repo")
    result = await PolicyActivation(db).configure(
        "p", updates={"integration_repository": {
            "id": "repo", "url": "https://github.com/acme/replacement.git",
            "default_branch": "release",
        }}, expected_generation=0, reason="retarget repository", operator_id="operator",
    )
    assert result["outcome"] == "busy"
    assert await db.get_repo("repo") == before
    assert Subject.from_row(await db.get_integration_subject(subject.id)) == subject
async def test_root_ancestry_uses_retained_objects_and_falls_back_before_construction():
    from src.integration.root_runtime import RootGitObservationReader
    from unittest.mock import AsyncMock

    git = SimpleNamespace(ais_ancestor=AsyncMock(side_effect=[True, None, False]))
    reader = RootGitObservationReader(git, lambda repository_id: "/retained/" + repository_id)
    repository = {"id": "repo", "checkout_base_path": "/base"}
    assert await reader.is_ancestor(repository, BASE, HEAD) is True
    assert await reader.is_ancestor(repository, HEAD, BASE) is False
    assert [call.args[0] for call in git.ais_ancestor.await_args_list] == [
        "/retained/repo", "/retained/repo", "/base"]


async def test_root_remote_reads_use_retained_repository_when_base_is_unavailable(tmp_path):
    from src.git.manager import RemoteRefResult, RemoteRefState
    from src.integration.root_runtime import RootGitObservationReader
    from unittest.mock import AsyncMock

    store = tmp_path / "repo.git"
    store.mkdir()
    git = SimpleNamespace(als_remote_ref=AsyncMock(return_value=RemoteRefResult(
        RemoteRefState.PRESENT, oid=HEAD
    )))
    reader = RootGitObservationReader(git, lambda _: store)
    repository = {"id": "repo", "checkout_base_path": "/unavailable", "url": "repo-url"}
    assert (await reader.remote_head(repository, "refs/heads/main")).sha == HEAD
    git.als_remote_ref.assert_awaited_once_with(str(store), "main", repository_url="repo-url")
    store.rmdir()
    await reader.remote_head(repository, "refs/heads/main")
    assert git.als_remote_ref.await_args.args[0] == "/unavailable"


@pytest.mark.parametrize('sealed', [False, True])
async def test_only_unsealed_root_defers_member_ancestry_unknown_to_admission(root, monkeypatch, sealed):
    from src.integration.observe import IntegrationObserver
    from src.integration.subjects import MemberFacts

    db, _, subject = root
    if not sealed:
        subject = subject.model_copy(update={'batch_id': None})
    observed = facts(subject).model_copy(update={
        'members': (MemberFacts(task_id='delivered', head_sha=HEAD, ancestry='contained'),
                    MemberFacts(task_id='new', head_sha=BASE, ancestry='unknown')),
        'unknown': ('ancestry_unknown:new', 'remote_unknown:refs/heads/main'),
    })
    monkeypatch.setattr(IntegrationObserver, 'observe', AsyncMock(return_value=observed))
    result = await RootObserver(db, None).observe(subject)
    if sealed:
        assert result.members == observed.members
        assert result.unknown == observed.unknown
    else:
        assert [m.task_id for m in result.members] == ['new']
        assert result.unknown == ('remote_unknown:refs/heads/main',)


async def test_root_remote_heads_batch_the_retained_repository_then_the_base(tmp_path):
    from src.git.manager import RemoteRefResult, RemoteRefState
    from src.integration.root_runtime import RootGitObservationReader

    store = tmp_path / "repo.git"
    store.mkdir()

    async def read(path, branches, *, repository_url):
        if path == str(store):
            return {
                branch: RemoteRefResult(RemoteRefState.PRESENT, oid=HEAD)
                if branch == "main"
                else RemoteRefResult(RemoteRefState.ERROR, error="unreadable")
                for branch in branches
            }
        return {branch: RemoteRefResult(RemoteRefState.ABSENT) for branch in branches}

    git = SimpleNamespace(als_remote_refs=AsyncMock(side_effect=read))
    reader = RootGitObservationReader(git, lambda _: store)
    repository = {"id": "repo", "checkout_base_path": "/base", "url": "repo-url"}
    heads = await reader.remote_heads(repository, ["refs/heads/aq/x", "refs/heads/main"])
    assert heads["refs/heads/main"].sha == HEAD and heads["refs/heads/aq/x"].state == "absent"
    assert [call.args for call in git.als_remote_refs.await_args_list] == [
        (str(store), ["aq/x", "main"]),
        ("/base", ["aq/x"]),
    ]


@pytest.mark.parametrize("previous", (
    (), ("same",), ("same", "same"), ("different", "same"), ("merged", "same"),
    ("same", "blocked"), ("same", "other_generation"),
))
async def test_merge_reports_named_blocker_after_three_identical_mutation_refusals(root, previous):
    from src.integration.root_adapters import CANDIDATE_MUTATION_BLOCKER

    db, _, subject = root
    reason = "candidate mutation identity changed"
    commands = SimpleNamespace(execute=AsyncMock(return_value={"outcome": "wait", "reason": reason}))
    adapters = RootPrimitiveAdapters(
        db, commands, SimpleNamespace(observe=AsyncMock(return_value=facts(subject)))
    )
    async with db._engine.connect() as conn:
        members = (await conn.execute(select(t.integration_batch_members).where(
            t.integration_batch_members.c.batch_id == subject.batch_id,
        ).order_by(t.integration_batch_members.c.ordinal))).mappings().all()
    args = SimpleNamespace(
        primitive=Primitive.GIT_MERGE_MEMBERS, target_ref=subject.target_ref,
        regenerate_generated=True, base_sha=BASE,
        members=tuple(SimpleNamespace(
            task_id=m["task_id"], head_sha=m["reviewed_head_sha"], base_sha=m["source_base_sha"]
        ) for m in members),
    )
    for index, prior in enumerate(previous):
        result = PrimitiveOutcome.unknown(
            args.primitive, reason if prior == "same" else "different refusal"
        ).model_dump(mode="json")
        if prior == "merged":
            result = {"outcome": "merged"}
        elif prior == "blocked":
            result = PrimitiveOutcome.unknown(
                args.primitive, CANDIDATE_MUTATION_BLOCKER, original_reason=reason,
                blocker=CANDIDATE_MUTATION_BLOCKER, refusals=3,
            ).model_dump(mode="json")
        elif prior == "other_generation":
            result = PrimitiveOutcome.unknown(args.primitive, reason).model_dump(mode="json")
        await db.append_integration_subject_journal({
            "subject_id": subject.id, "entry_kind": "action", "mode": "active",
            "idempotency_key": f"mutation-refusal:{index}",
            "policy_artifact_sha256": PIN.artifact_sha256,
            "subject_version": subject.version, "phase": subject.phase.value,
            "head_sha": subject.head_sha,
            "generation": subject.generation + 1 if prior == "other_generation" else
            subject.generation,
            "primitive": args.primitive.value, "outcome": result["outcome"],
            "payload": {"result": result}, "recorded_at": 10 + index,
        })
    answer = await adapters.merge(subject, args)
    assert answer.is_unknown
    if previous in {("same", "same"), ("same", "blocked")}:
        assert answer.reason == CANDIDATE_MUTATION_BLOCKER
        assert answer.detail == {
            "blocker": CANDIDATE_MUTATION_BLOCKER, "original_reason": reason, "refusals": 3,
        }
    else:
        assert answer.reason == reason


async def test_named_mutation_blocker_is_visible_in_subject_schedule(root):
    from src.integration.reconciler import VisitTransition
    from src.integration.root_adapters import CANDIDATE_MUTATION_BLOCKER
    from src.integration.root_runtime import PinnedRootPolicy

    _, _, subject = root
    expected = SubjectSchedule.backoff(
        now=100, max_wait_seconds=600, refusal_streak=2,
        base_seconds=30, ceiling_seconds=600,
    )
    policy = PinnedRootPolicy(None)
    policy.policies[PIN.artifact_sha256] = SimpleNamespace(
        settle=AsyncMock(return_value=VisitTransition(schedule=expected))
    )
    refusal = PrimitiveOutcome.unknown(
        Primitive.GIT_MERGE_MEMBERS, CANDIDATE_MUTATION_BLOCKER,
        blocker=CANDIDATE_MUTATION_BLOCKER, original_reason="candidate mutation identity changed",
    )
    transition = await policy.settle(subject, None, refusal, now=100)
    assert transition.schedule.wait_reason == CANDIDATE_MUTATION_BLOCKER
    assert transition.schedule.next_due_at == expected.next_due_at
    assert transition.schedule.refusal_streak == expected.refusal_streak
    assert transition.schedule.gate_id is None
