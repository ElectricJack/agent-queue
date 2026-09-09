# ruff: noqa: F811
"""A pushed batch CI fix becomes a new exact candidate, never rewrites evidence."""

import pytest
from sqlalchemy import insert, select, update

from tests.test_integration_repair import db, _seed_root_operation, STARTING_SHA  # noqa: F401
from src.database.tables import (
    integration_batches,
    integration_branch_owners,
    integration_candidate_revisions,
    integration_candidate_member_results,
    integration_repair_stages,
)
from tests.test_integration_candidates import db as candidate_db  # noqa: F401
from src.integration.models import BranchKey, Fence
from src.integration.repair import RepairService


@pytest.mark.asyncio
async def test_batch_ci_fix_gets_new_revision_and_keeps_old_evidence(db):
    operation_id = await _seed_root_operation(db)
    service = RepairService(db)
    await service.start(operation_id, STARTING_SHA, "batch", now=100)
    head = "b" * 40
    proof = {"base_sha": STARTING_SHA, "head_sha": head, "commits": [head]}
    async with db.immediate() as conn:
        await service.adopt_batch_repair_on(
            conn, operation_id, head_sha=head, commit_proof=proof, now=110
        )
    async with db._engine.connect() as conn:
        revisions = (
            (
                await conn.execute(
                    select(integration_candidate_revisions).order_by(
                        integration_candidate_revisions.c.revision
                    )
                )
            )
            .mappings()
            .all()
        )
        stage = (await conn.execute(select(integration_repair_stages))).mappings().one()
        batch = (await conn.execute(select(integration_batches))).mappings().one()
        members = (
            (await conn.execute(select(integration_candidate_member_results))).mappings().all()
        )
    assert [(r["head_sha"], r["state"]) for r in revisions] == [
        (STARTING_SHA, "superseded"),
        (head, "built"),
    ]
    assert revisions[1]["repair_parent_revision"] == 0
    assert revisions[1]["ci_evidence_id"] is None
    assert batch["current_revision"] == 1
    assert batch["tested_candidate_sha"] is None
    assert stage["current_subject"] == {"kind": "batch", "revision": 1, "candidate_sha": head}
    assert stage["deadline_at"] == 130
    assert stage["attempts"] == 0
    assert stage["dossier"]["repair_commits"] == [head]
    assert len(members) == 0
    async with db.immediate() as conn:
        await service.adopt_batch_repair_on(
            conn,
            operation_id,
            head_sha=head,
            commit_proof={"base_sha": head, "head_sha": head, "commits": []},
            now=111,
        )
    async with db._engine.connect() as conn:
        assert len((await conn.execute(select(integration_candidate_revisions))).all()) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "proof", [None, {"base_sha": "d" * 40, "head_sha": "b" * 40, "commits": ["b" * 40]}]
)
async def test_batch_ci_fix_rejects_missing_or_stale_lineage(db, proof):
    operation_id = await _seed_root_operation(db)
    service = RepairService(db)
    await service.start(operation_id, STARTING_SHA, "batch", now=100)
    with pytest.raises(ValueError):
        async with db.immediate() as conn:
            await service.adopt_batch_repair_on(
                conn, operation_id, head_sha="b" * 40, commit_proof=proof, now=110
            )
    async with db._engine.connect() as conn:
        row = (await conn.execute(select(integration_batches))).mappings().one()
    assert row["current_revision"] == 0


@pytest.mark.asyncio
async def test_batch_rebuild_repair_rejects_non_merge_or_wrong_parent(db):
    operation_id = await _seed_root_operation(db)
    service = RepairService(db)
    await service.start(operation_id, STARTING_SHA, "batch", now=100)
    new_base = "b" * 40
    resolved = "c" * 40
    async with db.immediate() as conn:
        stage = (
            await conn.execute(select(integration_repair_stages).with_for_update())
        ).mappings().one()
        dossier = dict(stage["dossier"])
        dossier["candidate_rebuild_conflict"] = {
            "kind": "candidate_rebuild",
            "id": "frozen-conflict",
            "operation_id": operation_id,
            "operation_stage": 0,
            "batch_id": "batch",
            "revision": 0,
            "candidate_sha": STARTING_SHA,
            "new_base_sha": new_base,
        }
        await conn.execute(update(integration_repair_stages).values(dossier=dossier))
    for parents in ([STARTING_SHA], [STARTING_SHA, "d" * 40]):
        with pytest.raises(ValueError, match="exact ancestry-preserving merge"):
            async with db.immediate() as conn:
                await service.adopt_batch_repair_on(
                    conn,
                    operation_id,
                    head_sha=resolved,
                    commit_proof={
                        "base_sha": STARTING_SHA,
                        "head_sha": resolved,
                        "commits": [resolved],
                        "head_parents": parents,
                    },
                    now=110,
                )
    async with db._engine.connect() as conn:
        batch = (await conn.execute(select(integration_batches))).mappings().one()
    assert batch["current_revision"] == 0


