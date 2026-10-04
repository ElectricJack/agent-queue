"""Parent visits: lost events, exact receipt refresh, recovery and exclusive engines."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert, update

from src.commands.integration_commands import IntegrationCommandsMixin
from src.commands.gate_commands import GateCommandsMixin
from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
from src.database import tables as t
from src.integration.collection import CollectionService
from src.integration.child_delivery import ChildDelivery
from src.integration.engine import EngineRefused
from src.integration.parent_adapters import (
    REOPEN_REFUSED_META_KEY,
    ParentPolicyFacts,
    ParentPrimitiveAdapters,
)
from src.integration.parent_engine import ParentEngineOwnership
from src.integration.parent_ci import ParentCIService
from src.integration.promotion import PromotionService
from src.integration.parent_runtime import (
    ParentSubjectRuntime,
    ParentVisitObserver,
    PinnedParentPolicy,
    ensure_parent_subject_on,
)
from src.integration.parent_subjects import ParentDatabaseObservationReader, ParentSubjectAdapter
from src.integration.models import PromotionInput
from src.integration.repair import RepairService
from src.integration.subjects import (
    PolicyArtifactPin,
    Subject,
)
from src.models import Project, Task, TaskStatus
from src.git.manager import GitManager
from src.playbooks.definition import load_definition_json
from src.playbooks.integration_policy import IntegrationPolicy
from src.profiles.capabilities import DENY_ALL
from src.integration.writers import WriterPrimitives
from tests.test_integration_cancelled_collection import (  # noqa: F401
    _failed_aggregate,
    _held_red_aggregate,
    _rows,
    _commit_on,
    _git,
    _promote_next,
    case as case_fixture,
)
from tests.test_integration_parent_completion import _parent_tree, _code_receipt


case = case_fixture


def definition():
    return load_definition_json(
        Path("tests/fixtures/playbooks/v2/parent-integration/artifact.json").read_text()
    )


class Commands(IntegrationCommandsMixin, GateCommandsMixin):
    """Real command bodies with deterministic, explicitly authorized test dispatch."""

    def __init__(self, db, hierarchy, promotion=None):
        self.db, self.hierarchy, self.promotion = db, hierarchy, promotion
        self.orchestrator = SimpleNamespace(parent_subject_runtime=None)
        self.calls = []

    async def execute(self, name, args):
        self.calls.append(name)
        return await getattr(self, "_cmd_" + name)(args)

    async def _integration_delivery_authorized(self, *args, **kwargs):
        return True

    def _hierarchy_integration_service(self):
        return self.hierarchy

    def _integration_promotion_service(self):
        return self.promotion

    def _integration_repair_service(self):
        return SimpleNamespace(dispatch=AsyncMock())


async def setup(
    db, hierarchy, *, task_id="parent", promotion=None, parent_ci=None, active=True, shadow=False
):
    artifact = definition()
    pin = PolicyArtifactPin(playbook_id=artifact.id, artifact_sha256=artifact.artifact_sha256())
    async with db.immediate() as conn:
        await conn.execute(
            insert(t.playbook_artifacts).values(
                artifact_sha256=pin.artifact_sha256,
                playbook_id=pin.playbook_id,
                source_digest="sha256:" + "1" * 64,
                contract_fingerprint="sha256:" + "2" * 64,
                compiler_build="test",
                path="/test/parent.json",
                created_at=1,
            )
        )
        subject, _ = await ParentSubjectAdapter(db, clock=lambda: 1000).ensure_on(
            conn,
            task_id,
            policy=pin,
            max_wait_seconds=3600,
        )
    if active:
        await ParentEngineOwnership(db, clock=lambda: 1000).transfer(
            "repo",
            task_id=task_id,
            engine="reconciler",
            expected_versions={subject.id: subject.version},
            reason="scenario cutover",
            evidence=("shadow", "scenario", "approval"),
        )
        subject = Subject.from_row(await db.get_integration_subject(subject.id))
    return runtime_env(
        db, hierarchy, subject, promotion=promotion, parent_ci=parent_ci, active=active, shadow=shadow
    )


def runtime_env(db, hierarchy, subject, *, promotion=None, parent_ci=None, active=True, shadow=False):
    """Build fresh process-local components without creating or adopting a subject."""
    observer = ParentVisitObserver(
        ParentDatabaseObservationReader(db), clock=lambda: 1000, facts_type=ParentPolicyFacts
    )
    commands = Commands(db, hierarchy, promotion)
    adapters = ParentPrimitiveAdapters(
        db, commands, observer, parent_ci=parent_ci, clock=lambda: 1000
    )
    policy = PinnedParentPolicy(lambda _: definition())
    runtime = ParentSubjectRuntime(
        db,
        observer,
        policy,
        adapters,
        lambda _: definition(),
        active=active,
        shadow=shadow,
        clock=lambda: 1000,
    )
    commands.orchestrator.parent_subject_runtime = runtime
    return SimpleNamespace(
        db=db,
        subject=subject,
        observer=observer,
        commands=commands,
        adapters=adapters,
        policy=policy,
        loop=runtime.loops[0],
        runtime=runtime,
    )


async def restart(env, hierarchy, *, promotion=None, parent_ci=None, active=True, shadow=False):
    await env.runtime.stop()
    subject = Subject.from_row(await env.db.get_integration_subject(env.subject.id))
    return runtime_env(
        env.db, hierarchy, subject, promotion=promotion, parent_ci=parent_ci,
        active=active, shadow=shadow,
    )


async def visit(env):
    async with env.db.immediate() as conn:
        row = await env.db.lock_integration_subject_on(conn, env.subject.id)
        if row["next_due_at"] is None:
            return
        await env.db.update_integration_subject_on(
            conn,
            subject_id=row["id"],
            expected_version=row["version"],
            values={"next_due_at": 1000, "due_set_at": 1000},
            now=1000,
        )
    await env.loop.visit(env.subject.id)


@pytest.fixture
async def db(reuse_database):
    db = await reuse_database("parent-reconciler")
    await db.create_project(Project(id="p", name="parent reconciler"))
    return db


async def test_eight_child_failure_and_conflict_reach_one_gate_without_operator_commands(db):
    hierarchy, _, children = await _parent_tree(db, children=8)
    async with db.immediate() as conn:
        await conn.execute(
            update(t.tasks).where(t.tasks.c.id == children[2]).values(status="FAILED")
        )
        await conn.execute(
            insert(t.integration_promotion_intents).values(
                id="conflict",
                domain_key="conflict",
                receipt_id="unwritten",
                project_id="p",
                source_task_id=children[5],
                target_task_id="parent",
                repository_id="repo",
                target_branch="aq/parent",
                source_head="b" * 40,
                source_base="a" * 40,
                expected_target="a" * 40,
                fence_owner_id="collector",
                fence_token=1,
                state="conflict",
                conflict_diagnostics={"files": ["shared.py"]},
                created_at=20,
                updated_at=20,
            )
        )
    env = await setup(db, hierarchy)
    await visit(env)
    for _ in range(3):
        await env.loop.tick(1000)
    row = await db.get_integration_subject(env.subject.id)
    assert row["gate_id"] and row["next_due_at"] is None
    gates = await _rows(db, t.gates)
    assert len(gates) == 1 and gates[0]["status"] == "open"
    assert env.commands.calls == ["integration_parent_action"]
    assert await _rows(db, t.integration_repair_stages) == []


async def test_real_eight_child_adoption_restart_and_rollback_preserve_gate_and_receipts(
    case, record_property
):
    checkpoint = await case.db.get_integration_checkpoint("epic")
    extra = await case.hierarchy.file_children(
        "epic", [{"title": f"pending child {i}"} for i in range(4, 9)], checkpoint["generation"]
    )
    assert len(extra["children"]) == 5
    assert await _promote_next(case, 1000)
    assert await _promote_next(case, 1001) is None  # real shared.txt merge conflict
    async with case.db.immediate() as conn:
        await conn.execute(update(t.tasks).where(t.tasks.c.id == "epic.3").values(status="FAILED"))
    original_receipts = await _rows(case.db, t.task_delivery_receipts)
    original_intents = await _rows(case.db, t.integration_promotion_intents)
    assert len(original_receipts) == 1
    assert {r["state"] for r in original_intents} == {"committed", "conflict"}
    env = await setup(
        case.db, case.hierarchy, task_id="epic", promotion=case.promotion, active=False, shadow=True
    )
    facts = await env.observer.observe(env.subject)
    assert len(facts.children) == 8 and facts.failed_child_count == 1
    assert facts.conflicts[0].member_task_id == "epic.2"
    assert facts.conflicts[0].files == ("shared.txt",)
    await visit(env)
    assert env.commands.calls == []
    shadow = await case.db.list_integration_subject_journal(env.subject.id)
    assert any(r["entry_kind"] == "decision" and r["mode"] == "shadow" for r in shadow)
    owner = ParentEngineOwnership(case.db, clock=lambda: 1000)
    preview = await owner.transfer(
        "repo", task_id="epic", engine="reconciler", expected_versions={}, reason="", dry_run=True
    )
    await owner.transfer(
        "repo", task_id="epic", engine="reconciler",
        expected_versions=preview["expected_versions"], reason="disposable scenario adoption",
        evidence=("fixture-shadow-journal", "fixture-eight-child-scenario", "fixture-approval"),
        operator_id="fixture-operator",
    )
    env = await restart(env, case.hierarchy, promotion=case.promotion)
    for _ in range(3):
        await visit(env)
    row = await case.db.get_integration_subject(env.subject.id)
    gate_id = row["gate_id"]
    assert gate_id and row["next_due_at"] is None
    assert env.commands.calls == ["integration_parent_action"]
    assert not await _rows(case.db, t.integration_repair_stages)
    answered = await env.commands.execute(
        "gate_resolve", {"gate_id": gate_id, "resolution": "hold", "resolved_by": "fixture"}
    )
    assert answered["success"], answered
    env = await restart(env, case.hierarchy, promotion=case.promotion)
    for _ in range(3):
        await visit(env)
    assert env.commands.calls == []
    assert len(await _rows(case.db, t.gates)) == 1
    assert (await case.db.get_integration_subject(env.subject.id))["gate_id"] == gate_id
    assert await _rows(case.db, t.task_delivery_receipts) == original_receipts
    assert await _rows(case.db, t.integration_promotion_intents) == original_intents
    # Feature-off stops visits but cannot release durable ownership to legacy.
    await env.runtime.stop()
    legacy = CollectionService(case.db, hierarchy_service_factory=lambda: case.hierarchy)
    assert (await legacy.collect_parent("epic", 1000))["outcome"] == "waiting"
    preview = await owner.transfer(
        "repo", task_id="epic", engine="legacy", expected_versions={}, reason="", dry_run=True
    )
    await owner.transfer(
        "repo", task_id="epic", engine="legacy",
        expected_versions=preview["expected_versions"], reason="disposable feature-off rollback",
        operator_id="fixture-operator",
    )
    with pytest.raises(EngineRefused, match="gate"):
        async with owner.operation("epic"):
            pass
    row = await case.db.get_integration_subject(env.subject.id)
    assert row["engine"] == "legacy" and row["gate_id"] == gate_id
    assert row["parent_episode_id"] == original_receipts[0]["parent_episode_id"]
    assert await _rows(case.db, t.task_delivery_receipts) == original_receipts
    assert await _rows(case.db, t.integration_promotion_intents) == original_intents
    transfers = [
        r for r in await case.db.list_integration_subject_journal(env.subject.id)
        if r["payload"].get("command") == "integration_engine_transfer"
    ]
    assert [(r["payload"]["from"], r["payload"]["to"]) for r in transfers] == [
        ("legacy", "reconciler"), ("reconciler", "legacy")
    ]
    assert all(r["payload"]["operator_id"] == "fixture-operator" for r in transfers)
    assert _git(["rev-parse", "refs/heads/aq/epic"], case.origin) == original_receipts[0]["after_sha"]
    assert _git(["rev-parse", "refs/heads/main"], case.origin) == case.base
    record_property("rollout_evidence", json.dumps({
        "scenario": "eight_children_failed_child_real_conflict_gate_restart_rollback",
        "environment": "disposable PostgreSQL and local bare Git; not production",
        "policy_artifact_sha256": env.subject.policy.artifact_sha256,
        "base_sha": case.base,
        "child_heads": case.heads,
        "receipts": original_receipts,
        "intents": original_intents,
        "gate": (await _rows(case.db, t.gates))[0],
        "shadow_decisions": [r for r in shadow if r["entry_kind"] == "decision"],
        "transfers": transfers,
        "final_subject": row,
    }, sort_keys=True))


async def test_stale_rollback_refuses_then_fresh_legacy_service_resumes_same_episode(case):
    env = await setup(case.db, case.hierarchy, task_id="epic", promotion=case.promotion)
    await visit(env)  # first real child is delivered by the adopted subject
    original_receipts = await _rows(case.db, t.task_delivery_receipts)
    assert len(original_receipts) == 1
    owner = ParentEngineOwnership(case.db, clock=lambda: 1000)
    preview = await owner.transfer(
        "repo", task_id="epic", engine="legacy", expected_versions={}, reason="", dry_run=True
    )
    await visit(env)  # refresh receipt-derived head, advancing the exact version
    before = await case.db.get_integration_subject(env.subject.id)
    with pytest.raises(EngineRefused, match="version changed"):
        await owner.transfer(
            "repo", task_id="epic", engine="legacy",
            expected_versions=preview["expected_versions"], reason="stale rollback",
        )
    assert await case.db.get_integration_subject(env.subject.id) == before
    await env.runtime.stop()
    preview = await owner.transfer(
        "repo", task_id="epic", engine="legacy", expected_versions={}, reason="", dry_run=True
    )
    await owner.transfer(
        "repo", task_id="epic", engine="legacy",
        expected_versions=preview["expected_versions"], reason="fresh rollback",
    )
    assert await _promote_next(case, 1000) is None  # legacy collection resumes and sees the conflict
    assert await _rows(case.db, t.task_delivery_receipts) == original_receipts
    checkpoint = await case.db.get_integration_checkpoint("epic")
    assert checkpoint["episode_id"] == env.subject.parent_episode_id
    assert _git(["rev-parse", "refs/heads/aq/epic"], case.origin) == original_receipts[0]["after_sha"]


async def test_receipt_head_refresh_can_file_and_lease_one_unified_verifier(db):
    hierarchy, _, children = await _parent_tree(db, children=1)
    env = await setup(db, hierarchy)
    await _code_receipt(db, children[0], "a" * 40, "d" * 40)
    await visit(env)  # aggregate moves independently of checkpoint
    row = await db.get_integration_subject(env.subject.id)
    assert row["head_sha"] == "d" * 40
    assert (await db.get_integration_checkpoint("parent"))["checkpoint_sha"] == "a" * 40
    facts = await env.observer.observe(Subject.from_row(row))
    assert not facts.identity_moved and facts.readiness == "ready"
    await visit(env)
    row = await db.get_integration_subject(env.subject.id)
    writer_id = row["writer_task_id"]
    assert writer_id and writer_id.startswith("writer-")
    assert (await db.get_task(writer_id)).status.value == "BLOCKED"
    await visit(env)
    row = await db.get_integration_subject(env.subject.id)
    assert row["writer_fence_token"] is not None
    assert (await db.get_task(writer_id)).status.value == "READY"
    checkpoint = await db.get_integration_checkpoint("parent")
    assert checkpoint["state"] == "verifying" and checkpoint["checkpoint_sha"] == "d" * 40
    writers = [r for r in await _rows(db, t.tasks) if r["created_by_kind"] == "integration_writer"]
    assert len(writers) == 1


async def test_interrupted_verifier_filing_replays_same_ordinal_and_links_before_lease(db):
    hierarchy, _, children = await _parent_tree(db, children=1)
    env = await setup(db, hierarchy)
    await _code_receipt(db, children[0], "a" * 40, "d" * 40)
    await visit(env)
    subject = Subject.from_row(await db.get_integration_subject(env.subject.id))
    facts = await env.observer.observe(subject)
    decision = await env.policy.decide(subject, facts)
    filed = await WriterPrimitives(db, clock=lambda: 1000).file(subject, decision.request)
    assert filed.outcome == "filed"
    operation = await db.get_active_parent_integration_operation("parent")
    assert operation["verifier_task_id"] is None
    env = await restart(env, hierarchy)
    await visit(env)
    operation = await db.get_active_parent_integration_operation("parent")
    assert operation["verifier_task_id"] == filed.detail["task_id"]
    await visit(env)
    assert (await db.get_task(filed.detail["task_id"])).status.value == "READY"
    writers = [r for r in await _rows(db, t.tasks) if r["created_by_kind"] == "integration_writer"]
    assert len(writers) == 1


@pytest.mark.parametrize("remote_written", [False, True])
async def test_prepared_parent_intent_resumes_by_readback_without_new_merge(case, remote_written):
    env = await setup(case.db, case.hierarchy, task_id="epic", promotion=case.promotion)
    facts = await env.observer.observe(env.subject)
    member = facts.collection_members[0]
    await ChildDelivery(case.db, case.promotion).ensure_evidence(member.task_id, 1000)
    request = PromotionInput(
        operation_key=facts.parent_operation_id,
        source_task_id=member.task_id,
        source_head=member.head_sha,
        source_base=member.base_sha,
        expected_target=env.subject.head_sha,
        fence=facts.collector_fence,
    )
    async with ParentEngineOwnership(case.db).operation("epic", subject=env.subject):
        prepared = await case.promotion.prepare(request)
        if remote_written:

            async def crash(phase):
                if phase == "after_push":
                    raise RuntimeError("simulated response loss")

            case.promotion.crash_hook = crash
            with pytest.raises(RuntimeError, match="response loss"):
                await case.promotion.push(prepared.intent_id, request.fence)
            case.promotion.crash_hook = None
    assert not await _rows(case.db, t.task_delivery_receipts)
    # Reconstruct the service and runtime after the interrupted publication.
    # No pending write identity is carried in process-local memory.
    promotion = PromotionService(
        case.db, data_dir=case.promotion.data_dir, git_manager=GitManager()
    )
    env = await restart(env, case.hierarchy, promotion=promotion)
    await visit(env)
    [receipt] = await _rows(case.db, t.task_delivery_receipts)
    assert receipt["after_sha"] == prepared.prepared_sha
    [intent] = await _rows(case.db, t.integration_promotion_intents)
    assert intent["state"] == "committed" and intent["id"] == prepared.intent_id
    assert receipt["parent_episode_id"] == env.subject.parent_episode_id
    assert receipt["reviewed_head_sha"] == member.head_sha
    assert receipt["before_sha"] == request.expected_target
    assert _git(["rev-parse", "refs/heads/aq/epic"], case.origin) == prepared.prepared_sha
    assert _git(["rev-parse", "refs/heads/main"], case.origin) == case.base
    original = await _rows(case.db, t.task_delivery_receipts)
    current = Subject.from_row(await case.db.get_integration_subject(env.subject.id))
    async with ParentEngineOwnership(case.db).operation("epic", subject=current):
        await promotion.reconcile(prepared.intent_id)
    assert await _rows(case.db, t.task_delivery_receipts) == original
    assert env.commands.calls == ["integration_parent_action"]
    journal = await case.db.list_integration_subject_journal(env.subject.id)
    assert any(r["rule"] == "resume-publication" for r in journal)


async def test_stale_stage13_dossier_gates_without_dispatch_or_unchanged_head_expiry(db):
    hierarchy, checkpoint, _ = await _parent_tree(db, children=1)
    operation_id = checkpoint["operation_id"]
    async with db.immediate() as conn:
        await conn.execute(
            update(t.integration_repair_operations)
            .where(t.integration_repair_operations.c.id == operation_id)
            .values(active_stage=13)
        )
        await conn.execute(
            insert(t.integration_repair_stages).values(
                operation_id=operation_id,
                ordinal=13,
                policy={"on_exhausted": "continue"},
                starting_sha="a" * 40,
                state="active",
                deadline_at=10,
                started_at=1,
                attempts=0,
                dossier={"no_progress": "unchanged-head repeated failure"},
            )
        )
    env = await setup(db, hierarchy)
    await visit(env)
    stages = await _rows(db, t.integration_repair_stages)
    repair = RepairService(db)
    assert (await repair.expire(operation_id, 13, now=1000))["outcome"] == "stale"
    assert (await repair.dispatch(operation_id, 13))["outcome"] == "stale"
    ignored = await repair.record_result(operation_id, "missing", now=1000)
    assert ignored["outcome"] == "continue" and ignored["action"] == "stale"
    assert await _rows(db, t.integration_repair_stages) == stages
    assert len(await _rows(db, t.gates)) == 1
    assert (await db.get_integration_subject(env.subject.id))["gate_id"]


async def test_human_gate_hold_is_durable_and_agent_cannot_spoof_resolution(db):
    hierarchy, _, children = await _parent_tree(db, children=1)
    async with db.immediate() as conn:
        await conn.execute(
            update(t.tasks).where(t.tasks.c.id == children[0]).values(status="FAILED")
        )
    env = await setup(db, hierarchy)
    await visit(env)
    gate_id = (await db.get_integration_subject(env.subject.id))["gate_id"]
    args = {"gate_id": gate_id, "resolved_by": "human:spoof", "resolution": "hold"}
    with principal_context(ExecutionPrincipal(kind=PrincipalKind.SERVICE, policy=DENY_ALL)):
        rejected = await env.commands.execute("gate_resolve", args)
    assert not rejected["success"]
    answer = await env.commands.execute("gate_resolve", args)
    assert answer["success"]
    # The failure can disappear without clearing the human's hold.
    async with db.immediate() as conn:
        await conn.execute(
            update(t.tasks).where(t.tasks.c.id == children[0]).values(status="COMPLETED")
        )
    await visit(env)
    await visit(env)
    row = await db.get_integration_subject(env.subject.id)
    assert row["gate_id"] == gate_id and row["writer_task_id"] is None
    assert len(await _rows(db, t.gates)) == 1
    journal = await db.list_integration_subject_journal(env.subject.id)
    assert any(r["payload"].get("answered_by") == "human:local-operator" for r in journal)


@pytest.mark.parametrize(
    "evidence_head,conclusion", [("d", "success"), ("e", "success"), ("d", "failure")]
)
async def test_ci_visit_completes_only_exact_trusted_green(db, evidence_head, conclusion):
    hierarchy, checkpoint, children = await _parent_tree(db, children=1)
    ci = SimpleNamespace(
        handle=AsyncMock(return_value={"outcome": "green", "evidence_ids": ["ci"]})
    )
    env = await setup(db, hierarchy, parent_ci=ci)
    await _code_receipt(db, children[0], "a" * 40, "d" * 40)
    for _ in range(3):
        await visit(env)  # head refresh, file, lease
    async with db.immediate() as conn:
        await conn.execute(
            insert(t.integration_check_evidence).values(
                id="ci",
                operation_id=checkpoint["operation_id"],
                parent_task_id="parent",
                parent_generation=1,
                parent_head_sha=evidence_head * 40,
                producer_id="forge-observer",
                workflow_id="workflow",
                run_id="run",
                attempt=1,
                required_check_version="parent-v1",
                checks={"unit": conclusion},
                conclusion=conclusion,
                classification="conclusive",
                observed_at=900,
            )
        )
    await visit(env)
    await visit(env)
    if evidence_head == "d" and conclusion == "success":
        assert (await db.get_task("parent")).status.value == "COMPLETED"
        assert (await db.get_integration_subject(env.subject.id))["phase"] == "done"
        assert ci.handle.await_args.args[0]["head_sha"] == "d" * 40
    else:
        assert (await db.get_task("parent")).status.value != "COMPLETED"
        assert not await _rows(db, t.integration_parent_verifications)
        if conclusion == "failure":
            assert len(await _rows(db, t.gates)) == 1


async def test_subject_seed_uses_episode_pin_and_does_not_cut_over_authority(db):
    hierarchy, _, _ = await _parent_tree(db, children=1)
    env = await setup(db, hierarchy, active=False, shadow=True)
    artifact = definition()
    # Model an episode frozen to this reviewed parent artifact rather than the
    # legacy helper's artifact, then exercise the command-owned creation seam.
    async with db.immediate() as conn:
        operation = await db.get_active_parent_integration_operation("parent")
        frozen = dict(
            operation["artifact_snapshot"],
            playbook_id=artifact.id,
            artifact_sha256=artifact.artifact_sha256(),
        )
        await conn.execute(
            update(t.integration_repair_operations)
            .where(t.integration_repair_operations.c.id == operation["id"])
            .values(artifact_snapshot=frozen)
        )
        from sqlalchemy import delete

        await conn.execute(
            delete(t.integration_subjects).where(t.integration_subjects.c.id == env.subject.id)
        )
        seeded, created = await ensure_parent_subject_on(db, conn, "parent", lambda _: artifact)
        assert created and seeded.id == env.subject.id and seeded.engine.value == "legacy"
        assert seeded.policy.artifact_sha256 == frozen["artifact_sha256"]
        _, created_again = await ensure_parent_subject_on(db, conn, "parent", lambda _: artifact)
        assert not created_again
    await env.runtime.tick(1000)
    await env.runtime.stop()
    assert (await db.get_integration_subject(env.subject.id))["engine"] == "legacy"
    assert env.commands.calls == []


async def test_mutation_requires_prewrite_and_rejects_stale_subject(db):
    hierarchy, _, children = await _parent_tree(db, children=1)
    env = await setup(db, hierarchy)
    await _code_receipt(db, children[0], "a" * 40, "d" * 40)
    await visit(env)
    subject = Subject.from_row(await db.get_integration_subject(env.subject.id))
    facts = await env.observer.observe(subject)
    decision = await env.policy.decide(subject, facts)
    refused = await env.adapters.guarded(subject, decision.request)
    assert refused.reason == "decision_prewrite_missing"
    await visit(env)
    stale = await env.adapters.guarded(subject, decision.request)
    assert stale.is_unknown
    assert (
        len([r for r in await _rows(db, t.tasks) if r["created_by_kind"] == "integration_writer"])
        == 1
    )


async def test_parent_collection_cannot_publish_default_branch_without_ci(db):
    hierarchy, _, _ = await _parent_tree(db, children=1)
    env = await setup(db, hierarchy)
    facts = await env.observer.observe(env.subject)
    target = env.subject.model_copy(update={"target_ref": "refs/heads/main"})
    decision = await env.policy.decide(target, facts)
    async with ParentEngineOwnership(db).operation("parent", subject=env.subject):
        refused = await env.adapters.perform(
            env.commands, target, decision.request.model_dump(mode="json")
        )
    assert refused.reason == "parent_target_is_default_branch"
    assert not env.commands.calls and not await _rows(db, t.task_delivery_receipts)


async def test_green_call_cannot_attach_to_successor_stage_after_waiting_for_authority(
    reuse_database, monkeypatch
):
    from tests.test_integration_repair import (
        STARTING_SHA,
        _configure_db,
        _seed_parent_operation,
        _add_parent_evidence,
    )

    database = await reuse_database("parent-reconciler-stage-race")
    await _configure_db(database)
    await _seed_parent_operation(database)
    await _add_parent_evidence(database, "green", run_id="run-green", conclusion="success")
    service = RepairService(database)
    await service.start("operation", STARTING_SHA, "failed-check", now=100)
    observed, resume = asyncio.Event(), asyncio.Event()
    original = ParentEngineOwnership.operation

    @asynccontextmanager
    async def paused(self, task_id, *, subject=None):
        if asyncio.current_task().get_name() == "green-at-entry":
            observed.set()
            await resume.wait()
        async with original(self, task_id, subject=subject):
            yield

    monkeypatch.setattr(ParentEngineOwnership, "operation", paused)
    green = asyncio.create_task(
        service.record_result("operation", "green", now=130), name="green-at-entry"
    )
    await asyncio.wait_for(observed.wait(), 10)
    assert (await service.expire("operation", 0, now=130))["outcome"] == "expired"
    resume.set()
    assert (await green)["action"] == "stale"
    stages = await _rows(database, t.integration_repair_stages)
    successor = next(s for s in stages if s["ordinal"] == 1)
    assert successor["state"] == "active" and successor["attempts"] == 0


async def test_held_red_verifier_does_not_strand_completed_fix_child(case, orchestrator_factory):
    orchestrator = await orchestrator_factory()
    red_head, verifier, _, provider = await _held_red_aggregate(case, orchestrator)
    original = await _rows(case.db, t.task_delivery_receipts)
    env = await setup(case.db, case.hierarchy, task_id="epic", promotion=case.promotion)
    env.commands.orchestrator.aconfirm_integration_owner_handoff = (
        orchestrator.aconfirm_integration_owner_handoff
    )
    episode = env.subject.parent_episode_id
    await visit(env)  # Trusted red exact subject settles the held verifier through the command.
    checkpoint = await case.db.get_integration_checkpoint("epic")
    assert checkpoint["state"] == "awaiting_children", env.commands.calls
    assert checkpoint["episode_id"] == episode
    assert (await case.db.get_task(verifier)).status == TaskStatus.FAILED
    provider.stop.assert_awaited_once()
    assert "integration_reopen_collection" in env.commands.calls
    assert await _rows(case.db, t.task_delivery_receipts) == original
    env = await restart(env, case.hierarchy, promotion=case.promotion)
    await visit(env)  # Refresh the generation and retire the previous writer projection.
    await visit(env)  # Collect and receipt the completed fix in the same episode.
    receipts = await _rows(case.db, t.task_delivery_receipts)
    assert len(receipts) == 3
    fix = next(r for r in receipts if r["source_task_id"] == "epic.3")
    assert fix["parent_episode_id"] == episode and fix["after_sha"] != red_head
    await visit(env)  # Adopt the receipt-derived head.
    await visit(env)  # File a fresh verifier for the changed aggregate.
    current = await case.db.get_integration_subject(env.subject.id)
    assert current["writer_task_id"] and current["writer_task_id"] != verifier
    assert current["head_sha"] == fix["after_sha"]
    assert not await _rows(case.db, t.integration_repair_stages)
    provider.stop.assert_awaited_once()


async def test_durably_refused_reopen_reaches_red_gate_instead_of_backing_off(
    case, orchestrator_factory
):
    """A refusal no retry can satisfy is a human decision, not a backoff loop."""
    orchestrator = await orchestrator_factory()
    red_head, verifier, _, provider = await _held_red_aggregate(case, orchestrator)
    original = await _rows(case.db, t.task_delivery_receipts)
    env = await setup(case.db, case.hierarchy, task_id="epic", promotion=case.promotion)
    # No provider stop/detach proof, so the reopen is durably refused and the
    # held verifier's work must survive the refusal untouched.
    await visit(env)
    refused = await case.db.get_integration_subject(env.subject.id)
    assert refused["refusal_streak"] == 1 and refused["gate_id"] is None
    assert env.commands.calls == [
        "integration_parent_action",
        "integration_reopen_collection",
    ]
    provider.stop.assert_not_awaited()
    provider.confirm_stopped.assert_not_awaited()
    assert (await case.db.get_task(verifier)).status == TaskStatus.IN_PROGRESS
    assert (await case.db.get_session("held-session")).claim_phase == "active"
    assert await _rows(case.db, t.task_delivery_receipts) == original
    [action] = [
        row
        for row in await case.db.list_integration_subject_journal(env.subject.id)
        if row["entry_kind"] == "action" and row["primitive"] == "git_merge_members"
    ]
    assert action["outcome"] == "unknown"
    assert action["payload"]["result"]["reason"] == "reopen_refused:blocked"
    assert action["payload"]["result"]["detail"]["refusal_reason"] == (
        "server-side verifier stop/detach proof is unavailable"
    )
    [marker] = await _rows(
        case.db, t.task_metadata, t.task_metadata.c.key == REOPEN_REFUSED_META_KEY
    )
    refusal = json.loads(marker["value"])
    assert refusal["refusal"] == "blocked" and refusal["head_sha"] == red_head
    assert refusal["episode_id"] == env.subject.parent_episode_id
    assert refusal["generation"] == env.subject.generation
    assert refusal["reason"] == "server-side verifier stop/detach proof is unavailable"

    await visit(env)  # The recorded refusal routes to the red human gate.
    held = await case.db.get_integration_subject(env.subject.id)
    [gate] = await _rows(case.db, t.gates)
    assert gate["status"] == "open"
    assert gate["question"] == (
        "Aggregate CI is red. Choose retry after a new child fix, or hold."
    )
    assert held["gate_id"] == gate["id"] and held["next_due_at"] is None
    assert env.commands.calls.count("integration_reopen_collection") == 1
    assert (await case.db.get_task(verifier)).status == TaskStatus.IN_PROGRESS
    assert await _rows(case.db, t.task_delivery_receipts) == original


async def test_failed_verifier_collects_new_fix_in_same_episode_without_redrives(case):
    red_head, verifier = await _failed_aggregate(case)
    original = await _rows(case.db, t.task_delivery_receipts)
    env = await setup(case.db, case.hierarchy, task_id="epic", promotion=case.promotion)
    episode = env.subject.parent_episode_id
    await visit(env)  # existing keen-stone-14 recovery
    checkpoint = await case.db.get_integration_checkpoint("epic")
    assert checkpoint["state"] == "awaiting_children"
    assert checkpoint["episode_id"] == episode and checkpoint["generation"] > env.subject.generation
    assert await _rows(case.db, t.task_delivery_receipts) == original
    env = await restart(env, case.hierarchy, promotion=case.promotion)
    await visit(env)  # refresh generation, retire failed writer projection
    await visit(env)  # collect new child fix with normal expected-old intent/receipt
    receipts = await _rows(case.db, t.task_delivery_receipts)
    assert len(receipts) == 3
    fix = next(r for r in receipts if r["source_task_id"] == "epic.3")
    assert fix["parent_episode_id"] == episode and fix["after_sha"] != red_head
    assert (await case.db.get_task(verifier)).status.value == "FAILED"
    await visit(env)  # adopt receipt-derived head
    await visit(env)  # fresh unified verifier
    current = await case.db.get_integration_subject(env.subject.id)
    assert current["writer_task_id"] and current["writer_task_id"] != verifier
    assert current["head_sha"] == fix["after_sha"]
    assert not await _rows(case.db, t.integration_repair_stages)
    # Repeat the fault with the unified verifier itself. Its durable ordinal
    # must survive recovery so the failed task is never filed again on replay.
    await visit(env)  # lease the first unified verifier
    first_unified = current["writer_task_id"]
    env = await restart(env, case.hierarchy, promotion=case.promotion)
    _git(["fetch", "origin", "aq/epic"], case.work)
    next_head = _commit_on(case.work, "aq/epic.4", current["head_sha"], "fix2.txt", "fix2\n")
    await case.db.create_task(
        Task(
            id="epic.4",
            project_id="p",
            parent_task_id="epic",
            repo_id="repo",
            branch_name="aq/epic.4",
            title="second fix",
            description="second fix",
            status=TaskStatus.COMPLETED,
        )
    )
    async with case.db.immediate() as conn:
        await conn.execute(
            update(t.tasks).where(t.tasks.c.id == first_unified).values(status="FAILED")
        )
        await conn.execute(
            insert(t.task_completion_records).values(
                id="unified-failed",
                task_id=first_unified,
                outcome="fail",
                branch="aq/epic",
                commits=json.dumps([current["head_sha"]]),
                summary="new scope failure",
                completed_at=2e10 + 1,
            )
        )
        [origin] = await _rows(
            case.db, t.task_branch_origins, t.task_branch_origins.c.task_id == "epic.3"
        )
        await conn.execute(
            insert(t.task_branch_origins).values(
                **dict(
                    origin,
                    id="origin-epic4",
                    task_id="epic.4",
                    branch_name="aq/epic.4",
                    base_sha=current["head_sha"],
                )
            )
        )
        await conn.execute(
            insert(t.task_integration_checkpoints).values(
                task_id="epic.4",
                repository_id="repo",
                branch="aq/epic.4",
                generation=0,
                checkpoint_sha=next_head,
                state="working",
                updated_at=1000,
            )
        )
    for _ in range(5):
        await visit(env)
    successor = await case.db.get_integration_subject(env.subject.id)
    assert successor["budget_ordinal"] == 2
    assert successor["writer_task_id"] not in {None, verifier, first_unified}
    assert (await case.db.get_task(first_unified)).status.value == "FAILED"
    assert not await _rows(case.db, t.integration_repair_stages)


async def test_active_engine_blocks_legacy_ticks_and_feature_off_does_not_transfer(db):
    hierarchy, _, _ = await _parent_tree(db, children=1)
    env = await setup(db, hierarchy)
    collection = CollectionService(db, hierarchy_service_factory=lambda: hierarchy)
    result = await collection.collect_parent("parent", 1000)
    assert result["outcome"] == "waiting" and "reconciler" in result["reason"]
    resolve_binding = AsyncMock()
    legacy_ci = ParentCIService(SimpleNamespace(db=db), resolve_binding)
    assert (await legacy_ci.handle({"task_id": "parent"}))["outcome"] == "none"
    resolve_binding.assert_not_awaited()
    async with db.immediate() as conn:
        ready = await hierarchy.parent_completion.mark_ready_on(conn, "parent")
    assert ready["outcome"] == "waiting"
    with pytest.raises(EngineRefused):
        async with ParentEngineOwnership(db).operation("parent"):
            pass
    versions = {env.subject.id: (await db.get_integration_subject(env.subject.id))["version"]}
    await ParentEngineOwnership(db).transfer(
        "repo",
        task_id="parent",
        engine="legacy",
        expected_versions=versions,
        reason="feature-off rollback",
    )
    async with ParentEngineOwnership(db).operation("parent"):
        pass
    assert (await db.get_integration_subject(env.subject.id))["engine"] == "legacy"


@pytest.mark.parametrize("answer", [None, "hold", "retry"])
async def test_rollback_preserves_binding_gate_for_legacy_mutations(db, answer):
    hierarchy, _, children = await _parent_tree(db, children=1)
    async with db.immediate() as conn:
        await conn.execute(update(t.tasks).where(t.tasks.c.id == children[0]).values(status="FAILED"))
    env = await setup(db, hierarchy)
    await visit(env)
    row = await db.get_integration_subject(env.subject.id)
    gate_id = row["gate_id"]
    if answer:
        result = await env.commands.execute(
            "gate_resolve", {"gate_id": gate_id, "resolution": answer, "resolved_by": "fixture"}
        )
        assert result["success"], result
    owner = ParentEngineOwnership(db)
    await owner.transfer(
        "repo", task_id="parent", engine="legacy",
        expected_versions={env.subject.id: row["version"]}, reason="gate-preserving rollback",
    )
    await env.runtime.stop()
    # The failed child can recover; its transient status must not erase a hold.
    async with db.immediate() as conn:
        await conn.execute(
            update(t.tasks).where(t.tasks.c.id == children[0]).values(status="COMPLETED")
        )
        if answer is None:
            # Expiring the dashboard projection supplies no immutable answer.
            await conn.execute(update(t.gates).where(t.gates.c.id == gate_id).values(status="expired"))
    legacy = CollectionService(db, hierarchy_service_factory=lambda: hierarchy)
    if answer == "retry":
        async with owner.operation("parent"):
            pass
    else:
        with pytest.raises(EngineRefused, match="gate"):
            async with owner.operation("parent"):
                pass
        assert (await legacy.collect_parent("parent", 1000))["outcome"] == "waiting"
        async with db.immediate() as conn:
            assert (await hierarchy.parent_completion.mark_ready_on(conn, "parent"))["outcome"] == "waiting"
    assert (await db.get_integration_subject(env.subject.id))["gate_id"] == gate_id
    assert len(await _rows(db, t.gates)) == 1


async def test_transfer_waits_for_remote_action_and_spawned_task_cannot_inherit_authority(db):
    hierarchy, _, _ = await _parent_tree(db, children=1)
    env = await setup(db, hierarchy)
    owner = ParentEngineOwnership(db)
    async with owner.operation("parent", subject=env.subject):

        async def escaped():
            async with owner.operation("parent"):
                pass

        with pytest.raises(EngineRefused, match="escaped"):
            await asyncio.create_task(escaped())
        transfer = asyncio.create_task(
            owner.transfer(
                "repo",
                task_id="parent",
                engine="legacy",
                expected_versions={env.subject.id: env.subject.version},
                reason="rollback",
            )
        )
        await asyncio.sleep(0.02)
        assert not transfer.done()
    assert (await transfer)["outcome"] == "transferred"


@pytest.mark.parametrize("target_engine", ["reconciler", "legacy"])
async def test_unresolved_intent_blocks_transfer(db, target_engine):
    hierarchy, _, _ = await _parent_tree(db, children=1)
    env = await setup(
        db, hierarchy, active=target_engine == "legacy", shadow=target_engine == "reconciler"
    )
    async with db.immediate() as conn:
        await conn.execute(
            insert(t.integration_promotion_intents).values(
                id="pending",
                domain_key="pending",
                receipt_id="pending",
                project_id="p",
                source_task_id="parent.1",
                target_task_id="parent",
                repository_id="repo",
                target_branch="aq/parent",
                source_head="b" * 40,
                source_base="a" * 40,
                expected_target="a" * 40,
                fence_owner_id="collector",
                fence_token=1,
                state="prepared",
                created_at=20,
                updated_at=20,
            )
        )
    with pytest.raises(EngineRefused, match="unresolved"):
        await ParentEngineOwnership(db).transfer(
            "repo",
            task_id="parent",
            engine=target_engine,
            expected_versions={env.subject.id: env.subject.version},
            reason="cutover",
            evidence=("approved",),
        )


async def test_shadow_only_journals_and_requires_decision_prewrite_for_mutation(db):
    hierarchy, _, _ = await _parent_tree(db, children=1)
    env = await setup(db, hierarchy, active=False, shadow=True)
    before = await db.get_integration_subject(env.subject.id)
    await visit(env)
    after = await db.get_integration_subject(env.subject.id)
    assert (after["head_sha"], after["phase"], after["engine"]) == (
        before["head_sha"],
        before["phase"],
        "legacy",
    )
    assert env.commands.calls == []
    journal = await db.list_integration_subject_journal(env.subject.id)
    assert any(r["entry_kind"] == "decision" and r["mode"] == "shadow" for r in journal)
    assert after["next_due_at"] <= after["due_set_at"] + after["max_wait_seconds"]


def test_parent_policy_paths_are_typed_and_not_accepted_for_root():
    artifact = definition()
    raw = artifact.integration_policy.model_dump(mode="json", exclude_none=True)
    assert "parent_episode" in raw["tables"]
    raw["tables"]["root_batch"] = raw["tables"].pop("parent_episode")
    with pytest.raises(ValueError, match="unknown policy path"):
        IntegrationPolicy.model_validate(raw)
