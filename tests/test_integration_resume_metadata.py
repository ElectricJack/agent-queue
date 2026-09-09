import pytest
from sqlalchemy import insert, select, update
from src.database.tables import (
    integration_batches,
    integration_branch_owners,
    integration_candidate_publications,
    integration_candidate_ref_mutations,
    integration_candidate_revisions,
    integration_repair_operations,
    integration_repair_stages,
)
from src.integration.models import RepairPolicy
from src.integration.controls import IntegrationControlService
from tests.test_integration_operational_controls import db, _policy, _artifact  # noqa: F401


async def seed_publication(database):
    db = database  # noqa: F811
    repair_policy = RepairPolicy(
        primary_seconds=60,
        debug_seconds=120,
        debug_intelligence_class="deep",
        debug_profile_id="debugger",
    ).model_dump(mode="json")
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_batches).values(
                id="human-batch",
                project_id="p",
                repository_id="repo",
                request_id="human-request",
                trigger="manual",
                source_manifest_digest="sha256:" + "a" * 64,
                base_sha="b" * 40,
                lifecycle="human_blocked",
                integration_branch="refs/heads/aq/integration/human",
                repair_stage_ordinal=1,
                policy_snapshot=_policy(),
                artifact_snapshot=_artifact().model_dump(mode="json"),
                cleanup_state="pending",
                created_at=50.0,
                updated_at=50.0,
            )
        )
        await conn.execute(
            insert(integration_candidate_revisions).values(
                batch_id="human-batch",
                revision=0,
                construction_base_sha="b" * 40,
                state="red",
                created_at=50.0,
                updated_at=50.0,
            )
        )
        await conn.execute(
            insert(integration_repair_operations).values(
                id="human-operation",
                target_kind="batch",
                batch_id="human-batch",
                episode_id="episode",
                active_stage=1,
                state="human_required",
                policy_snapshot=_policy(),
                artifact_snapshot=_artifact().model_dump(mode="json"),
                required_check_version="checks-v1",
                created_at=50.0,
                updated_at=50.0,
            )
        )
        await conn.execute(
            insert(integration_branch_owners).values(
                id="human-resume-collector",
                repository_id="repo",
                ref="refs/heads/aq/integration/human",
                owner_id="human-operation",
                owner_role="collector",
                fence_token=1,
                handoff_state="reserved",
                created_at=50.0,
                updated_at=50.0,
            )
        )
        await conn.execute(
            insert(integration_repair_stages).values(
                operation_id="human-operation",
                ordinal=1,
                policy=repair_policy,
                intelligence_class="deep",
                profile_id="debugger",
                starting_sha="b" * 40,
                attempts=3,
                dossier={},
                state="failed",
                completed_at=50.0,
            )
        )
        await conn.execute(
            insert(integration_candidate_publications).values(
                batch_id="human-batch",
                revision=0,
                state="reserved",
                repository_id="repo",
                repository_numeric_id=1234,
                repository_full_name="acme/widgets",
                base_ref="main",
                head_ref="aq/integration/human",
                head_sha="c" * 40,
                expected_old_sha="b" * 40,
                idempotency_key="human-publication",
                created_at=50.0,
                updated_at=50.0,
            )
        )


@pytest.mark.parametrize(
    "case", ["exact", "missing", "remote", "old", "branch", "episode", "reserved", "ref_published"]
)
async def test_resume_only_metadata_after_exact_applied_ref(db, case):  # noqa: F811
    await seed_publication(db)
    state = case if case in {"reserved", "ref_published"} else "pr_reserved"
    async with db.immediate() as conn:
        if state != "reserved":
            await conn.execute(
                update(integration_candidate_publications).values(state="ref_published")
            )
        if state == "pr_reserved":
            await conn.execute(
                update(integration_candidate_publications).values(state="pr_reserved")
            )
        if case != "missing":
            await conn.execute(
                insert(integration_candidate_ref_mutations).values(
                    id="publication-mutation",
                    batch_id="human-batch",
                    revision=0,
                    purpose="candidate_final",
                    repository_id="repo",
                    branch="refs/heads/aq/integration/human",
                    target_branch="refs/heads/aq/integration/other"
                    if case == "branch"
                    else "refs/heads/aq/integration/human",
                    expected_old_sha=("d" if case == "old" else "b") * 40,
                    desired_sha=("d" if case == "remote" else "c") * 40,
                    remote_sha=("d" if case == "remote" else "c") * 40,
                    operation_id="human-operation",
                    operation_episode_id="wrong" if case == "episode" else "episode",
                    operation_stage=0,
                    lease_owner_id="lease",
                    lease_fence_token=1,
                    branch_owner_id="collector",
                    branch_owner_role="collector",
                    branch_fence_token=1,
                    nonce="nonce",
                    state="applied",
                    expires_at=55,
                    created_at=50,
                    updated_at=50,
                )
            )
    service = IntegrationControlService(db, clock=lambda: 60)
    aborted = await service.abort("human-operation", reason="test")
    assert aborted["outcome"] == "ambiguous"
    resumed = await service.resume("human-operation")
    assert resumed["outcome"] == ("resumed" if case == "exact" else "ambiguous")
    async with db._engine.connect() as conn:
        assert (
            await conn.execute(select(integration_candidate_publications.c.state))
        ).scalar_one() == state
