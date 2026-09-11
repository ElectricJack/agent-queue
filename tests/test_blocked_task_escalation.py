"""Source, artifact and contract checks for the shipped ``blocked-task-escalation`` playbook."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml

from src.commands.contracts import CONTRACTS
from src.commands.contracts.builtin import (
    TaskRecoveryNotifyArgs,
    TaskRecoveryNotifyValue,
    set_handler_provider,
)
from src.models import TaskStatus
from src.playbooks.authoring import PlaybookSource
from src.playbooks.definition import (
    canonical_bytes,
    contract_fingerprint,
    load_definition_json,
    source_digest,
)
from src.playbooks.expressions import ResolutionScope, resolve_value

FIXTURE = Path("tests/fixtures/playbooks/v2/blocked-task-escalation")
SHIPPED = Path("src/prompts/default_playbooks/blocked-task-escalation.md")
RULE = "escalate-blocked-task"
NOTIFY = f"{RULE}--notify_supervisor"
SUCCESSES = ("queued", "existing", "not_actionable", "retired")


def _definition():
    return load_definition_json((FIXTURE / "artifact.json").read_text(encoding="utf-8"))


def _blocked_event(**overrides):
    event = {
        "task_id": "task-1",
        "project_id": "proj",
        "title": "Ship the widget",
        "status": TaskStatus.BLOCKED.value,
        "context": "max_retries",
        "error": "tests kept failing",
        "agent_id": "agent-7",
    }
    event.update(overrides)
    return event


def test_source_is_a_system_scoped_prose_playbook_on_task_failed() -> None:
    loaded = PlaybookSource.load(SHIPPED, vault_root=SHIPPED.parent)
    assert isinstance(loaded, PlaybookSource), getattr(loaded, "errors", ())
    assert loaded.frontmatter["id"] == "blocked-task-escalation"
    assert loaded.frontmatter["scope"] == "system"
    assert loaded.frontmatter["enabled"] is True
    assert loaded.frontmatter["triggers"] == ["task.failed"]
    assert (FIXTURE / "source.md").read_bytes() == SHIPPED.read_bytes()


def test_artifact_filters_blocked_closes_and_wakes_the_one_recovery_incident() -> None:
    definition = _definition()
    assert definition.id == "blocked-task-escalation"
    assert definition.scope.type == "system"
    assert [rule.id for rule in definition.rules] == [RULE]
    trigger = definition.rules[0].trigger
    assert trigger.event_type == "task.failed"
    # The wire value is the enum's own upper-case spelling; a lower-case
    # literal here matched nothing (task fair-ridge, 2026-09-07).
    assert trigger.filter == {"status": TaskStatus.BLOCKED.value}

    done, failed = f"{RULE}--done", f"{RULE}--failed"
    assert set(definition.steps) == {NOTIFY, done, failed}
    notify = definition.steps[NOTIFY]
    assert notify.command == "task_recovery_notify"
    assert notify.save_result_as == "incident"
    assert notify.transitions == {
        **{outcome: done for outcome in SUCCESSES},
        "rejected": failed,
        "runtime_error": failed,
    }
    inputs = {name: value.model_dump(mode="json") for name, value in notify.inputs.items()}
    assert inputs == {
        "task_id": {"type": "event_ref", "path": "task_id"},
        "project_id": {"type": "event_ref", "path": "project_id"},
    }
    assert definition.steps[done].outcome == "completed"
    assert definition.steps[failed].outcome == "failed"


def test_the_step_binds_only_the_event_identity() -> None:
    """Owner, budget and next action come from the durable incident, not the event."""
    notify = _definition().steps[NOTIFY]
    scope = ResolutionScope(event=_blocked_event(), context={}, bindings={}, loop={})
    rendered = {name: resolve_value(value, scope) for name, value in notify.inputs.items()}
    assert rendered == {"task_id": "task-1", "project_id": "proj"}


def test_the_playbook_writes_no_message_of_its_own() -> None:
    """A second, event-only notice is what split one failure into two incidents."""
    definition = _definition()
    assert {
        step.command for step in definition.steps.values() if getattr(step, "command", None)
    } == {"task_recovery_notify"}
    source = SHIPPED.read_text(encoding="utf-8")
    assert "writes no message of its own" in source
    assert "one incident and one" in source


def test_every_command_and_outcome_resolves_against_the_live_registry() -> None:
    definition = _definition()
    registration = CONTRACTS.require("task_recovery_notify")
    declared = {spec.name for spec in registration.contract.execution.outcomes}
    for outcome in definition.steps[NOTIFY].transitions:
        assert outcome == "runtime_error" or outcome in declared, outcome
    assert set(definition.compiled_against.commands) == {"task_recovery_notify"}
    assert definition.compiled_against.profiles == {}


def test_artifact_is_canonical_bound_to_the_source_and_its_manifest() -> None:
    raw = (FIXTURE / "artifact.json").read_bytes()
    definition = load_definition_json(raw.decode("utf-8"))
    source = (FIXTURE / "source.md").read_text(encoding="utf-8")
    recorded = (FIXTURE / "artifact.sha256").read_text(encoding="utf-8").strip()

    assert canonical_bytes(definition) == raw
    assert definition.source_hash == source_digest(source)
    assert recorded == "sha256:" + hashlib.sha256(raw).hexdigest()
    assert json.loads((FIXTURE / "diagnostics.json").read_text(encoding="utf-8")) == []

    text = (FIXTURE / "manifest.md").read_text(encoding="utf-8")
    manifest = yaml.safe_load(text[4 : text.index("\n---\n", 4)])
    assert manifest["artifact_sha256"] == recorded
    assert manifest["source_sha256"] == source_digest(source)
    assert manifest["contract_fingerprint"] == contract_fingerprint(definition)
    assert manifest["capabilities_granted"]["aq_commands"] == ["task_recovery_notify"]


# ---------------------------------------------------------------------------
# The ``task_recovery_notify`` contract the playbook depends on
# ---------------------------------------------------------------------------


@pytest.fixture
def handler():
    fake = AsyncMock()
    set_handler_provider(lambda: fake)
    try:
        yield fake
    finally:
        set_handler_provider(None)


def test_task_recovery_notify_is_contracted_as_an_idempotent_create() -> None:
    registration = CONTRACTS.require("task_recovery_notify")
    execution = registration.contract.execution
    assert execution.args_model is TaskRecoveryNotifyArgs
    assert execution.result_model is TaskRecoveryNotifyValue
    assert {spec.name for spec in execution.outcomes} == {*SUCCESSES, "rejected"}
    assert execution.side_effect == "create"
    assert [clause.subject for clause in execution.effects] == ["message"]


async def test_task_recovery_notify_adapter_maps_every_outcome_and_a_refusal(handler) -> None:
    registration = CONTRACTS.require("task_recovery_notify")
    args = TaskRecoveryNotifyArgs(task_id="task-1", project_id="proj")

    handler.execute.return_value = {
        "outcome": "queued", "task_id": "task-1", "incident_id": "recovery-1",
    }
    result = await registration.invoke(args, None)
    assert result.outcome == "queued"
    assert result.value.incident_id == "recovery-1"
    name, sent = handler.execute.await_args.args
    assert name == "task_recovery_notify"
    assert sent == {"task_id": "task-1", "project_id": "proj"}

    for outcome in SUCCESSES[1:]:
        handler.execute.return_value = {"outcome": outcome, "task_id": "task-1"}
        assert (await registration.invoke(args, None)).outcome == outcome

    handler.execute.return_value = {"error": "Task 'task-1' not found in this project"}
    refused = await registration.invoke(args, None)
    assert refused.outcome == "rejected"
    assert "not found" in refused.summary

    handler.execute.return_value = {"outcome": "something-new"}
    assert (await registration.invoke(args, None)).outcome == "rejected"


def test_the_incident_hook_stays_off_every_external_surface() -> None:
    from src.api.codegen import API_EXCLUDED
    from src.cli.auto_commands import EXCLUDED
    from src.mcp_registration import DEFAULT_EXCLUDED_COMMANDS

    assert "task_recovery_notify" in API_EXCLUDED
    assert "task_recovery_notify" in EXCLUDED
    assert "task_recovery_notify" in DEFAULT_EXCLUDED_COMMANDS
