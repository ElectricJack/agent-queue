# ruff: noqa: F811
"""Accepted root repairs finish their original claims independently of candidate CI."""

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, insert, select, update

from src.commands.principal import principal_context
from src.database.tables import (
    integration_batches,
    integration_branch_owners,
    integration_candidate_member_results,
    integration_candidate_ref_mutations,
    integration_candidate_revisions,
    integration_check_evidence,
    integration_repair_operations,
    integration_repair_stages,
    sessions,
    tasks,
    workspaces,
)
from src.integration.repair import RepairService
from src.models import TaskStatus
from src.profiles.capabilities import CapabilityPolicy
from tests.test_integration_candidates import _git
from tests.test_integration_published_repair_handoff import repair, reservation, submit  # noqa: F401


async def close(repair, **overrides):
    principal = replace(
        repair.principal,
        policy=CapabilityPolicy.from_namespaces(
            aq_commands=["task_close", "integration_resolve_candidate_member", "create_task"],
        ),
    )
    with principal_context(principal):
        return await repair.handler.execute(
            "task_close",
            {
                "task_id": repair.task_id,
                "session_id": "repair-session",
                "claim_epoch": repair.args["claim_epoch"],
                "outcome": "pass",
                "summary": "Exact candidate accepted and handed to the collector for CI.",
                "commit": repair.head,
                "_scope": {
                    "kind": "session",
                    "session_id": "repair-session",
                    "session_instance_token": "instance-1",
                    "project_id": "p",
                },
                **overrides,
            },
        )


async def stage(repair):
    async with repair.db._engine.connect() as conn:
        return dict(
            (
                await conn.execute(
                    select(integration_repair_stages).where(
                        integration_repair_stages.c.repair_task_id == repair.task_id,
                    )
                )
            )
            .mappings()
            .one()
        )


