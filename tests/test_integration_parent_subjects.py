"""Parent subject bridge pins exact lineage and refuses mismatched identity."""

from __future__ import annotations


import pytest
from sqlalchemy import update

from src.database import tables as t
from src.integration.parent_subjects import (
    ParentSubjectAdapter,
    parent_subject_from_rows,
)
from src.integration.runtime_contracts import (
    PolicyArtifactPin,
)
from src.models import Project
from tests.test_integration_cancelled_collection import (  # noqa: F401
    _policy,
    _rows,
    case,
)
from tests.test_integration_parent_completion import _artifact, _parent_tree

BASE, SOURCE, FIRST, HEAD = (char * 40 for char in "abcd")
PIN = PolicyArtifactPin(
    playbook_id=_artifact().playbook_id, artifact_sha256=_artifact().artifact_sha256
)


@pytest.fixture
async def db(reuse_database):
    database = await reuse_database("parent-subjects")
    await database.create_project(Project(id="p", name="parent subjects"))
    return database


async def bridge(db, task_id="parent", pin=PIN):
    async with db.immediate() as conn:
        return await ParentSubjectAdapter(db, clock=lambda: 1000).ensure_on(
            conn, task_id, policy=pin, max_wait_seconds=300
        )


async def test_bridge_replay_pins_policy_schedule_and_episode_after_generation_moves(db):
    await _parent_tree(db, children=1)
    subject, _ = await bridge(db)
    async with db.immediate() as conn:
        await conn.execute(
            update(t.task_integration_checkpoints)
            .where(t.task_integration_checkpoints.c.task_id == "parent")
            .values(generation=t.task_integration_checkpoints.c.generation + 1)
        )
    replay, created = await bridge(db)
    assert not created and replay == subject


@pytest.mark.parametrize("changed", ["repository", "episode", "generation", "branch", "operation"])
def test_bridge_rejects_mismatched_parent_identity(changed):
    parent = {"id": "parent", "project_id": "p", "repo_id": "repo", "branch_name": "aq/parent"}
    checkpoint = {
        "task_id": "parent",
        "repository_id": "repo",
        "branch": "aq/parent",
        "episode_id": "episode",
        "generation": 3,
        "checkpoint_sha": HEAD,
        "state": "awaiting_children",
    }
    episode = {
        "id": "episode",
        "parent_task_id": "parent",
        "repository_id": "repo",
        "generation": 2,
        "pre_collection_checkpoint_sha": BASE,
    }
    operation = {"parent_task_id": "parent", "episode_id": "episode", "target_kind": "parent"}
    if changed == "repository":
        episode["repository_id"] = "other"
    elif changed == "episode":
        checkpoint["episode_id"] = "other"
    elif changed == "generation":
        checkpoint["generation"] = 1
    elif changed == "branch":
        parent["branch_name"] = "aq/other"
    else:
        operation["parent_task_id"] = "other"
    with pytest.raises(ValueError, match="identity mismatch"):
        parent_subject_from_rows(
            parent, checkpoint, episode, operation, policy=PIN, now=1000, max_wait_seconds=300
        )


@pytest.mark.parametrize("dry_run", [False, True])
async def test_parent_repair_head_recovery_refuses_reconciler_owned_parent(db, tmp_path, dry_run):
    from src.database.tables import integration_repair_stages, task_integration_checkpoints
    from sqlalchemy import select
    from tests.test_integration_parent_completion import _parent_repair_case

    recovery, _, request, _, _, _ = await _parent_repair_case(db, tmp_path)
    subject, _ = await bridge(db)
    async with db.immediate() as conn:
        await conn.execute(
            update(t.integration_subjects)
            .where(t.integration_subjects.c.id == subject.id)
            .values(engine="reconciler")
        )
        before = dict(
            (
                await conn.execute(
                    select(task_integration_checkpoints).where(
                        task_integration_checkpoints.c.task_id == "parent"
                    )
                )
            )
            .mappings()
            .one()
        )
        stage_before = dict(
            (
                await conn.execute(
                    select(integration_repair_stages).where(
                        integration_repair_stages.c.operation_id == request.operation_id
                    )
                )
            )
            .mappings()
            .one()
        )
    request.dry_run = dry_run
    result = await recovery.run(request, principal="operator:test")
    assert result["outcome"] == "blocked", result
    assert "reconciler" in result["reason"]
    async with db.immediate() as conn:
        assert (
            dict(
                (
                    await conn.execute(
                        select(task_integration_checkpoints).where(
                            task_integration_checkpoints.c.task_id == "parent"
                        )
                    )
                )
                .mappings()
                .one()
            )
            == before
        )
        assert (
            dict(
                (
                    await conn.execute(
                        select(integration_repair_stages).where(
                            integration_repair_stages.c.operation_id == request.operation_id
                        )
                    )
                )
                .mappings()
                .one()
            )
            == stage_before
        )
