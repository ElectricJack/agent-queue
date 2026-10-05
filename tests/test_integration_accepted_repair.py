# ruff: noqa: F811
"""Accepted root repairs finish their original claims independently of candidate CI."""

from dataclasses import replace

import pytest
from sqlalchemy import select, update

from src.commands.principal import principal_context
from src.database.tables import (
    integration_repair_stages,
    sessions,
)
from src.models import TaskStatus
from src.profiles.capabilities import CapabilityPolicy
from tests.test_integration_published_repair_handoff import (  # noqa: F401
    repair,
    reservation,
    submit,
)


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
async def test_old_reservation_cannot_close_a_new_claim(repair):
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
        CandidateCISubject,
        CIService,
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

