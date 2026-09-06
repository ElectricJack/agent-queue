"""Persistence and read projections for integration rollout controls."""

from __future__ import annotations

import pytest
from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import IntegrityError

from src.database import Database
from src.database.tables import (
    gates,
    integration_history_waivers,
    integration_legacy_gate_applicability,
    integration_batches,
    integration_release_results,
    integration_rollout_transitions,
)
from src.integration.status import IntegrationStatusService
from src.models import Project, RepoConfig, RepoSourceType, Task, TaskStatus


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "integration-controls.db"))
    await database.initialize()
    await database.create_project(Project(id="p", name="project"))
    await database.create_repo(
        RepoConfig(
            id="repo",
            project_id="p",
            source_type=RepoSourceType.CLONE,
            url="https://github.com/acme/widgets.git",
        )
    )
    yield database
    await database.close()


async def test_project_control_state_is_typed_and_defaults_disabled(db):
    project = await db.get_project("p")

    assert project is not None
    assert project.integration_mode is None
    assert project.hierarchical_integration_mode == "disabled"
    assert project.hierarchical_integration_desired_mode == "disabled"
    assert project.hierarchical_integration_draining is False
    assert project.hierarchical_integration_generation == 0

    with pytest.raises(ValueError, match="CAS"):
        await db.update_project("p", hierarchical_integration_desired_mode="train")

    status = await IntegrationStatusService(db).status("p")
    assert status is not None
    assert status["ready"] is False
    assert status["rollout_ready"] is False
    assert {item["code"] for item in status["blockers"]} == {
        "preflight_evidence_unavailable",
        "repository_not_designated",
    }


async def test_conn_owned_cas_appends_transition_and_reversible_suppression(db):
    before = {
        "merge_sweep_suppressed": False,
        "final_review_route_suppressed": False,
        "legacy_gate_creation_suppressed": False,
    }
    after = {key: True for key in before}
    async with db.immediate() as conn:
        await db.lock_hierarchy_project(conn, "p")
        changed = await db.cas_project_integration_control_on(
            conn,
            project_id="p",
            expected_generation=0,
            effective_mode="disabled",
            desired_mode="observe",
            draining=False,
        )
        await db.append_integration_rollout_transition_on(
            conn,
            transition_id="transition-1",
            project_id="p",
            generation=1,
            old_effective_mode="disabled",
            new_effective_mode="disabled",
            old_desired_mode="disabled",
            new_desired_mode="observe",
            draining=False,
            operator_id="operator:local",
            reason="begin observation",
            blocker_digest="sha256:" + "a" * 64,
            old_legacy_policy=before,
            new_legacy_policy=after,
            waiver_id=None,
            now=10.0,
        )
        await db.set_integration_legacy_suppression_on(
            conn,
            project_id="p",
            generation=1,
            merge_sweep_suppressed=True,
            final_review_route_suppressed=True,
            legacy_gate_creation_suppressed=True,
            policy_snapshot=after,
            now=10.0,
        )

    assert changed is True
    project = await db.get_project("p")
    assert project is not None
    assert project.hierarchical_integration_mode == "disabled"
    assert project.hierarchical_integration_desired_mode == "observe"
    assert project.hierarchical_integration_generation == 1
    assert await db.get_integration_legacy_suppression("p") == {
        "project_id": "p",
        "generation": 1,
        **after,
        "policy_snapshot": after,
        "updated_at": 10.0,
    }

    async with db.immediate() as conn:
        assert await db.cas_project_integration_control_on(
            conn,
            project_id="p",
            expected_generation=1,
            effective_mode="disabled",
            desired_mode="disabled",
            draining=False,
        )
        stale = await db.cas_project_integration_control_on(
            conn,
            project_id="p",
            expected_generation=0,
            effective_mode="disabled",
            desired_mode="train",
            draining=False,
        )
        await db.set_integration_legacy_suppression_on(
            conn,
            project_id="p",
            generation=2,
            merge_sweep_suppressed=False,
            final_review_route_suppressed=False,
            legacy_gate_creation_suppressed=False,
            policy_snapshot=before,
            now=20.0,
        )

    assert stale is False
    restored = await db.get_integration_legacy_suppression("p")
    assert restored is not None
    assert restored["merge_sweep_suppressed"] is False
    assert (await db.get_project("p")).hierarchical_integration_desired_mode == "disabled"


