"""Reviewed supervisor-failure-triage artifact and internal command contract."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml

from src.commands.contracts import CONTRACTS
from src.commands.contracts.builtin import (
    TaskFailureTriageNotifyArgs,
    TaskFailureTriageNotifyValue,
    set_handler_provider,
)
from src.playbooks.authoring import PlaybookSource
from src.playbooks.definition import canonical_bytes, contract_fingerprint, load_definition_json, source_digest


FIXTURE = Path("tests/fixtures/playbooks/v2/supervisor-failure-triage")
SHIPPED = Path("src/prompts/default_playbooks/supervisor-failure-triage.md")
RULE = "triage-failed-task"
NOTIFY = f"{RULE}--notify_supervisor"
SUCCESSES = ("queued", "existing", "not_actionable", "retired")


def _definition():
    return load_definition_json((FIXTURE / "artifact.json").read_text(encoding="utf-8"))


def test_source_and_artifact_use_one_unfiltered_durable_failure_rule() -> None:
    loaded = PlaybookSource.load(SHIPPED, vault_root=SHIPPED.parent)
    assert isinstance(loaded, PlaybookSource), getattr(loaded, "errors", ())
    assert loaded.frontmatter["id"] == "supervisor-failure-triage"
    assert loaded.frontmatter["scope"] == "system"
    assert loaded.frontmatter["triggers"] == ["task.failed"]
    assert (FIXTURE / "source.md").read_bytes() == SHIPPED.read_bytes()

    definition = _definition()
    assert [rule.id for rule in definition.rules] == [RULE]
    assert definition.rules[0].trigger.event_type == "task.failed"
    assert definition.rules[0].trigger.filter is None
    notify = definition.steps[NOTIFY]
    assert notify.command == "task_failure_triage_notify"
    assert notify.transitions == {
        **{outcome: f"{RULE}--done" for outcome in SUCCESSES},
        "rejected": f"{RULE}--failed",
        "runtime_error": f"{RULE}--failed",
    }


def test_artifact_is_canonical_and_manifest_binds_the_new_contract() -> None:
    raw = (FIXTURE / "artifact.json").read_bytes()
    definition = load_definition_json(raw.decode("utf-8"))
    assert canonical_bytes(definition) == raw
    assert (FIXTURE / "artifact.sha256").read_text().strip() == "sha256:" + hashlib.sha256(raw).hexdigest()
    assert definition.source_hash == source_digest((FIXTURE / "source.md").read_text())
    manifest = (FIXTURE / "manifest.md").read_text()
    frontmatter = yaml.safe_load(manifest[4 : manifest.index("\n---\n", 4)])
    assert frontmatter["contract_fingerprint"] == contract_fingerprint(definition)
    assert frontmatter["capabilities_granted"]["aq_commands"] == ["task_failure_triage_notify"]
    assert json.loads((FIXTURE / "diagnostics.json").read_text()) == []


@pytest.fixture
def handler():
    fake = AsyncMock()
    set_handler_provider(lambda: fake)
    try:
        yield fake
    finally:
        set_handler_provider(None)


async def test_internal_triage_command_is_idempotent_and_maps_all_successes(handler) -> None:
    registration = CONTRACTS.require("task_failure_triage_notify")
    execution = registration.contract.execution
    assert execution.args_model is TaskFailureTriageNotifyArgs
    assert execution.result_model is TaskFailureTriageNotifyValue
    assert {spec.name for spec in execution.outcomes} == {*SUCCESSES, "rejected"}
    assert execution.idempotency.mode == "natural"

    args = TaskFailureTriageNotifyArgs(task_id="task-1", project_id="project-1")
    for outcome in SUCCESSES:
        handler.execute.return_value = {"outcome": outcome, "task_id": "task-1"}
        assert (await registration.invoke(args, None)).outcome == outcome
    handler.execute.return_value = {"error": "scope refused"}
    assert (await registration.invoke(args, None)).outcome == "rejected"


EQUIVALENCE = FIXTURE.with_name("supervisor-failure-triage-equivalence")
SCENARIOS = json.loads((EQUIVALENCE / "scenarios.json").read_text())


def test_port_preserves_baseline_policy_and_records_source_provenance():
    baseline = (EQUIVALENCE / "baseline-source.md").read_bytes()
    assert hashlib.sha256(baseline).hexdigest() == (
        "8cb0684991e1f9cb760abcde9e4ce7d150541a0c58d5ea0038f94db3e2e0de5e"
    )
    old = load_definition_json((EQUIVALENCE / "baseline-artifact.json").read_text())
    new = _definition()
    assert old.source_hash == source_digest(baseline.decode())
    # Source locations and compile metadata may move; executable policy may not.
    for attribute in ("rules", "steps"):
        def policy(definition):
            value = definition.model_dump(mode="json", exclude_none=True)[attribute]
            rows = value.values() if isinstance(value, dict) else value
            for row in rows:
                row.pop("source", None)
            return value
        assert policy(old) == policy(new)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda scenario: scenario["name"])
async def test_port_matches_baseline_through_engine_and_dispatch(scenario):
    from types import SimpleNamespace

    from src.commands.handler import CommandHandler
    from src.commands.principal import TRUSTED_LOCAL
    from src.config import AppConfig
    from src.playbooks.engine import PlaybookEngine
    from src.playbooks.executors.base import EngineServices
    from tests.playbook_v2_engine_helpers import (
        InMemoryArtifactStore,
        RecordingBus,
        RecordingRunRepository,
        StubActivations,
        artifact_ref_for,
    )

    traces = []
    for definition in (
        load_definition_json((EQUIVALENCE / "baseline-artifact.json").read_text()),
        _definition(),
    ):
        db = AsyncMock()
        db.get_task.return_value = SimpleNamespace(id="task-1", project_id="project-1")
        config = AppConfig()
        bus = RecordingBus()
        command_handler = CommandHandler(SimpleNamespace(db=db, bus=bus), config)
        execute = command_handler.execute
        command_handler.execute = AsyncMock(wraps=execute)
        set_handler_provider(lambda: command_handler)
        store = InMemoryArtifactStore()
        store.put(definition)
        ref = artifact_ref_for(definition)
        runs = RecordingRunRepository()
        engine = PlaybookEngine(
            services=EngineServices(
                contracts=CONTRACTS, clock=lambda: 1000.0,
                artifact_store=store, bus=bus, private_values=AsyncMock(),
            ),
            runs=runs, waits=None, activations=StubActivations([ref]),
        )
        trace = []
        try:
            for index, outcome in enumerate(scenario["outcomes"]):
                event = {**scenario["event"], "event_id": f'{scenario["event"]["event_id"]}-{index}'}
                if outcome == "runtime_error":
                    command_handler.execute.side_effect = RuntimeError("dispatch unavailable")
                else:
                    db.notify_task_recovery.return_value = (
                        {"success": False, "error": "scope refused"}
                        if outcome == "rejected" else
                        {"outcome": outcome, "task_id": "task-1", "incident_id": "incident-1"}
                    )
                result = await engine.dispatch_event(event, TRUSTED_LOCAL)
                assert len(result.run_ids) == 1
                snapshot = runs.snapshots[result.run_ids[0]]
                assert snapshot.lifecycle.value == scenario["terminal"]
                receipt = next(r for r in runs.receipts if r.run_id == snapshot.run_id
                               and r.step_id == NOTIFY and r.receipt_kind == "step")
                assert receipt.selected_transition == f"{RULE}::{NOTIFY}::{outcome}"
                trace.append((snapshot.lifecycle.value, receipt.selected_transition))
                command_handler.execute.assert_awaited_with(
                    "task_failure_triage_notify",
                    {"task_id": "task-1", "project_id": "project-1"},
                )
                # The same delivery is deduplicated before invoking the command again.
                before = command_handler.execute.await_count
                await engine.dispatch_event(event, TRUSTED_LOCAL)
                assert command_handler.execute.await_count == before
            assert command_handler.execute.await_count == len(scenario["outcomes"])
            if "runtime_error" not in scenario["outcomes"]:
                assert db.notify_task_recovery.await_count == len(scenario["outcomes"])
            traces.append(trace)
        finally:
            set_handler_provider(None)
    assert traces[0] == traces[1]
