"""Parent visits: lost events, exact receipt refresh, recovery and exclusive engines."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, insert, select, update

from src.commands.gate_commands import GateCommandsMixin
from src.commands.integration_commands import IntegrationCommandsMixin
from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
from src.database import tables as t
from src.git.manager import GitManager
from src.integration.child_delivery import ChildDelivery
from src.integration.collection import CollectionService
from src.integration.engine import EngineRefused
from src.integration.gates import GatePrimitives
from src.integration.hierarchy import HierarchyIntegration
from src.integration.models import PromotionInput
from src.integration.parent_adapters import (
    REOPEN_REFUSED_META_KEY,
    ParentPolicyFacts,
    ParentPrimitiveAdapters,
)
from src.integration.parent_ci import ParentCIService
from src.integration.parent_engine import ParentEngineOwnership
from src.integration.parent_runtime import (
    ParentSubjectRuntime,
    ParentVisitObserver,
    PinnedParentPolicy,
    ensure_parent_subject_on,
)
from src.integration.parent_subjects import ParentDatabaseObservationReader, ParentSubjectAdapter
from src.integration.promotion import PromotionService
from src.integration.repair import RepairService
from src.integration.subjects import (
    GateArgs,
    PolicyArtifactPin,
    Subject,
)
from src.integration.writers import WriterPrimitives
from src.models import Agent, Project, SessionRecord, Task, TaskStatus
from src.orchestrator.monitoring import MonitoringMixin
from src.playbooks.definition import load_definition_json
from src.playbooks.integration_policy import IntegrationPolicy
from src.profiles.capabilities import DENY_ALL
from tests.test_integration_cancelled_collection import (
    _commit_on,
    _failed_aggregate,
    _git,
    _held_red_aggregate,
    _rows,
)
from tests.test_integration_cancelled_collection import (
    case as case_fixture,
)
from tests.test_integration_parent_completion import _code_receipt, _enable_project, _parent_tree

case = case_fixture


def definition():
    return load_definition_json(
        Path("tests/fixtures/playbooks/v2/parent-integration/artifact.json").read_text()
    )


class ContainerSweep(MonitoringMixin):
    """The shipped container sweep's stale-claim leg, bound to a database.

    ``release_stale_container_claims`` only reads ``self.db``; mounting the real
    mixin is what lets a test run the sweep the daemon runs instead of
    re-implementing its two statements.
    """

    def __init__(self, db):
        self.db = db


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
        loop=runtime.loops[0] if runtime.loops else None,
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


async def _empty_parent(db):
    """The enabled project, the parent row and its collection episode, no children.

    ``_parent_tree`` files children as part of building the parent, which marks
    it a container — and a container is never leased, so the claim this helper
    drives could not happen after that.  The claim comes first and the children
    land under the held parent, which is the order the live case had.
    """
    await _enable_project(db)
    await db.create_task(
        Task(
            id="parent",
            project_id="p",
            repo_id="repo",
            branch_name="aq/parent",
            title="parent",
            description="parent",
            status=TaskStatus.IN_PROGRESS,
        )
    )
    hierarchy = HierarchyIntegration(
        db,
        default_head_resolver=lambda _repo, _branch: "a" * 40,
        checkpoint_verifier=lambda _task, _repo, head: head,
    )
    return hierarchy, await hierarchy.checkpoint_parent("parent", "a" * 40, 0)


async def _stop_the_parent_worker(db, tmp_path, *, children=1):
    """The parent tree of bold-crest-75, built by driving the paths that make it.

    The live shape: the parent ran as a worker's own task, committed its head,
    filed children under it — becoming a container while held — and then its
    pool worker drained for good.  What the drain leaves is *not* a session row
    pointing back at the parent: ``terminate_pool_session`` writes the parent
    back ``IN_PROGRESS`` with no agent and clears ``sessions.task_id``,
    ``claim_phase`` and the workspace lock, the teardown then marks the row
    ``stopped``/``stopped``, and the only surviving record of the claim is
    ``task_metadata.claimed_by_session`` (sharp-ridge-57's premise,
    fleet-delta-97's correction).  Every write below is the shipped one, so the
    shape is the one the daemon leaves rather than one assembled by hand.

    ``children=0`` is the childless container the flag still marks — bold-crest-75
    after its last child was deleted, which is what stranded it in the first
    place.

    ``task_session_attempts`` carries the attempt the claim began and the drain
    ended, which is what tells ``mark_ready_on`` that this parent ran as a worker
    rather than never having been leased at all.
    """
    hierarchy, checkpointed = await _empty_parent(db)
    work_dir = str(tmp_path / "parent-session")
    await db.create_agent(
        Agent(id="parent-agent", name="parent-agent", profile_id="worker")
    )
    await db.create_session(
        SessionRecord(
            id="parent-session",
            project_id="p",
            profile_id="worker",
            harness="claude",
            provider="fake",
            name="p-worker--p--parent-session",
            lifecycle="pool",
            work_dir=work_dir,
            epoch="e",
            instance_token="parent-instance",
            started_at=1.0,
            state="running",
            desired_state="running",
            task_id=None,
            agent_id="parent-agent",
        )
    )
    # A parent nobody holds is what recovery resets to READY; that is also the
    # only way the claim path will look at it, since a container is never leased.
    await db.transition_task(
        "parent", TaskStatus.READY, context="recovery", assigned_agent_id=None
    )
    async with db.immediate() as conn:
        assert await db.take_task(conn, "parent", agent_id="parent-agent", now=1.5)
        epoch = await conn.scalar(
            select(t.tasks.c.claim_epoch).where(t.tasks.c.id == "parent")
        )
        await db.record_holder(
            conn,
            session_id="parent-session",
            task_id="parent",
            claim_epoch=epoch,
            agent_id="parent-agent",
            work_dir=work_dir,
            now=2.0,
            agent_reserved=True,
        )
    assert await db.activate_claim("parent-session", "parent", epoch=epoch, now=3.0)
    child_ids = []
    if children:
        # Filing under a task held right now is bold-flare-35's window: the
        # children are what make the held parent a container.
        filed = await hierarchy.file_children(
            "parent", [{"title": f"child {index}"} for index in range(children)], 0
        )
        child_ids = [row["task_id"] for row in filed["children"]]
        async with db.immediate() as conn:
            await conn.execute(
                update(t.tasks).where(t.tasks.c.id.in_(child_ids)).values(
                    status="COMPLETED"
                )
            )
            await conn.execute(
                update(t.task_integration_checkpoints)
                .where(t.task_integration_checkpoints.c.task_id.in_(child_ids))
                .values(checkpoint_sha="b" * 40)
            )
    else:
        # The flag lands with a task's first child, so a childless flagged
        # container is exactly one whose children are gone.
        async with db.immediate() as conn:
            await db.mark_container("parent", conn=conn)
    # The drain: the worker goes away, the parent keeps the claim record.
    assert (
        await db.terminate_pool_session(
            "parent-session", reason="drained", task_status=TaskStatus.IN_PROGRESS
        )
    ).released
    await db.update_session(
        "parent-session",
        state="stopped",
        desired_state="stopped",
        ended_at=4.0,
        end_reason="drained",
    )
    return hierarchy, checkpointed, child_ids


async def test_deleting_the_last_child_leaves_a_stopped_holders_parent_finishable(db, tmp_path):
    """A container with no children left is finished, whatever its dead holder did.

    Deleting a container's last child is what stranded bold-crest-75: §7
    settlement refuses the parent because its collection episode owns the
    completion, ``mark_ready_on`` refuses it while it is not ``PAUSED``, and the
    claim frontier never offers a container to anybody — so the claim a stopped
    worker left behind was the last thing in the way, and ``reopen-collection``,
    ``recover-parent-head`` and ``redrive-root`` all refuse an IN_PROGRESS
    parent.  Nothing but the delete may move it.

    The parent must reach verification, and then COMPLETED, on the shipped
    parent runtime alone.  With the child gone the collected aggregate is the
    parent's own pre-collection head: that is the head CI runs against on the
    ``aq/parent`` ref, so it is what the evidence must name.
    """
    hierarchy, checkpointed, (child,) = await _stop_the_parent_worker(db, tmp_path)
    parent = await db.get_task("parent")
    assert (parent.status, parent.assigned_agent_id) == (TaskStatus.IN_PROGRESS, None)
    # The shape the 14:05Z restart found: a dead pool session that no longer
    # points at the parent, with the claim recorded only on the parent.
    holder = await db.get_session("parent-session")
    assert (holder.state, holder.desired_state, holder.task_id) == ("stopped", "stopped", None)
    assert await db.get_task_meta("parent", "claimed_by_session") == "parent-session"
    assert await db.stale_container_claim_candidates() == ["parent"]

    await db.delete_task(child, branch_policy="keep")

    # The delete took the exact stopped claim back, in its own transaction:
    # the parent is now the PAUSED, unassigned shape the collection consumers
    # require, its episode and its own head intact.
    parent = await db.get_task("parent")
    assert (parent.status, parent.assigned_agent_id) == (TaskStatus.PAUSED, None)
    session = await db.get_session("parent-session")
    assert (session.state, session.task_id, session.claim_phase) == ("stopped", None, None)
    assert session.last_claim_result == "stale_container_claim_released"
    assert await db.get_task_meta("parent", "claimed_by_session") is None
    assert await db.get_task(child) is None
    assert await db.stale_container_claim_candidates() == []
    checkpoint = await db.get_integration_checkpoint("parent")
    assert checkpoint["episode_id"] == checkpointed["episode_id"]
    assert checkpoint["checkpoint_sha"] == "a" * 40
    assert await _rows(db, t.task_delivery_receipts) == []

    ci = SimpleNamespace(
        handle=AsyncMock(return_value={"outcome": "green", "evidence_ids": ["ci"]})
    )
    env = await setup(db, hierarchy, parent_ci=ci)
    for _ in range(4):
        await visit(env)  # head refresh, readiness, file, lease

    # Verification started, on the aggregate that is actually left: the
    # parent's own pre-collection head, published on an ``aq/parent`` ref.
    checkpoint = await db.get_integration_checkpoint("parent")
    assert checkpoint["state"] == "verifying"
    assert checkpoint["checkpoint_sha"] == "a" * 40
    assert ci.handle.await_args.args[0]["head_sha"] == "a" * 40
    # The parent itself is not the verifier and never has to be: it stays the
    # PAUSED collection the writer is verifying on its behalf.
    assert (await db.get_task("parent")).status is TaskStatus.PAUSED

    async with db.immediate() as conn:
        await conn.execute(
            insert(t.integration_check_evidence).values(
                id="ci",
                operation_id=checkpointed["operation_id"],
                parent_task_id="parent",
                parent_generation=checkpoint["generation"],
                parent_head_sha="a" * 40,
                producer_id="forge-observer",
                workflow_id="workflow",
                run_id="run",
                attempt=1,
                required_check_version="parent-v1",
                checks={"unit": "success"},
                conclusion="success",
                classification="conclusive",
                observed_at=900,
            )
        )
    await visit(env)
    await visit(env)

    assert (await db.get_task("parent")).status is TaskStatus.COMPLETED
    assert (await db.get_integration_subject(env.subject.id))["phase"] == "done"
    # Nothing here is an operator command: the parent runtime drove every leg,
    # and none of the controls that refuse an IN_PROGRESS parent was needed.
    assert env.commands.calls
    assert not {
        "integration_reopen_collection",
        "integration_recover_parent_head",
        "integration_redrive_root",
    } & set(env.commands.calls)


async def test_the_backstop_reconciler_takes_back_the_same_stopped_claim(db, tmp_path):
    """A strand that already exists is repaired by the shipped sweep, not an operator.

    ``_delete_task_body`` prevents the shape; this is the path for rows that
    predate it (``aq doctor`` names ``claims.container_held``, and the delete's
    own event is long gone).  Same proof, same release — only the caller differs.
    """
    # The strand as it is found: the children are gone, the parent's worker
    # stopped, and nothing has run since.
    await _stop_the_parent_worker(db, tmp_path, children=0)
    assert await db.stale_container_claim_candidates() == ["parent"]

    assert (await db.release_stale_container_claim("parent")).released

    parent = await db.get_task("parent")
    assert (parent.status, parent.assigned_agent_id) == (TaskStatus.PAUSED, None)
    session = await db.get_session("parent-session")
    assert (session.state, session.task_id, session.claim_phase) == ("stopped", None, None)
    # The claim record went with it, so the next interval has nothing to find.
    assert await db.get_task_meta("parent", "claimed_by_session") is None
    assert await db.stale_container_claim_candidates() == []
    # Idempotent: the sweep runs every interval and must not act twice.
    assert not (await db.release_stale_container_claim("parent")).released


async def test_a_restart_cycle_ends_paused_under_a_stopped_pool_holder(db, tmp_path):
    """The oscillation every restart used to repeat now ends in the sweep.

    On 2026-10-05 each restart logged ``Recovery: resetting task ... from
    IN_PROGRESS to READY`` and then ``Released container task ... to IN_PROGRESS
    without an agent``, and stopped there: the parent came back to IN_PROGRESS
    with a claim no reconciler could see and no parent runtime would accept
    (bold-crest-75, fleet-delta-97).  The two halves of the cycle are the
    shipped statements; the sweep that has to break it is the shipped one too.
    """
    await _stop_the_parent_worker(db, tmp_path, children=0)
    parent = await db.get_task("parent")
    assert parent.status is TaskStatus.IN_PROGRESS

    # Recovery: a parent nobody holds goes back to READY ...
    await db.transition_task(
        "parent", TaskStatus.READY, context="recovery", assigned_agent_id=None
    )
    # ... and the container sweep puts it straight back on IN_PROGRESS, no agent.
    await db.transition_task(
        "parent", TaskStatus.IN_PROGRESS, context="container_released", assigned_agent_id=None
    )
    assert (await db.get_task("parent")).status is TaskStatus.IN_PROGRESS

    sweep = ContainerSweep(db)
    assert await sweep.release_stale_container_claims() == ["parent"]
    assert (await db.get_task("parent")).status is TaskStatus.PAUSED
    assert (await db.get_task("parent")).assigned_agent_id is None
    assert await db.get_task_meta("parent", "claimed_by_session") is None
    # And the next cycle has nothing left to oscillate over.
    assert await sweep.release_stale_container_claims() == []


async def test_archiving_the_last_child_takes_the_same_claim_back(db, tmp_path):
    """Archive empties a container exactly as a delete does, so it strands the same.

    ``archive_task`` is the structural twin of ``delete_task`` on this path: the
    child's row leaves ``tasks`` either way, so the parent is left with no
    children, no receipt and a stopped holder's claim.  Both removals must hand
    the parent's lifecycle back; neither may leave an operator control to run.
    """
    _, _, (child,) = await _stop_the_parent_worker(db, tmp_path)
    assert await db.stale_container_claim_candidates() == ["parent"]

    # The child is archived as delivered work, which is the shape an operator
    # is in when they sweep up a duplicate rather than deleting it outright.
    async with db.immediate() as conn:
        await conn.execute(
            update(t.tasks).where(t.tasks.c.id == child).values(status="COMPLETED")
        )
    assert await db.archive_task(child) is True

    parent = await db.get_task("parent")
    assert (parent.status, parent.assigned_agent_id) == (TaskStatus.PAUSED, None)
    assert (await db.get_session("parent-session")).task_id is None
    assert await db.stale_container_claim_candidates() == []


@pytest.mark.parametrize(
    "damage",
    [
        "running",
        "restarting",
        "mid_claim",
        "unfenced_epoch",
        "second_holder",
        "second_holder_restarting",
        "moved_on",
        "agent_reused",
        "workspace_locked",
        "manual_hold",
        "open_child",
        "no_episode",
        "not_a_container",
        "no_claim_record",
    ],
)
async def test_the_stale_container_claim_release_refuses_everything_it_cannot_prove(
    db, tmp_path, damage
):
    """The reconciler may only move a claim it can prove is already dead.

    Every refusal here is one condition of
    ``stale_container_claim_clauses`` plus the workspace and agent checks the
    predicate cannot express.  A stopped session still mid-drain, a session a
    restart is about to revive, one stopped mid-preparation on a claim nobody
    ever activated, a claim epoch the task has already moved past, a second
    live session on the same task (running, or stopped with a restart pending),
    a stopped holder that has since taken other
    work, an agent and a workspace that have been reused, a human hold, a child
    still in flight, a checkpoint with no collection episode, a row that is not
    a container at all, or a claim with no record naming a holder — each of
    those keeps the claim, because the release moves a task's lifecycle and
    nothing less than the exact-holder proof may do that.
    """
    _, _, children = await _stop_the_parent_worker(db, tmp_path, children=2)
    child = children[0]
    async with db.immediate() as conn:
        epoch = await conn.scalar(select(t.tasks.c.claim_epoch).where(t.tasks.c.id == "parent"))
        if damage == "running":
            await conn.execute(
                update(t.sessions).where(t.sessions.c.id == "parent-session").values(
                    state="running", desired_state="running"
                )
            )
        elif damage == "restarting":
            await conn.execute(
                update(t.sessions).where(t.sessions.c.id == "parent-session").values(
                    desired_state="running"
                )
            )
        elif damage == "mid_claim":
            await conn.execute(
                update(t.sessions).where(t.sessions.c.id == "parent-session").values(
                    claim_phase="preparing"
                )
            )
        elif damage == "unfenced_epoch":
            await conn.execute(
                update(t.tasks).where(t.tasks.c.id == "parent").values(claim_epoch=epoch + 1)
            )
        elif damage in {"second_holder", "second_holder_restarting"}:
            await conn.execute(
                insert(t.sessions).values(
                    id="second-session",
                    project_id="p",
                    profile_id="worker",
                    harness="claude",
                    provider="fake",
                    name="p-worker--p--second-session",
                    lifecycle="pool",
                    work_dir=str(tmp_path / "second-session"),
                    epoch="e",
                    instance_token="second-instance",
                    started_at=1.0,
                    state="running",
                    desired_state="running",
                    task_id="parent",
                    agent_id="parent-agent",
                    claim_phase="active",
                    claim_phase_at=1.0,
                    last_claim_epoch=epoch + 1,
                )
            )
            if damage == "second_holder_restarting":
                # A session a restart is about to revive is not a dead one, so
                # it refuses just as a running one does.
                await conn.execute(
                    update(t.sessions).where(t.sessions.c.id == "second-session").values(
                        state="stopped", desired_state="running"
                    )
                )
        elif damage == "moved_on":
            # Stopped, but not on this task: the claim it left behind here is
            # not its only one, and clearing its row would disturb the other.
            await conn.execute(
                update(t.sessions).where(t.sessions.c.id == "parent-session").values(
                    task_id=child
                )
            )
        elif damage == "agent_reused":
            await conn.execute(
                update(t.agents)
                .where(t.agents.c.id == "parent-agent")
                .values(current_task_id=child)
            )
        elif damage == "workspace_locked":
            await conn.execute(
                insert(t.workspaces).values(
                    id="parent-ws",
                    project_id="p",
                    workspace_path=str(tmp_path / "parent-session"),
                    kind_id="project-repo",
                    source_type="LINK",
                    locked_by_task_id="parent",
                    created_at=1.0,
                )
            )
        elif damage == "manual_hold":
            await conn.execute(
                insert(t.task_metadata).values(
                    task_id="parent",
                    key="manual_pause",
                    value=json.dumps({"reason": "operator is looking at it"}),
                )
            )
        elif damage == "open_child":
            await conn.execute(
                update(t.tasks).where(t.tasks.c.id == child).values(status="READY")
            )
        elif damage == "no_episode":
            await conn.execute(
                update(t.task_integration_checkpoints)
                .where(t.task_integration_checkpoints.c.task_id == "parent")
                .values(episode_id=None)
            )
        elif damage == "no_claim_record":
            # Nothing names the holder, so there is no proof of who holds it.
            await conn.execute(
                delete(t.task_metadata).where(
                    t.task_metadata.c.task_id == "parent",
                    t.task_metadata.c.key == "claimed_by_session",
                )
            )
        else:
            # Neither reason the frontier keeps a row off it: not the §7
            # container flag, and no children either.
            await conn.execute(
                delete(t.task_metadata).where(
                    t.task_metadata.c.task_id == "parent",
                    t.task_metadata.c.key == "container",
                )
            )
            await conn.execute(
                update(t.tasks)
                .where(t.tasks.c.id.in_([child, children[1]]))
                .values(parent_task_id=None)
            )
    holder = await db.get_session("parent-session")
    before = ((await db.get_task("parent")).status, holder.state, holder.claim_phase,
              await db.get_task_meta("parent", "claimed_by_session"))
    assert damage not in await db.stale_container_claim_candidates()
    assert not (await db.release_stale_container_claim("parent")).released
    holder = await db.get_session("parent-session")
    assert before == (
        (await db.get_task("parent")).status, holder.state, holder.claim_phase,
        await db.get_task_meta("parent", "claimed_by_session"),
    ), "a refused release must not have written anything"


async def test_subject_seed_uses_episode_pin_and_current_ownership(db):
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
        assert created and seeded.id == env.subject.id and seeded.engine.value == "reconciler"
        assert seeded.policy.artifact_sha256 == frozen["artifact_sha256"]
        _, created_again = await ensure_parent_subject_on(db, conn, "parent", lambda _: artifact)
        assert not created_again
    await env.runtime.tick(1000)
    await env.runtime.stop()
    assert (await db.get_integration_subject(env.subject.id))["engine"] == "reconciler"
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
        _add_parent_evidence,
        _configure_db,
        _seed_parent_operation,
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
    with pytest.raises(ValueError, match="legacy"):
        await ParentEngineOwnership(db).transfer(
            "repo", task_id="parent", engine="legacy", expected_versions=versions,
            reason="feature-off rollback",
        )
    assert (await db.get_integration_subject(env.subject.id))["engine"] == "reconciler"





async def held_parent(db, *, answer="hold"):
    hierarchy, _, children = await _parent_tree(db, children=1)
    async with db.immediate() as conn:
        await conn.execute(update(t.tasks).where(t.tasks.c.id == children[0]).values(status="FAILED"))
    env = await setup(db, hierarchy)
    await visit(env)
    row = await db.get_integration_subject(env.subject.id)
    gate_id = row["gate_id"]
    if answer:
        result = await env.commands.execute(
            "gate_resolve", {"gate_id": gate_id, "resolution": answer, "resolved_by": "fixture"},
        )
        assert result["success"], result
    row = await db.get_integration_subject(env.subject.id)
    request = {
        "subject_id": row["id"], "gate_id": gate_id, "expected_version": row["version"],
        "reason": "human is ready to resume", "dry_run": False,
    }
    return env, hierarchy, children, request


async def test_release_held_gate_resumes_after_restart_without_rewriting_answer(db):
    env, hierarchy, _, request = await held_parent(db)
    env = await restart(env, hierarchy, active=True)
    before = await db.get_integration_subject(env.subject.id)
    old_journal = await db.list_integration_subject_journal(env.subject.id)
    old_gates = await _rows(db, t.gates)
    old_receipts = await _rows(db, t.task_delivery_receipts)
    # Ordinary resolution still cannot revise a hold into approval.
    assert (await env.commands.execute("gate_resolve", {
        "gate_id": request["gate_id"], "resolution": "retry", "resolved_by": "fixture",
    }))["reason"] == "answer_immutable"
    preview = await env.commands.execute(
        "integration_release_held_gate", {**request, "dry_run": True},
    )
    assert preview["success"] and preview["expected_version"] == before["version"]
    assert await db.get_integration_subject(env.subject.id) == before
    assert await db.list_integration_subject_journal(env.subject.id) == old_journal
    result = await env.commands.execute("integration_release_held_gate", request)
    assert result["success"] and result["outcome"] == "released", result
    after = await db.get_integration_subject(env.subject.id)
    assert after["version"] == before["version"] + 1 and after["gate_id"] is None
    assert after["next_due_at"] == after["wake_requested_at"]
    for field in ("engine", "parent_episode_id", "head_sha", "generation", "writer_task_id",
                  "writer_fence_token", "policy_artifact_sha256"):
        assert after[field] == before[field]
    assert await _rows(db, t.gates) == old_gates
    assert await _rows(db, t.task_delivery_receipts) == old_receipts
    journal = await db.list_integration_subject_journal(env.subject.id)
    assert journal[:-1] == old_journal
    release = journal[-1]
    assert release["idempotency_key"] == f"gate-release:{request['gate_id']}"
    assert release["subject_version"] == before["version"]
    assert release["payload"]["previous_answer"]["choice"] == "hold"
    assert release["payload"]["operator_id"] == "human:local-operator"
    assert release["payload"]["reason"] == request["reason"]
    env = await restart(env, hierarchy, active=True)
    await visit(env)
    fresh = await db.get_integration_subject(env.subject.id)
    assert fresh["gate_id"] and fresh["gate_id"] != request["gate_id"]
    assert len(await _rows(db, t.gates)) == 2
    old_answer = await env.commands.execute("gate_resolve", {
        "gate_id": request["gate_id"], "resolution": "retry", "resolved_by": "fixture",
    })
    assert old_answer["reason"] == "gate_not_current"




@pytest.mark.parametrize("change", ["version", "gate", "reason", "missing_version", "head",
                                    "generation", "policy", "terminal", "unanswered", "retry"])
async def test_release_held_gate_refuses_stale_or_nonheld_requests_without_writes(
    db, change, monkeypatch,
):
    env, _, _, request = await held_parent(
        db, answer=None if change == "unanswered" else "retry" if change == "retry" else "hold",
    )
    if change == "version":
        request["expected_version"] -= 1
    elif change == "gate":
        request["gate_id"] = "old-gate"
    elif change == "reason":
        request["reason"] = "   "
    elif change == "missing_version":
        request.pop("expected_version")
    elif change == "policy":
        # Policies and journal entries are immutable in PostgreSQL. Exercise the
        # identity fence with a stale journal observation, without disabling it.
        import src.integration.gates as gates_module

        read_entry = gates_module.journal_entry_on

        async def stale_policy(*args):
            entry = await read_entry(*args)
            return {**entry, "policy_artifact_sha256": "sha256:" + "e" * 64}

        monkeypatch.setattr(gates_module, "journal_entry_on", stale_policy)
    elif change in {"head", "generation", "terminal"}:
        values = {
            "head": {"head_sha": "f" * 40}, "generation": {"generation": 99},
            "terminal": {"phase": "done", "gate_id": None, "next_due_at": None,
                         "closed_reason": "closed"},
        }[change]
        async with db.immediate() as conn:
            await conn.execute(
                update(t.integration_subjects).where(t.integration_subjects.c.id == env.subject.id)
                .values(**values),
            )
    before = await db.get_integration_subject(env.subject.id)
    journal = await db.list_integration_subject_journal(env.subject.id)
    result = await env.commands.execute("integration_release_held_gate", request)
    assert not result["success"] and result["outcome"] == "refused", result
    assert await db.get_integration_subject(env.subject.id) == before
    assert await db.list_integration_subject_journal(env.subject.id) == journal


@pytest.mark.parametrize("kind", [PrincipalKind.SESSION, PrincipalKind.SERVICE,
                                PrincipalKind.PLAYBOOK])
async def test_agent_and_policy_principals_cannot_release_held_gate(db, kind):
    env, _, _, request = await held_parent(db)
    before = await db.get_integration_subject(env.subject.id)
    with principal_context(ExecutionPrincipal(kind=kind, policy=DENY_ALL, elevated=True)):
        result = await env.commands.execute("integration_release_held_gate", request)
    assert not result["success"] and result["outcome"] == "unauthorized"
    assert await db.get_integration_subject(env.subject.id) == before


@pytest.mark.parametrize("choice", ["reject", "abort"])
async def test_release_held_gate_cannot_lift_a_rejection(db, choice):
    hierarchy, _, _ = await _parent_tree(db, children=1)
    env = await setup(db, hierarchy)
    gate = await GatePrimitives(db).gate(
        env.subject, GateArgs(question="Proceed?", choices=("hold", "reject", "abort"),
                              no_default=True),
    )
    gate_id = gate.detail["gate_id"]
    assert (await env.commands.execute("gate_resolve", {
        "gate_id": gate_id, "resolution": choice, "resolved_by": "fixture",
    }))["success"]
    row = await db.get_integration_subject(env.subject.id)
    result = await env.commands.execute("integration_release_held_gate", {
        "subject_id": row["id"], "gate_id": gate_id, "expected_version": row["version"],
        "reason": "resume", "dry_run": False,
    })
    assert not result["success"] and result["error"] == "gate_not_held"
    assert await db.get_integration_subject(env.subject.id) == row


async def test_release_held_gate_rolls_back_its_audit_if_subject_update_fails(db, monkeypatch):
    env, _, _, request = await held_parent(db)
    before = await db.get_integration_subject(env.subject.id)
    journal = await db.list_integration_subject_journal(env.subject.id)
    monkeypatch.setattr(db, "update_integration_subject_on", AsyncMock(return_value=None))
    with pytest.raises(RuntimeError, match="locked subject changed"):
        await env.commands.execute("integration_release_held_gate", request)
    assert await db.get_integration_subject(env.subject.id) == before
    assert await db.list_integration_subject_journal(env.subject.id) == journal


async def test_release_held_gate_preserves_manual_and_project_pause(db):
    env, _, _, request = await held_parent(db)
    async with db.immediate() as conn:
        await conn.execute(
            insert(t.task_metadata).values(task_id="parent", key="manual_pause", value="{}"),
        )
        await conn.execute(update(t.projects).where(t.projects.c.id == "p").values(status="PAUSED"))
    pause_before = await db.get_task_meta("parent", "manual_pause")
    assert pause_before is not None
    assert (await env.commands.execute("integration_release_held_gate", request))["success"]
    await visit(env)
    facts = await env.observer.observe(Subject.from_row(await db.get_integration_subject(env.subject.id)))
    assert facts.holds
    assert await db.get_task_meta("parent", "manual_pause") == pause_before
    assert (await db.get_project("p")).status.value == "PAUSED"


async def test_release_held_gate_waits_for_parent_operation_and_rechecks_version(db):
    env, _, _, request = await held_parent(db)
    owner = ParentEngineOwnership(db)
    async with owner.operation("parent", subject=Subject.from_row(
        await db.get_integration_subject(env.subject.id),
    )):
        release = asyncio.create_task(env.commands.execute("integration_release_held_gate", request))
        await asyncio.sleep(0.02)
        assert not release.done()
        async with db.immediate() as conn:
            await db.update_integration_subject_on(
                conn, subject_id=env.subject.id, expected_version=request["expected_version"],
                values={"refusal_streak": 1}, now=2000,
            )
    result = await release
    assert not result["success"] and result["error"] == "stale_subject"
    assert (await db.get_integration_subject(env.subject.id))["gate_id"] == request["gate_id"]
    assert not any(r["outcome"] == "released" for r in
                   await db.list_integration_subject_journal(env.subject.id))


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
                engine="reconciler",
                evidence=("approved",),
                expected_versions={env.subject.id: env.subject.version},
                reason="rollback",
            )
        )
        await asyncio.sleep(0.02)
        assert not transfer.done()
    assert (await transfer)["outcome"] == "transferred"


@pytest.mark.parametrize("target_engine", ["reconciler"])
async def test_unresolved_intent_blocks_transfer(db, target_engine):
    hierarchy, _, _ = await _parent_tree(db, children=1)
    env = await setup(
        db, hierarchy, active=True
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




def test_parent_policy_paths_are_typed_and_not_accepted_for_root():
    artifact = definition()
    raw = artifact.integration_policy.model_dump(mode="json", exclude_none=True)
    assert "parent_episode" in raw["tables"]
    raw["tables"]["root_batch"] = raw["tables"].pop("parent_episode")
    with pytest.raises(ValueError, match="unknown policy path"):
        IntegrationPolicy.model_validate(raw)