async def test_waiver_consumption_and_gate_applicability_are_append_only(db):
    async with db.immediate() as conn:
        with pytest.raises(ValueError, match="sha256"):
            await db.append_integration_history_waiver_on(
                conn,
                waiver_id="invalid-waiver",
                project_id="p",
                operator_id="operator:local",
                reason="invalid digest is not durable evidence",
                blocker_digest="sha256:" + "z" * 64,
                now=1.0,
            )
        await conn.execute(
            insert(gates).values(
                id="gate-1",
                project_id="p",
                gate_type="pr-merged",
                title="legacy merge",
                status="open",
                created_at=1.0,
            )
        )
        await db.append_integration_history_waiver_on(
            conn,
            waiver_id="waiver-1",
            project_id="p",
            operator_id="operator:local",
            reason="accept pre-cutover review history",
            blocker_digest="sha256:" + "b" * 64,
            now=2.0,
        )
        assert await db.cas_project_integration_control_on(
            conn,
            project_id="p",
            expected_generation=0,
            effective_mode="disabled",
            desired_mode="observe",
            draining=False,
        )
        await db.append_integration_rollout_transition_on(
            conn,
            transition_id="transition-1",
            project_id="p",
            generation=1,
            old_effective_mode="disabled",
            new_effective_mode="disabled",
            old_desired_mode="disabled",
            new_desired_mode="observe",
            draining=False,
            operator_id="operator:local",
            reason="observe with explicit history waiver",
            blocker_digest="sha256:" + "b" * 64,
            old_legacy_policy={},
            new_legacy_policy={},
            waiver_id="waiver-1",
            now=3.0,
        )
        consumed = await db.consume_integration_history_waiver_on(
            conn,
            waiver_id="waiver-1",
            transition_id="transition-1",
            project_id="p",
            blocker_digest="sha256:" + "b" * 64,
            consumed_by="operator:local",
            now=3.0,
        )
        await db.append_integration_legacy_gate_applicability_on(
            conn,
            project_id="p",
            gate_id="gate-1",
            waiver_id="waiver-1",
            transition_id="transition-1",
            blocker_digest="sha256:" + "b" * 64,
            applicable=False,
            now=3.0,
        )

    assert consumed is True
    assert await db.consume_integration_history_waiver(
        waiver_id="waiver-1",
        transition_id="transition-1",
        project_id="p",
        blocker_digest="sha256:" + "b" * 64,
        consumed_by="operator:local",
        now=4.0,
    ) is False
    async with db._engine.connect() as conn:
        gate = (await conn.execute(select(gates).where(gates.c.id == "gate-1"))).mappings().one()
    assert gate["status"] == "open"
    assert gate["resolution"] is None

    immutable_tables = (
        integration_history_waivers,
        integration_rollout_transitions,
        integration_legacy_gate_applicability,
    )
    for table in immutable_tables:
        with pytest.raises(IntegrityError):
            async with db._engine.begin() as conn:
                await conn.execute(update(table).values(created_at=99.0))
        with pytest.raises(IntegrityError):
            async with db._engine.begin() as conn:
                await conn.execute(table.delete())


async def test_status_is_read_only_sorted_and_absent_preflight_is_blocking(db):
    await db.update_project("p", integration_repository_id="repo")
    async with db.immediate() as conn:
        assert await db.cas_project_integration_control_on(
            conn,
            project_id="p",
            expected_generation=0,
            effective_mode="disabled",
            desired_mode="observe",
            draining=False,
        )
        await conn.execute(
            insert(integration_batches).values(
                id="released-batch",
                project_id="p",
                repository_id="repo",
                request_id="request-1",
                trigger="manual",
                source_manifest_digest="sha256:" + "c" * 64,
                base_sha="1" * 40,
                lifecycle="promoted",
                integration_branch="aq/integration/released",
                policy_snapshot={},
                artifact_snapshot={},
                cleanup_state="complete",
                created_at=1.0,
                updated_at=2.0,
            )
        )
        await conn.execute(
            insert(integration_release_results).values(
                batch_id="released-batch",
                project_id="p",
                request_id="request-1",
                operation_id="operation-1",
                catchup_request_id=None,
                released_at=3.0,
            )
        )
    service = IntegrationStatusService(db)
    async with db._engine.connect() as conn:
        before = int(
            (
                await conn.execute(
                    text(
                        "SELECT total_changes()"
                        if conn.dialect.name == "sqlite"
                        else "SELECT 0"
                    )
                )
            ).scalar_one()
        )

    status = await service.status("p")

    async with db._engine.connect() as conn:
        after = int(
            (
                await conn.execute(
                    text(
                        "SELECT total_changes()"
                        if conn.dialect.name == "sqlite"
                        else "SELECT 0"
                    )
                )
            ).scalar_one()
        )
    assert after == before
    assert status["effective_mode"] == "disabled"
    assert status["desired_mode"] == "observe"
    assert status["draining"] is False
    assert status["schedule"] is None
    assert status["active_batch"] is None
    assert status["cleanup_pending"] == []
    assert status["release"]["batch_id"] == "released-batch"
    assert status["ready"] is False
    codes = [item["code"] for item in status["blockers"]]
    assert codes == sorted(codes)
    assert "preflight_evidence_unavailable" in codes


async def test_task_blockers_validate_relationship_and_expose_hierarchy_reasons(db):
    await db.create_task(
        Task(
            id="parent",
            project_id="p",
            title="parent",
            description="parent",
            status=TaskStatus.IN_PROGRESS,
            repo_id="repo",
        )
    )
    await db.create_task(
        Task(
            id="child",
            project_id="p",
            title="child",
            description="child",
            status=TaskStatus.READY,
            repo_id="repo",
            parent_task_id="parent",
        )
    )
    await db.update_project("p", integration_repository_id="repo")
    async with db.immediate() as conn:
        assert await db.cas_project_integration_control_on(
            conn,
            project_id="p",
            expected_generation=0,
            effective_mode="disabled",
            desired_mode="observe",
            draining=False,
        )
    service = IntegrationStatusService(db)

    blockers = await service.task_blockers("parent")

    assert blockers["project_id"] == "p"
    assert [item["code"] for item in blockers["blockers"]] == sorted(
        item["code"] for item in blockers["blockers"]
    )
    assert "open_child" in {item["code"] for item in blockers["blockers"]}
    assert await service.task_blockers("missing") is None
