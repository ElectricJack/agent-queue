"""Manual ejection preserves review approval and restores future eligibility."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from src.cli.integration import integration as integration_cli
from src.commands.integration_commands import IntegrationCommandsMixin
from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
from src.database.tables import (
    events,
    integration_batch_members,
    integration_batches,
    integration_review_evidence,
)
from src.integration.controls import IntegrationControlService
from src.integration.candidates import CandidateService
from src.integration.scheduler import IntegrationScheduler, TrainService
from src.models import Project, RepoConfig, RepoSourceType
from src.profiles.capabilities import DENY_ALL
from tests.test_integration_sealing import _enable_train, _request, _seed_leaf


@pytest.fixture
async def db(reuse_database):
    database = await reuse_database("integration-eject.db")
    await database.create_project(Project(id="p", name="integration project"))
    await database.create_repo(
        RepoConfig(id="repo", project_id="p", source_type=RepoSourceType.LINK, default_branch="main")
    )
    yield database


@pytest.fixture
def control_service(db):
    return IntegrationControlService(db, clock=lambda: 30.0)


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


async def test_eject_removes_the_member_and_records_the_reason(control_service, sealed_batch, db):
    batch_id = sealed_batch["batch_id"]
    result = await control_service.eject(
        batch_id, task_id="e2", reason="cannot go green", operator_id="supervisor"
    )
    assert result == {
        "outcome": "ejected",
        "batch_id": batch_id,
        "task_id": "e2",
        "reason": "cannot go green",
    }
    events = await db.get_recent_events(event_type="integration.batch_ejected", project_id="p")
    assert len(events) == 1
    assert events[0]["task_id"] == "e2"
    assert json.loads(events[0]["payload"]) == {
        "batch_id": batch_id,
        "reason": "cannot go green",
        "operator_id": "supervisor",
        "at": 30.0,
    }


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


async def test_prior_eject_event_cannot_authorize_a_later_manifest_edit(
    control_service, sealed_batch, db
):
    batch_id = sealed_batch["batch_id"]
    await control_service.eject(batch_id, task_id="e2", reason="manual", operator_id="supervisor")
    async with db.immediate() as conn:
        event_id = (
            await conn.execute(
                select(events.c.id)
                .where(events.c.event_type == "integration.batch_ejected")
                .order_by(events.c.id.desc())
                .limit(1)
            )
        ).scalar_one()
        await db.lock_hierarchy_project(conn, "p")
        await conn.execute(select(func.set_config("aq.integration_eject_batch", batch_id, True)))
        await conn.execute(select(func.set_config("aq.integration_eject_event", str(event_id), True)))
        with pytest.raises((IntegrityError, DBAPIError)):
            async with conn.begin_nested():
                await conn.execute(
                    update(integration_batches)
                    .where(integration_batches.c.id == batch_id)
                    .values(source_manifest_digest="forged")
                )


async def test_the_remaining_members_stay_in_the_batch(control_service, sealed_batch, members_of):
    await control_service.eject(
        sealed_batch["batch_id"], task_id="e2", reason="cannot go green", operator_id="supervisor"
    )
    assert await members_of(sealed_batch["batch_id"]) == ["e1"]


async def test_ejecting_the_first_member_compacts_ordinals_and_refreshes_manifest(
    control_service, sealed_batch, db
):
    batch_id = sealed_batch["batch_id"]
    async with db._engine.connect() as conn:
        original_digest = (
            await conn.execute(
                select(integration_batches.c.source_manifest_digest).where(
                    integration_batches.c.id == batch_id
                )
            )
        ).scalar_one()
    await control_service.eject(
        batch_id, task_id="e1", reason="manual", operator_id="supervisor"
    )
    async with db._engine.connect() as conn:
        row = (
            await conn.execute(
                select(integration_batch_members.c.task_id, integration_batch_members.c.ordinal)
                .where(integration_batch_members.c.batch_id == batch_id)
            )
        ).one()
        batch = (
            await conn.execute(select(integration_batches).where(integration_batches.c.id == batch_id))
        ).mappings().one()
    assert row == ("e2", 0)
    assert batch["source_manifest_digest"] != original_digest
    assert batch["lifecycle"] == "sealed"


async def test_an_ejected_epic_keeps_its_approval_and_is_eligible_again(
    control_service, sealed_batch, eligible_task_ids, db
):
    await control_service.eject(
        sealed_batch["batch_id"], task_id="e2", reason="cannot go green", operator_id="supervisor"
    )
    async with db._engine.connect() as conn:
        review = (
            await conn.execute(
                select(integration_review_evidence.c.verdict).where(
                    integration_review_evidence.c.source_task_id == "e2"
                )
            )
        ).scalar_one()
    assert review == "approved"
    assert "e2" in await eligible_task_ids("p")


async def test_ejecting_an_absent_member_is_refused(control_service, sealed_batch, members_of):
    batch_id = sealed_batch["batch_id"]
    assert await control_service.eject(
        batch_id, task_id="nope", reason="typo", operator_id="supervisor"
    ) == {"outcome": "not_a_member", "batch_id": batch_id, "task_id": "nope"}
    assert await members_of(batch_id) == ["e1", "e2"]


async def test_ejecting_the_last_member_empties_the_batch(
    control_service, sealed_batch, members_of, eligible_task_ids, db
):
    batch_id = sealed_batch["batch_id"]
    for task_id in ("e1", "e2"):
        await control_service.eject(
            batch_id, task_id=task_id, reason="drain", operator_id="supervisor"
        )
    assert await members_of(batch_id) == []
    async with db._engine.connect() as conn:
        batch = (
            await conn.execute(
                select(integration_batches).where(integration_batches.c.id == batch_id)
            )
        ).mappings().one()
    assert batch["lifecycle"] == "aborted"
    assert batch["cleanup_state"] == "complete"
    assert await eligible_task_ids("p") == ["e1", "e2"]
    next_request = await IntegrationScheduler(db).mark_due("p", 40.0, "manual")
    next_batch = await TrainService(db).seal("p", next_request["request_id"], 50.0)
    assert next_batch["outcome"] == "sealed"
    assert await members_of(next_batch["batch_id"]) == ["e1", "e2"]


async def test_late_candidate_build_observes_the_terminal_ejected_batch(
    control_service, sealed_batch, db, tmp_path
):
    batch_id = sealed_batch["batch_id"]
    for task_id in ("e1", "e2"):
        await control_service.eject(
            batch_id, task_id=task_id, reason="drain", operator_id="supervisor"
        )
    candidate = CandidateService(db, data_dir=tmp_path, git_manager=None)
    result = await candidate.build(batch_id)
    assert result.outcome == "empty"
    assert result.batch_id == batch_id


async def test_eject_refuses_a_batch_that_has_started_building(
    control_service, sealed_batch, members_of, db
):
    batch_id = sealed_batch["batch_id"]
    async with db.immediate() as conn:
        await conn.execute(
            update(integration_batches)
            .where(integration_batches.c.id == batch_id)
            .values(lifecycle="building")
        )
    assert await control_service.eject(
        batch_id, task_id="e2", reason="too late", operator_id="supervisor"
    ) == {"outcome": "invalid_state", "batch_id": batch_id, "task_id": "e2"}
    assert await members_of(batch_id) == ["e1", "e2"]


async def test_eject_command_derives_operator_and_rejects_a_worker(
    control_service, sealed_batch, db
):
    handler = IntegrationCommandsMixin()
    handler.db = db
    handler.orchestrator = SimpleNamespace(integration_control_service=control_service)
    args = {"batch_id": sealed_batch["batch_id"], "task_id": "e2", "reason": "manual"}
    worker = ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        policy=DENY_ALL,
        session_id="worker",
        project_id="p",
        elevated=False,
    )
    with principal_context(worker):
        denied = await handler._cmd_integration_eject(args)
    assert denied["outcome"] == "unauthorized"
    accepted = await handler._cmd_integration_eject(args)
    assert accepted == {
        "outcome": "ejected",
        "batch_id": sealed_batch["batch_id"],
        "task_id": "e2",
        "reason": "manual",
    }
    events = await db.get_recent_events(event_type="integration.batch_ejected", project_id="p")
    assert json.loads(events[0]["payload"])["operator_id"] == "human:local-operator"


def test_cli_eject_forwards_required_arguments(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "src.cli.integration._execute",
        lambda _ctx, command, args: calls.append((command, args)),
    )
    result = CliRunner().invoke(
        integration_cli,
        ["eject", "--batch-id", "batch", "--task-id", "epic", "--reason", "manual"],
    )
    assert result.exit_code == 0, result.output
    assert calls == [
        ("integration_eject", {"batch_id": "batch", "task_id": "epic", "reason": "manual"})
    ]