@pytest.mark.asyncio
async def test_overdue_batch_rebuild_conflict_escalates_without_resetting_budget(db):
    operation_id = await _seed_root_operation(db)
    service = RepairService(db)
    started = await service.start(operation_id, STARTING_SHA, "batch", now=100)
    target = BranchKey(repository_id="repo", branch="aq/integration/batch")
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_branch_owners).values(
                id="root-rebuild-owner",
                repository_id="repo",
                ref=target.branch,
                owner_id=operation_id,
                owner_role="collector",
                fence_token=1,
                handoff_state="reserved",
                created_at=100,
                updated_at=100,
            )
        )
        recorded = await service.record_batch_rebuild_conflict_on(
            conn,
            operation_id,
            revision_number=0,
            candidate_sha=STARTING_SHA,
            new_base_sha="b" * 40,
            diagnostics="overlap",
            fence=Fence(target=target, owner_id=operation_id, token=1),
            now=131,
        )
    assert recorded["deadline_due"] is True
    expired = await service.expire(operation_id, 0, now=131)
    assert expired["outcome"] == "expired"
    assert expired["action"] == "dispatch_debug"
    async with db._engine.connect() as conn:
        stages = (
            (
                await conn.execute(
                    select(integration_repair_stages).order_by(
                        integration_repair_stages.c.ordinal
                    )
                )
            )
            .mappings()
            .all()
        )
    assert stages[0]["started_at"] == started["started_at"]
    assert stages[0]["deadline_at"] == started["deadline_at"]
    assert stages[0]["attempts"] == 0
    assert stages[0]["state"] == "expired"
    assert stages[1]["state"] == "active"
    assert stages[1]["dossier"]["candidate_rebuild_conflict"]["new_base_sha"] == "b" * 40


@pytest.mark.asyncio
async def test_batch_ci_fix_copies_frozen_member_results(candidate_db):
    from sqlalchemy import insert
    from tests.test_integration_candidates import _seed_batch

    await _seed_batch(
        candidate_db, lifecycle="testing", members=[(STARTING_SHA, "c" * 40, "d" * 40)]
    )
    async with candidate_db.immediate() as conn:
        await conn.execute(
            insert(integration_candidate_revisions).values(
                batch_id="batch",
                revision=0,
                construction_base_sha=STARTING_SHA,
                next_member_ordinal=1,
                head_sha=STARTING_SHA,
                state="built",
                created_at=1,
                updated_at=1,
            )
        )
        await conn.execute(
            insert(integration_candidate_member_results).values(
                batch_id="batch",
                revision=0,
                member_ordinal=0,
                input_head_sha="c" * 40,
                input_tree_sha="d" * 40,
                generated_squash_sha=STARTING_SHA,
                result="applied",
                created_at=1,
                updated_at=1,
            )
        )
    service = RepairService(candidate_db)
    await service.start("repair-batch-batch", STARTING_SHA, "batch", now=100)
    async with candidate_db.immediate() as conn:
        await service.adopt_batch_repair_on(
            conn,
            "repair-batch-batch",
            head_sha="b" * 40,
            commit_proof={"base_sha": STARTING_SHA, "head_sha": "b" * 40, "commits": ["b" * 40]},
            now=110,
        )
    async with candidate_db._engine.connect() as conn:
        members = (
            (
                await conn.execute(
                    select(integration_candidate_member_results).order_by(
                        integration_candidate_member_results.c.revision
                    )
                )
            )
            .mappings()
            .all()
        )
    assert len(members) == 2
    assert {
        k: v for k, v in members[0].items() if k not in {"revision", "created_at", "updated_at"}
    } == {k: v for k, v in members[1].items() if k not in {"revision", "created_at", "updated_at"}}
