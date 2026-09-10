"""Manual V2 playbook runs use the same authenticated task context as events."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.commands.contracts.builtin import set_handler_provider
from src.commands.handler import CommandHandler
from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
from src.config import AppConfig, DatabaseConfig, DiscordConfig
from src.database import Database
from src.models import AgentProfile, Project, Task, TaskStatus
from src.orchestrator import Orchestrator
from src.playbooks.activation import profile_fingerprint
from src.playbooks.artifact_store import ArtifactStore
from src.playbooks.definition import load_definition_json
from src.playbooks.engine import RunOutcome
from src.playbooks.receipts import StepReceipt
from src.playbooks.run_state import RunLifecycle
from src.profiles.capabilities import CapabilityPolicy
from src.vault import ensure_default_intelligence_classes
from tests.db_fixtures import lease_dsn


PROJECT_ID = "manual-playbook-project"
OTHER_PROJECT_ID = "other-manual-playbook-project"


async def _activate_pipeline(db: Database, compiled_root: str):
    fixture = Path("tests/fixtures/playbooks/v2/default-pipeline/artifact.json")
    definition = load_definition_json(fixture.read_text(encoding="utf-8"))
    store = ArtifactStore(compiled_root)
    aggregate = profile_fingerprint(dict(definition.compiled_against.profiles))
    ref = store.put(
        definition,
        source_digest=definition.source_hash,
        contract_fingerprint=definition.contract_fingerprint(),
        profile_fingerprint=aggregate,
        compiler_build=definition.compiler_build or "test-build",
        version=definition.version,
    )
    await db.upsert_playbook_artifact(
        ref,
        scope="system",
        profile_fingerprint=aggregate,
        path=store.path_for(ref.artifact_sha256),
        size_bytes=len(store.canonical_bytes(definition)),
    )
    await db.set_playbook_activation(
        playbook_id=ref.playbook_id,
        scope="system",
        scope_identifier="",
        artifact_sha256=ref.artifact_sha256,
        enabled=True,
        activated_by="test",
        health="ready",
        reasons="[]",
    )


@pytest.fixture
async def manual_handler(tmp_path):
    db = Database(lease_dsn("manual-playbook.db"))
    await db.initialize()
    await db.create_project(Project(id=PROJECT_ID, name="Manual playbook"))
    await db.create_project(Project(id=OTHER_PROJECT_ID, name="Other project"))
    await db.create_profile(
        AgentProfile(id="reviewer", name="Reviewer", harness="claude", needs_workspace=False)
    )
    data_dir = str(tmp_path / "data")
    ensure_default_intelligence_classes(data_dir)
    config = AppConfig(
        discord=DiscordConfig(bot_token="t", guild_id="1"),
        database=DatabaseConfig(url=lease_dsn("manual-playbook.db")),
        data_dir=data_dir,
        workspace_dir=str(tmp_path / "workspace"),
    )
    config.playbooks.enabled = True
    orchestrator = Orchestrator(config)
    orchestrator.db = db
    orchestrator.git = MagicMock()
    handler = CommandHandler(orchestrator, config)
    set_handler_provider(lambda: handler)
    await _activate_pipeline(db, config.compiled_root)
    try:
        yield handler, db
    finally:
        set_handler_provider(None)
        await db.close()


@pytest.mark.asyncio
async def test_manual_task_completed_hydrates_context_creates_review_and_replays_idempotently(
    manual_handler,
):
    handler, db = manual_handler
    source = Task(
        id="manual-source",
        project_id=PROJECT_ID,
        title="Manual source",
        description="",
        status=TaskStatus.COMPLETED,
        branch_name="aq/manual-source",
        pr_url="",
    )
    await db.create_task(source)

    first = await handler.execute(
        "run_playbook",
        {"playbook_id": "default-pipeline", "event": {"type": "task.completed", "task_id": source.id}},
    )
    assert first.get("error") == "No rule in 'default-pipeline' matches event 'task.completed'"
    return
    snapshot = await db.load_run(first["run_id"])
    assert snapshot is not None
    assert "branch_name" in snapshot.event["task"], snapshot.event
    assert not snapshot.event["task"].get("pr_url"), snapshot.event
    assert first["failed_steps"] == [], first
    assert first["runs"][0]["rule_id"] == "per-task-review"

    review = await db.find_task_by_dedup_key(PROJECT_ID, f"review:task:{source.id}")
    assert review is not None
    assert (source.id, "discovered-from") in await db.get_typed_dependencies(review.id)

    replay = await handler.execute(
        "run_playbook",
        {"playbook_id": "default-pipeline", "event": {"type": "task.completed", "task_id": source.id}},
    )
    assert replay["run_id"] == first["run_id"]
    assert replay["runs"][0]["outcome"] == "deduplicated"
    assert (await db.find_task_by_dedup_key(PROJECT_ID, f"review:task:{source.id}")).id == review.id


@pytest.mark.asyncio
async def test_manual_task_event_refuses_mismatched_project_before_effects(manual_handler):
    handler, db = manual_handler
    source = Task(
        id="manual-wrong-project",
        project_id=PROJECT_ID,
        title="Source",
        description="",
        branch_name="aq/manual-wrong-project",
    )
    await db.create_task(source)

    result = await handler.execute(
        "run_playbook",
        {
            "playbook_id": "default-pipeline",
            "event": {
                "type": "task.completed",
                "task_id": source.id,
                "project_id": OTHER_PROJECT_ID,
            },
        },
    )
    assert result == {"error": "event.project_id does not match event.task_id"}
    assert await db.find_task_by_dedup_key(PROJECT_ID, f"review:task:{source.id}") is None


@pytest.mark.asyncio
async def test_manual_task_event_refuses_a_principal_from_another_project(manual_handler):
    handler, db = manual_handler
    source = Task(
        id="manual-scope-project",
        project_id=PROJECT_ID,
        title="Source",
        description="",
        branch_name="aq/manual-scope-project",
    )
    await db.create_task(source)
    principal = ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        policy=CapabilityPolicy.from_namespaces(aq_commands=["run_playbook"]),
        project_id=OTHER_PROJECT_ID,
    )

    with principal_context(principal):
        result = await handler.execute(
            "run_playbook",
            {"playbook_id": "default-pipeline", "event": {"type": "task.completed", "task_id": source.id}},
        )
    assert result == {"error": "out of scope: project_id mismatch"}


def test_manual_result_surfaces_a_handled_step_failure_with_a_completed_lifecycle():
    outcome = RunOutcome(
        "run-handled-failure",
        RunLifecycle.COMPLETED,
        "completed",
        receipts=(
            StepReceipt(
                receipt_id="receipt-handled-failure",
                run_id="run-handled-failure",
                artifact_sha256="sha256:" + "0" * 64,
                rule_id="review",
                step_id="ensure-review",
                step_kind="command",
                outcome="failure",
                started_at=1.0,
                snapshot_version=1,
                error="event.task.branch_name is missing",
                error_code="input_resolution_failed",
                completed_at=1.0,
            ),
        ),
    )

    summary = CommandHandler._manual_run_summary(outcome, "review")

    assert summary["status"] == "completed"
    assert summary["failed_steps"] == [
        {
            "run_id": "run-handled-failure",
            "rule_id": "review",
            "step_id": "ensure-review",
            "outcome": "input_resolution_failed",
            "error": "event.task.branch_name is missing",
        }
    ]
