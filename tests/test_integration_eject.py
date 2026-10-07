"""Manual ejection preserves review approval and restores future eligibility."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from src.cli.integration import integration as integration_cli
from src.database.tables import (
    integration_batch_members,
    integration_batches,
    integration_candidate_revisions,
    integration_candidate_member_results,
)
from src.integration.scheduler import TrainService
from src.integration.repair import RepairService
from src.models import Project, RepoConfig, RepoSourceType
from tests.test_integration_sealing import _enable_train, _request, _seed_leaf


async def _start_repair(db, batch):
    batch_id = batch["batch_id"]
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_candidate_revisions).values(
                batch_id=batch_id,
                revision=0,
                construction_base_sha="a" * 40,
                head_sha="b" * 40,
                state="red",
                created_at=21.0,
                updated_at=21.0,
            )
        )
        await conn.execute(
            insert(integration_candidate_member_results).values(
                batch_id=batch_id,
                revision=0,
                member_ordinal=0,
                input_head_sha="1" * 40,
                input_tree_sha="c" * 40,
                result="applied",
                generated_squash_sha="d" * 40,
                created_at=21.0,
                updated_at=21.0,
            )
        )
        await conn.execute(
            update(integration_batches)
            .where(
                integration_batches.c.id == batch_id,
            )
            .values(lifecycle="repairing", tested_candidate_sha="b" * 40, ci_evidence_id="old-ci")
        )
    started = await RepairService(db, clock=lambda: 21.0).start(
        batch["operation_id"],
        "b" * 40,
        batch_id,
        now=21.0,
    )
    assert started["outcome"] == "started"














@pytest.fixture
async def db(reuse_database):
    database = await reuse_database("integration-eject.db")
    await database.create_project(Project(id="p", name="integration project"))
    await database.create_repo(
        RepoConfig(id="repo", project_id="p", source_type=RepoSourceType.LINK, default_branch="main")
    )
    yield database




@pytest.fixture
async def sealed_batch(db):
    await _enable_train(db)
    await _seed_leaf(db, "e1", "1" * 40)
    await _seed_leaf(db, "e2", "2" * 40)
    request = await _request(db)
    batch = await TrainService(db).seal("p", request["request_id"], 20.0)
    assert batch["outcome"] == "sealed"
    return batch


@pytest.fixture
def members_of(db):
    async def read(batch_id: str) -> list[str]:
        async with db._engine.connect() as conn:
            rows = await conn.execute(
                select(integration_batch_members.c.task_id)
                .where(integration_batch_members.c.batch_id == batch_id)
                .order_by(integration_batch_members.c.ordinal)
            )
            return list(rows.scalars())

    return read


@pytest.fixture
def eligible_task_ids(db):
    async def read(project_id: str) -> list[str]:
        async with db._engine.connect() as conn:
            page = await db.eligible_root_page_on(
                conn, project_id=project_id, repository_id="repo", after=None, limit=100
            )
        return [row["task_id"] for row in page]

    return read




async def test_matching_eject_setting_alone_cannot_edit_a_sealed_batch(sealed_batch, db):
    batch_id = sealed_batch["batch_id"]
    async with db.immediate() as conn:
        await conn.execute(select(func.set_config("aq.integration_eject_batch", batch_id, True)))
        with pytest.raises((IntegrityError, DBAPIError)):
            async with conn.begin_nested():
                await conn.execute(
                    delete(integration_batch_members).where(
                        integration_batch_members.c.batch_id == batch_id,
                        integration_batch_members.c.task_id == "e2",
                    )
                )
        with pytest.raises((IntegrityError, DBAPIError)):
            async with conn.begin_nested():
                await conn.execute(
                    update(integration_batches)
                    .where(integration_batches.c.id == batch_id)
                    .values(source_manifest_digest="forged")
                )


async def test_forged_eject_event_without_project_lock_cannot_edit_members(sealed_batch, db):
    batch_id = sealed_batch["batch_id"]
    with pytest.raises((IntegrityError, DBAPIError)):
        async with db.immediate() as conn:
            event_id = await db.log_event(
                "integration.batch_ejected",
                project_id="p",
                task_id="e2",
                payload=json.dumps(
                    {"batch_id": batch_id, "reason": "forged", "operator_id": "raw-sql"}
                ),
                conn=conn,
            )
            await conn.execute(select(func.set_config("aq.integration_eject_batch", batch_id, True)))
            await conn.execute(select(func.set_config("aq.integration_eject_event", str(event_id), True)))
            await conn.execute(
                delete(integration_batch_members).where(
                    integration_batch_members.c.batch_id == batch_id,
                    integration_batch_members.c.task_id == "e2",
                )
            )






















@pytest.mark.parametrize("argv,message", [
    (["--task", "task"], "Missing option '--batch'"),
    (["--batch", "batch"], "Missing option '--task'"),
    (["--batch", "batch", "--task", "task", "--apply"],
     "--apply needs a nonblank --reason"),
    (["--batch", "batch", "--task", "task", "--apply", "--reason", "   "],
     "--apply needs a nonblank --reason"),
])
def test_cli_ejection_requires_identity_and_apply_reason_before_transport(argv, message):
    with patch("src.cli.integration._get_client") as get_client:
        result = CliRunner().invoke(integration_cli, ["eject", *argv])
    assert result.exit_code == 2, result.output
    assert message in result.output
    get_client.assert_not_called()


@pytest.mark.parametrize("apply", [False, True])
def test_cli_ejection_command_exists_and_refuses_workers(monkeypatch, apply):
    from src.api.auth import LOCAL_SCOPE, RequestScope
    from src.api.scope import check_command_scope
    from src.cli.exceptions import ScopeDeniedError

    result = CliRunner().invoke(integration_cli, ["eject", "--help"])
    assert result.exit_code == 0, result.output
    worker = RequestScope(kind="session", session_id="worker", project_id="p", task_id="e2")

    async def execute(command, args):
        refusal = check_command_scope(command, args, worker)
        if refusal:
            raise ScopeDeniedError(command, refusal)
        return {"success": True}

    client = AsyncMock()
    client.__aenter__.return_value = client
    client.__aexit__.return_value = False
    client.execute.side_effect = execute
    monkeypatch.setattr("src.cli.integration._get_client", lambda _api_url: client)
    argv = ["eject", "--batch", "batch", "--task", "e2"]
    if apply:
        argv += ["--apply", "--reason", "isolate e2"]
    result = CliRunner().invoke(integration_cli, argv)
    assert result.exit_code == 4, result.output
    assert "local operator or supervisor" in result.output
    client.execute.assert_awaited_once_with("integration_eject", {
        "batch_id": "batch", "task_id": "e2", "reason": "isolate e2" if apply else "",
        "dry_run": not apply,
    })
    for scope in (LOCAL_SCOPE, RequestScope(kind="session", session_id="supervisor",
                                          project_id="p", elevated=True)):
        assert check_command_scope("integration_eject", {}, scope) is None


@pytest.fixture(autouse=True)
def reconciler_primitive_authority(monkeypatch):
    from tests.integration_primitive_scope import authorize_root_primitives

    authorize_root_primitives(monkeypatch)
