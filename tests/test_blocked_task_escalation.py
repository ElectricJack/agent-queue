"""Source, artifact and contract checks for the shipped ``blocked-task-escalation`` playbook."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml

from src.commands.contracts import CONTRACTS
from src.commands.contracts.builtin import MessageSendArgs, MessageSendValue, set_handler_provider
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


def test_artifact_filters_blocked_closes_and_messages_the_project_supervisor() -> None:
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
    assert notify.command == "message_send"
    assert notify.save_result_as == "notice"
    assert notify.transitions == {"queued": done, "rejected": failed, "runtime_error": failed}

    inputs = {name: value.model_dump(mode="json") for name, value in notify.inputs.items()}
    assert inputs["project_id"] == {"type": "event_ref", "path": "project_id"}
    assert inputs["to_kind"] == {"type": "literal", "value": "session"}
    assert inputs["from_kind"] == {"type": "literal", "value": "system"}
    assert inputs["from_id"] == {"type": "literal", "value": "playbook:blocked-task-escalation"}
    assert inputs["to_id"]["type"] == "template"
    assert definition.steps[done].outcome == "completed"
    assert definition.steps[failed].outcome == "failed"


def test_the_message_names_the_task_and_tells_the_supervisor_to_read_the_log_tail() -> None:
    notify = _definition().steps[NOTIFY]
    scope = ResolutionScope(event=_blocked_event(), context={}, bindings={}, loop={})
    rendered = {name: resolve_value(value, scope) for name, value in notify.inputs.items()}

    assert rendered["to_id"] == "supervisor-proj"
    assert rendered["subject"] == "Blocked task: Ship the widget (task-1)"
    body = rendered["body"]
    assert "task-1" in body and "Ship the widget" in body
    assert "max_retries" in body and "tests kept failing" in body and "agent-7" in body
    assert "aq session logs" in body and "aq session list" in body
    assert "aq task explain task-1" in body
    assert "only a supervisor triage notice" in body
    assert "ordinary dependency waits" in body and "active retry legs" in body
    assert "aq escalation create" in body
    assert "task-recovery:<incident-id>" in body
    assert "aq escalation apply-reply" in body


def test_optional_event_fields_render_with_fallbacks_instead_of_failing() -> None:
    notify = _definition().steps[NOTIFY]
    event = _blocked_event()
    event["agent_id"] = None
    del event["error"]
    scope = ResolutionScope(event=event, context={}, bindings={}, loop={})
    body = resolve_value(notify.inputs["body"], scope)
    assert "(`error`): n/a" in body
    assert "(`agent_id`): unknown" in body


def test_recovery_notice_preserves_integration_operation_authority() -> None:
    notify = _definition().steps[NOTIFY]
    scope = ResolutionScope(event=_blocked_event(), context={}, bindings={}, loop={})
    body = resolve_value(notify.inputs["body"], scope)
    assert "aq integration status proj" in body
    assert "do not use generic task recovery" in body
    assert "aq integration resume" in body
    assert "do not reset its attempt or time budgets" in body


def test_every_command_and_outcome_resolves_against_the_live_registry() -> None:
    definition = _definition()
    registration = CONTRACTS.require("message_send")
    declared = {spec.name for spec in registration.contract.execution.outcomes}
    for outcome in definition.steps[NOTIFY].transitions:
        assert outcome == "runtime_error" or outcome in declared, outcome
    assert set(definition.compiled_against.commands) == {"message_send"}
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
    assert manifest["capabilities_granted"]["aq_commands"] == ["message_send"]


# ---------------------------------------------------------------------------
# The ``message_send`` contract the playbook depends on
# ---------------------------------------------------------------------------


@pytest.fixture
def handler():
    fake = AsyncMock()
    set_handler_provider(lambda: fake)
    try:
        yield fake
    finally:
        set_handler_provider(None)


def test_message_send_is_contracted_as_a_create_with_a_queued_outcome() -> None:
    registration = CONTRACTS.require("message_send")
    execution = registration.contract.execution
    assert execution.args_model is MessageSendArgs
    assert execution.result_model is MessageSendValue
    assert {spec.name for spec in execution.outcomes} == {"queued", "rejected"}
    assert execution.side_effect == "create"
    assert [clause.subject for clause in execution.effects] == ["message"]
    assert MessageSendArgs(to_kind="session", to_id="supervisor-p", body="x", from_id="pb").from_kind == "system"


async def test_message_send_adapter_maps_a_queued_row_and_a_refusal(handler) -> None:
    registration = CONTRACTS.require("message_send")
    args = MessageSendArgs(
        to_kind="session", to_id="supervisor-proj", body="hello", from_id="playbook:x",
        project_id="proj", subject="s", priority=50,
    )

    handler.execute.return_value = {"message_id": "m-1", "state": "queued", "message": {}}
    result = await registration.invoke(args, None)
    assert result.outcome == "queued"
    assert result.value.message_id == "m-1" and result.value.state == "queued"
    name, sent = handler.execute.await_args.args
    assert name == "message_send"
    assert sent == {
        "to_kind": "session", "to_id": "supervisor-proj", "body": "hello", "from_id": "playbook:x",
        "from_kind": "system", "project_id": "proj", "subject": "s", "priority": 50,
    }

    handler.execute.return_value = {"error": "Project 'proj' not found"}
    refused = await registration.invoke(args, None)
    assert refused.outcome == "rejected"
    assert "not found" in refused.summary
