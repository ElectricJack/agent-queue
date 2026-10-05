"""Durable human decisions are shared, auditable and binding across supervisors."""
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert

from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
from src.database import tables as t
from src.models import AgentProfile, Project, SessionRecord, Task
from src.operator_decisions import OperatorDecisions
from src.profiles.capabilities import CapabilityPolicy
from tests.test_surface_commands import db as db, handler as handler, task as task
from tests.test_integration_batches import batch_env as batch_env
from tests.test_integration_gitops import setup as setup, git


def instruction(kind="task", identity="task-1", **changes):
    return dict(object_kind=kind, object_id=identity, operator="Jack",
                decision="Wait for the fix; do not land by hand", effect="hold",
                source="discord", source_ref="discord://channel/message-123",
                idempotency_key="message-123", releases=None, **changes)


async def supervisor(db, identity, project):
    await db.create_session(SessionRecord(
        id=identity, project_id=project, profile_id="supervisor", harness="codex",
        provider="openai", name=identity, lifecycle="named", work_dir="/tmp",
        epoch="test", instance_token=identity, started_at=1, state="running",
    ))
    return ExecutionPrincipal(
        kind=PrincipalKind.SESSION, session_id=identity, project_id=project,
        profile_id="supervisor", elevated=True,
        policy=CapabilityPolicy(aq_commands=frozenset({
            "decision_record", "decision_list", "integration_promote_main", "task_show",
        })),
    )


async def test_project_and_global_supervisors_share_hold_and_conflicting_control(
    db, handler, task, monkeypatch,
):
    await db.create_profile(AgentProfile(id="supervisor", name="Supervisor"))
    project = await supervisor(db, "project-supervisor", "proj-1")
    global_ = await supervisor(db, "global-supervisor", None)
    control = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(handler, "_cmd_integration_promote_main", control)
    with principal_context(project):
        recorded = await handler.execute("decision_record", instruction())
    assert recorded["success"], recorded
    row = recorded["decision"]
    assert row["operator"] == "Jack"
    assert row["recorded_by"] == "supervisor session:project-supervisor"
    assert row["created_at"] > 0
    for principal in (project, global_):
        with principal_context(principal):
            read = await handler.execute("decision_list", {
                "object_kind": "task", "object_id": task.id,
            })
            shown = await handler.execute("task_show", {"task_id": task.id})
            blocked = await handler.execute("integration_promote_main", {"task_id": task.id})
        assert read["operator_decisions"][0]["id"] == row["id"]
        assert shown["operator_decisions"] == read["operator_decisions"]
        assert blocked["outcome"] == "operator_decision_hold", blocked
    control.assert_not_awaited()
    with principal_context(global_):
        released = await handler.execute("decision_record", {
            **instruction(), "effect": "release", "releases": row["id"],
            "decision": "The fix is ready; resume integration", "idempotency_key": "message-124",
            "source_ref": "discord://channel/message-124",
        })
        assert released["success"], released
        assert (await handler.execute("integration_promote_main", {"task_id": task.id}))["success"]
    control.assert_awaited_once()
    # A fresh reader observes the release; nothing depends on either mailbox.
    history = await OperatorDecisions(db).history("task", task.id)
    assert len(history) == 2 and not any(row["active"] for row in history)


async def test_replay_changed_payload_and_multiple_holds(db, handler, task):
    first = await handler.execute("decision_record", instruction())
    assert first["success"], first
    assert await handler.execute("decision_record", instruction()) == first
    changed = await handler.execute("decision_record", {**instruction(), "decision": "Changed"})
    assert not changed["success"] and "different" in changed["error"]
    second = await handler.execute("decision_record", {**instruction(), "idempotency_key": "second"})
    release = {**instruction(), "effect": "release", "releases": first["decision"]["id"],
               "idempotency_key": "release"}
    assert (await handler.execute("decision_record", release))["success"]
    assert [row["id"] for row in await OperatorDecisions(db).holds("task", task.id)] == [
        second["decision"]["id"]
    ]
    assert not (await handler.execute("decision_record", {
        **release, "idempotency_key": "release-twice",
    }))["success"]


async def test_foreign_project_worker_and_service_cannot_record_or_release(db, handler, task):
    await db.create_project(Project(id="other", name="Other"))
    await db.create_profile(AgentProfile(id="supervisor", name="Supervisor"))
    other = await supervisor(db, "other-supervisor", "other")
    for principal in (other, replace(other, project_id="proj-1", elevated=False),
                      ExecutionPrincipal.service("integration")):
        with principal_context(principal):
            result = await handler.execute("decision_record", instruction())
            assert not result["success"], result
    assert await OperatorDecisions(db).history("task", task.id) == []