@pytest.mark.parametrize("repair", ["batch_debug"], indirect=True)
async def test_accepted_public_close_releases_claim_without_passing_ci(repair):  # noqa: F811
    before = await stage(repair)
    result = await submit(repair)
    assert result["outcome"] == "accepted", result
    owner = await repair.ownership.get_owner(repair.fence.target)
    closed = await close(repair)
    assert closed["success"], (closed, await stage(repair), await reservation(repair), owner)
    assert (await repair.db.get_task(repair.task_id)).status is TaskStatus.COMPLETED
    session = await repair.db.get_session("repair-session")
    workspace = await repair.db.get_workspace("repair-slot")
    assert session.task_id is None and session.claim_phase is None
    assert workspace.locked_by_task_id is None
    assert workspace.locked_by_agent_id == "repair-agent"
    assert await repair.ownership.get_owner(repair.fence.target) == owner
    assert _git(repair.checkout, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
    current = await stage(repair)
    for field in ("started_at", "deadline_at", "ordinal", "attempts", "state"):
        assert current[field] == before[field]
    assert current["success_evidence_id"] is None
    receipt = current["dossier"]["accepted_delegate_completion"]
    assert receipt["reservation_id"] == (await reservation(repair))["id"]
    assert receipt["head_sha"] == repair.head
    completions = await repair.db.get_task_completions(repair.task_id)
    assert completions[0].commits == [repair.head]


@pytest.mark.parametrize("repair", ["batch_debug"], indirect=True)
@pytest.mark.parametrize("crash_at", ["save_task_completion", "release_claim"])
async def test_accepted_close_replays_after_completion_before_claim_release(
    repair, monkeypatch, crash_at
):
    assert (await submit(repair))["outcome"] == "accepted"
    original_call = getattr(repair.db, crash_at)
    monkeypatch.setattr(repair.db, crash_at, AsyncMock(side_effect=RuntimeError("crash")))
    result = await close(repair)
    assert result.get("error") == "crash", result
    assert (await repair.db.get_task(repair.task_id)).status is TaskStatus.COMPLETED
    assert (await repair.db.get_session("repair-session")).task_id == repair.task_id
    receipt = (await stage(repair))["dossier"]["accepted_delegate_completion"]
    completions = await repair.db.get_task_completions(repair.task_id)
    assert len(completions) == (1 if crash_at == "release_claim" else 0)
    monkeypatch.setattr(repair.db, crash_at, original_call)
    # New service objects read all authority from Postgres after restart.
    result = await close(repair)
    assert result["success"], result
    assert (await repair.db.get_session("repair-session")).task_id is None
    assert (await stage(repair))["dossier"]["accepted_delegate_completion"] == receipt
    replayed = await repair.db.get_task_completions(repair.task_id)
    assert len(replayed) == 1
    if completions:
        assert replayed == completions


@pytest.mark.parametrize("repair", ["batch_debug"], indirect=True)
async def test_accepted_pending_ci_defers_original_deadline_after_restart(repair):  # noqa: F811
    assert (await submit(repair))["outcome"] == "accepted"
    before = await stage(repair)
    assert (await close(repair))["success"]
    for now in (before["deadline_at"], before["deadline_at"] + 600):
        result = await RepairService(repair.db).expire(
            "repair-batch-batch",
            before["ordinal"],
            now=now,
        )
        assert result["outcome"] == "not_due", result
        assert result["action"] == "wait"
    after = await stage(repair)
    for field in ("ordinal", "started_at", "deadline_at", "attempts", "state"):
        assert after[field] == before[field]
    owner = await repair.ownership.get_owner(repair.fence.target)
    dispatched = await RepairService(repair.db).dispatch("repair-batch-batch", before["ordinal"])
    assert dispatched["outcome"] == "already_dispatched", dispatched
    assert await repair.ownership.get_owner(repair.fence.target) == owner


@pytest.mark.parametrize("repair", ["batch_debug"], indirect=True)
async def test_old_reservation_cannot_close_a_new_claim(repair):  # noqa: F811
    assert (await submit(repair))["outcome"] == "accepted"
    proof = await reservation(repair)
    async with repair.db.immediate() as conn:
        await conn.execute(
            update(sessions)
            .where(sessions.c.id == "repair-session")
            .values(
                claim_phase_at=proof["created_at"] + 1,
            )
        )
    closed = await close(repair)
    assert not closed["success"], closed
    assert (await repair.db.get_task(repair.task_id)).status is TaskStatus.IN_PROGRESS
    assert (await repair.db.get_session("repair-session")).task_id == repair.task_id


async def configure_ci_and_promotion(repair, now):
    from src.database.tables import integration_batch_members
    from src.integration.ci import (
        CIService,
        CandidateCISubject,
        IntegrationCITrust,
        TrustedCIObservation,
        TrustedFixtureObserver,
    )
    from src.integration.main_promotion import RootAttestationProof, RootPromotionService
    from src.models import Task
    from tests.test_epic_train_end_to_end import _green_receipt

    receipt = _green_receipt(repair.head)
    receipt = receipt.model_copy(
        update={
            "producer_id": "forge",
            "checks": tuple(
                check.model_copy(update={"producer_id": "forge"}) for check in receipt.checks
            ),
        }
    )
    trust = IntegrationCITrust(
        canonical_repository_id="repo",
        repository_id=9,
        full_name="example/repo",
        producer_id="forge",
        required_checks={"version": "checks-v1", "names": ("unit",)},
    )
    ci = CIService(
        repair.db,
        trust,
        TrustedFixtureObserver(TrustedCIObservation(receipt, {22: 302})),
        clock=lambda: now,
    )

    class CIAdapter:
        async def handle_candidate_ci(self, row, _now):
            return await ci.observe_candidate(CandidateCISubject.model_validate(row))

    async def exact_attestation(subject):
        return RootAttestationProof(
            **subject.model_dump(),
            check_run_id=7001,
            external_id="aq-attestation-v1:" + "9" * 64,
        )

    repair.orchestrator.integration_attestation_service = CIAdapter()
    async with repair.db._engine.connect() as conn:
        members = (await conn.execute(select(integration_batch_members))).mappings().all()
    for member in members:
        if await repair.db.get_task(member["task_id"]) is None:
            await repair.db.create_task(
                Task(
                    id=member["task_id"],
                    project_id="p",
                    title="Frozen reviewed source",
                    description="",
                    status=TaskStatus.COMPLETED,
                    repo_id="repo",
                    branch_name=member["task_id"],
                )
            )
    await repair.db.update_repo("repo", url="https://github.com/example/repo.git")
    repair.orchestrator.root_promotion_service = RootPromotionService(
        repair.db,
        data_dir=repair.handler.config.data_dir,
        git_manager=repair.orchestrator.git,
        app_client=repair.app,
        attestation_resolver=exact_attestation,
        clock=lambda: now,
    )


@pytest.mark.parametrize("repair", ["batch_debug"], indirect=True)
async def test_public_submit_close_late_exact_ci_and_promotion(repair):  # noqa: F811
    accepted = await submit(repair)
    assert accepted["outcome"] == "accepted"
    original = await stage(repair)
    assert (await close(repair))["success"]
    late = original["deadline_at"] + 30
    assert (
        await RepairService(repair.db).expire(
            "repair-batch-batch",
            original["ordinal"],
            now=late,
        )
    )["outcome"] == "not_due"
    await configure_ci_and_promotion(repair, late)
    args = {"batch_id": "batch", "revision": accepted["continuation"]["revision"]}
    untested = await repair.handler.execute("integration_promote_main", args)
    assert untested["outcome"] == "ci_missing", untested
    observed = await repair.handler.execute("integration_ci_evidence", args)
    assert observed["outcome"] == "green", observed
    green_stage = await stage(repair)
    assert green_stage["state"] == "awaiting_completion"
    assert green_stage["deadline_at"] == original["deadline_at"]
    promoted = await repair.handler.execute("integration_promote_main", args)
    assert promoted["outcome"] == "promoted", promoted
    assert _git(repair.origin, "rev-parse", "refs/heads/main") == repair.head
    assert (await stage(repair))["state"] == "passed"


@pytest.mark.parametrize("repair", ["batch_debug"], indirect=True)
@pytest.mark.parametrize(
    "changed",
    [
        "pushed_only",
        "member_proof",
        "missing_handoff",
        "head",
        "revision",
        "subject",
        "deadline",
        "fence",
        "instance",
        "claim",
        "workspace",
        "human",
        "failure",
    ],
)
async def test_inexact_accepted_evidence_cannot_close_or_bypass_deadline(
    repair, changed, monkeypatch
):
    if changed == "pushed_only":
        from tests.test_integration_published_repair_handoff import retain_push

        await retain_push(repair, monkeypatch)
    else:
        assert (await submit(repair))["outcome"] == "accepted"
    before = await stage(repair)
    proof = await reservation(repair)
    async with repair.db.immediate() as conn:
        if changed == "member_proof":
            await conn.execute(
                update(integration_candidate_member_results).values(
                    conflict_evidence={"accepted_reservation_id": "other"},
                )
            )
        elif changed == "missing_handoff":
            await conn.execute(
                delete(integration_candidate_ref_mutations).where(
                    integration_candidate_ref_mutations.c.purpose == "repair_handoff",
                )
            )
        elif changed == "head":
            await conn.execute(update(integration_candidate_revisions).values(head_sha="f" * 40))
        elif changed == "revision":
            await conn.execute(update(integration_batches).values(current_revision=99))
        elif changed in {"subject", "deadline"}:
            await conn.execute(
                update(integration_repair_stages)
                .where(
                    integration_repair_stages.c.ordinal == before["ordinal"],
                )
                .values(
                    **(
                        {
                            "current_subject": {
                                **before["current_subject"],
                                "candidate_sha": "f" * 40,
                            }
                        }
                        if changed == "subject"
                        else {"deadline_at": before["deadline_at"] + 1}
                    )
                )
            )
        elif changed == "fence":
            await conn.execute(update(integration_branch_owners).values(fence_token=99))
        elif changed == "instance":
            await conn.execute(update(sessions).values(instance_token="replacement-instance"))
        elif changed == "claim":
            await conn.execute(
                update(tasks).where(tasks.c.id == repair.task_id).values(claim_epoch=99)
            )
        elif changed == "workspace":
            await conn.execute(
                update(workspaces)
                .where(workspaces.c.id == "repair-slot")
                .values(
                    locked_by_task_id=None,
                )
            )
        elif changed == "human":
            await conn.execute(update(integration_repair_operations).values(state="human_required"))
            await conn.execute(update(integration_batches).values(lifecycle="human_blocked"))
        elif changed == "failure":
            await conn.execute(
                insert(integration_check_evidence).values(
                    id="failed-ci",
                    operation_id="repair-batch-batch",
                    batch_id="batch",
                    candidate_revision=proof["revision"],
                    producer_id="forge",
                    workflow_id="unit",
                    run_id="failed-run",
                    attempt=1,
                    required_check_version="checks-v1",
                    checks={"unit": "failure"},
                    conclusion="failure",
                    classification="conclusive",
                    observed_at=before["deadline_at"] - 1,
                )
            )
    closed = await close(repair)
    assert not closed.get("success"), closed
    assert (await repair.db.get_task(repair.task_id)).status is TaskStatus.IN_PROGRESS
    assert (await repair.db.get_session("repair-session")).task_id == repair.task_id
    if changed not in {"instance", "claim", "workspace"}:
        expired = await RepairService(repair.db).expire(
            "repair-batch-batch",
            before["ordinal"],
            now=before["deadline_at"] + 10,
        )
        assert expired["outcome"] != "not_due", expired
        assert (await stage(repair))["deadline_at"] == (
            before["deadline_at"] + 1 if changed == "deadline" else before["deadline_at"]
        )


@pytest.mark.parametrize("repair", ["batch_continuous"], indirect=True)
@pytest.mark.parametrize("stopped,returned", [(False, False), (True, False), (True, True)])
async def test_original_accepted_delegate_retires_after_legacy_rollover(
    repair, monkeypatch, stopped, returned
):
    import src.integration.accepted_repair as accepted_repair

    assert (await submit(repair))["outcome"] == "accepted"
    before = await stage(repair)
    now = before["deadline_at"] + 1
    # Reproduce rows left by the deployed timeout path before this fix.
    with monkeypatch.context() as old_runtime:
        old_runtime.setattr(accepted_repair, "accepted_candidate_on", AsyncMock(return_value=None))
        expired = await RepairService(repair.db).expire(
            "repair-batch-batch", before["ordinal"], now=now
        )
    assert expired["action"] == "dispatch_debug"
    successor = await RepairService(repair.db).dispatch("repair-batch-batch", expired["stage"])
    assert successor["outcome"] == "dispatched", successor
    owner = await repair.ownership.get_owner(repair.fence.target)
    assert owner["owner_id"] == successor["repair_task_id"]
    if returned:
        await configure_ci_and_promotion(repair, now)
        green = await repair.handler.execute(
            "integration_ci_evidence",
            {"batch_id": "batch", "revision": 0},
        )
        assert green["outcome"] == "green", green
        # Model the successor close's already-tested terminal self-transfer;
        # exercise the real exact-green branch-return service and its receipt.
        async with repair.db.immediate() as conn:
            await repair.db._apply_transition(
                conn,
                successor["repair_task_id"],
                TaskStatus.COMPLETED,
                context="test_successor_closed",
                force=True,
                assigned_agent_id=None,
            )
        assert (
            await RepairService(repair.db).return_green_delegate_branch(
                "batch",
                now=now,
            )
            is not None
        )
        owner = await repair.ownership.get_owner(repair.fence.target)
        assert owner["owner_role"] == "collector"
        assert owner["fence_token"] > (await reservation(repair))["handoff_fence_token"]
        async with repair.db.immediate() as conn:
            successor_stage = (
                (
                    await conn.execute(
                        select(integration_repair_stages).where(
                            integration_repair_stages.c.repair_task_id
                            == successor["repair_task_id"],
                        )
                    )
                )
                .mappings()
                .one()
            )
            await conn.execute(
                update(integration_repair_stages)
                .where(
                    integration_repair_stages.c.repair_task_id == successor["repair_task_id"],
                )
                .values(dossier={**successor_stage["dossier"], "green_handoffs": []})
            )
        # A newer collector fence alone is insufficient authority.
        assert not (await close(repair)).get("success")
        assert (await repair.db.get_session("repair-session")).task_id == repair.task_id
        async with repair.db.immediate() as conn:
            await conn.execute(
                update(integration_repair_stages)
                .where(
                    integration_repair_stages.c.repair_task_id == successor["repair_task_id"],
                )
                .values(dossier=successor_stage["dossier"])
            )
    if stopped:
        from src.claim_file import read_claim_file, write_claim_file

        write_claim_file(
            str(repair.checkout),
            {
                "task_id": repair.task_id,
                "claim_epoch": repair.args["claim_epoch"],
            },
        )
        async with repair.db.immediate() as conn:
            await conn.execute(
                update(sessions)
                .where(sessions.c.id == "repair-session")
                .values(
                    state="stopped",
                    desired_state="stopped",
                )
            )
        assert await RepairService(repair.db).reconcile_accepted_delegates(now) == [repair.task_id]
        assert await RepairService(repair.db).reconcile_accepted_delegates(now) == []
        assert read_claim_file(str(repair.checkout)) is None
        assert (await repair.db.get_workspace("repair-slot")).locked_by_agent_id is None
        completions = await repair.db.get_task_completions(repair.task_id)
        assert len(completions) == 1 and completions[0].commits == [repair.head]
    else:
        # A reconciler cannot complete a live agent's task on its behalf.
        assert await RepairService(repair.db).reconcile_accepted_delegates(now) == []
        assert (await close(repair))["success"]
    assert (await repair.db.get_task(repair.task_id)).status is TaskStatus.COMPLETED
    assert (await repair.db.get_session("repair-session")).task_id is None
    assert await repair.ownership.get_owner(repair.fence.target) == owner
    current = await stage(repair)
    assert current["state"] == "expired"
    assert current["deadline_at"] == before["deadline_at"]
    assert (await repair.db.get_task(successor["repair_task_id"])).status is (
        TaskStatus.COMPLETED if returned else TaskStatus.READY
    )


@pytest.mark.parametrize("repair", ["batch_debug"], indirect=True)
async def test_accepted_original_close_after_exact_promotion(repair):
    assert (await submit(repair))["outcome"] == "accepted"
    before = await stage(repair)
    await configure_ci_and_promotion(repair, before["deadline_at"] + 30)
    args = {"batch_id": "batch", "revision": 0}
    assert (await repair.handler.execute("integration_ci_evidence", args))["outcome"] == "green"
    promoted = await repair.handler.execute("integration_promote_main", args)
    assert promoted["outcome"] == "promoted", promoted
    closed = await close(repair)
    assert closed["success"], closed
    assert (await repair.db.get_session("repair-session")).task_id is None
    assert (await stage(repair))["state"] == "passed"
    assert (await stage(repair))["deadline_at"] == before["deadline_at"]