async def test_batch_hold_prevents_real_publication_and_projects_to_member(batch_env):
    db, ops, repo, base, head, store, batch, members, green, _, snapshot, service = batch_env
    await db.create_project(Project(id="p", name="Project"))
    await db.create_task(Task(id="task", project_id="p", title="Task", description=""))
    candidate = await service.visit(batch, members, await snapshot())
    green.add(candidate.candidate_sha)
    decisions = OperatorDecisions(db)
    row = await decisions.record(instruction("batch", batch.id), recorded_by="human:local-operator")
    held = await service.visit(batch, members, await snapshot())
    assert held.state == "held"
    assert git(ops.git.remote_path, "rev-parse", "refs/heads/main") == base
    assert not await service._authorized(batch, members)
    assert (await decisions.holds("task", "task"))[0]["id"] == row["id"]
    from src.integration.status import IntegrationStatusService
    status = await IntegrationStatusService(db, git_first="active").status("p")
    assert status["batches"][0]["operator_decisions"][0]["id"] == row["id"]
    await decisions.record({**instruction("batch", batch.id), "effect": "release",
                            "releases": row["id"], "idempotency_key": "release"},
                           recorded_by="human:local-operator")
    assert (await service.visit(batch, members, await snapshot())).state == "delivered"


async def test_operation_hold_cannot_be_bypassed_using_batch_control(batch_env):
    db, _, _, _, _, _, batch, *_ = batch_env
    await db.create_project(Project(id="p", name="Project"))
    async with db._engine.begin() as conn:
        await conn.execute(insert(t.integration_repair_operations).values(
            id="repair", target_kind="batch", batch_id=batch.id, episode_id="episode",
            state="active", policy_snapshot={}, artifact_snapshot={},
            required_check_version="checks", created_at=1, updated_at=1,
        ))
    row = await OperatorDecisions(db).record(
        instruction("operation", "repair"), recorded_by="human:local-operator",
    )
    from src.operator_decisions import control_refusal
    refused = await control_refusal(db, "integration_promote_main", {"batch_id": batch.id})
    assert refused["operator_decisions"][0]["id"] == row["id"]


async def test_migration_is_idempotent_and_history_cannot_be_rewritten(db):
    import importlib

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect, text
    from sqlalchemy.exc import SQLAlchemyError

    revision = importlib.import_module("migrations.versions.a00000000077_operator_decisions")

    def exercise(conn):
        with Operations.context(MigrationContext.configure(conn)):
            revision.downgrade()
            revision.upgrade()
            revision.upgrade()
        assert {column["name"] for column in inspect(conn).get_columns("operator_decisions")} == {
            column.name for column in t.operator_decisions.columns
        }
        conn.execute(insert(t.operator_decisions).values(
            **instruction(), id="immutable", project_id="proj-1", created_at=1,
            recorded_by="human:local-operator",
        ))
        for statement in (
            "UPDATE operator_decisions SET decision = 'land now'",
            "DELETE FROM operator_decisions",
        ):
            with pytest.raises(SQLAlchemyError, match="append-only"), conn.begin_nested():
                conn.execute(text(statement))

    async with db._engine.begin() as conn:
        await conn.run_sync(exercise)


async def test_installed_supervisor_receives_decision_rule_without_profile_reseed(tmp_path):
    from types import SimpleNamespace

    from src.prime.sections import build_role_section

    profile = tmp_path / "supervisor" / "profile.md"
    profile.parent.mkdir()
    profile.write_text("## Role\nInstalled operator-customized supervisor.\n")
    section = await build_role_section(SimpleNamespace(vault_agent_types=str(tmp_path)), "supervisor")
    assert "Installed operator-customized" in section.body
    assert "aq decision record" in section.body and "aq decision list" in section.body
    assert "source and message reference" in section.body


async def test_hold_recorded_after_observation_still_prevents_target_push(batch_env):
    db, ops, repo, base, _, _, batch, members, _, _, snapshot, service = batch_env
    await db.create_project(Project(id="p", name="Project"))
    await service.visit(batch, members, await snapshot())

    async def hold_while_checking(*_):
        await OperatorDecisions(db).record(
            instruction("batch", batch.id), recorded_by="human:local-operator",
        )
        return True

    service.gate = hold_while_checking
    observed = await service.visit(batch, members, await snapshot())
    assert observed.state == "held"
    assert git(ops.git.remote_path, "rev-parse", "refs/heads/main") == base


def test_cli_decision_commands_are_discoverable():
    from click.testing import CliRunner
    from src.cli.app import cli

    for command in ("record", "list"):
        result = CliRunner().invoke(cli, ["decision", command, "--help"])
        assert result.exit_code == 0, result.output
        assert "--object-id" in result.output


@pytest.mark.parametrize("entity", ["task", "integration"])
def test_brief_never_hides_operator_instructions(entity):
    from src.cli.envelope import apply_brief

    decisions = [{"id": "hold", "decision": "Wait for the fix", "active": True}]
    assert apply_brief({"operator_decisions": decisions}, entity)["operator_decisions"] == decisions


async def test_note_cannot_release_a_hold_and_release_cannot_target_another_task(db, handler, task):
    hold = await handler.execute("decision_record", instruction())
    note = await handler.execute("decision_record", {
        **instruction(), "effect": "note", "idempotency_key": "note", "decision": "Fix is underway",
    })
    assert note["success"]
    assert len(await OperatorDecisions(db).holds("task", task.id)) == 1
    await db.create_task(Task(id="other-task", project_id="proj-1", title="Other", description=""))
    bad_release = await handler.execute("decision_record", {
        **instruction(identity="other-task"), "effect": "release", "idempotency_key": "release",
        "releases": hold["decision"]["id"],
    })
    assert not bad_release["success"]
    assert len(await OperatorDecisions(db).holds("task", task.id)) == 1
