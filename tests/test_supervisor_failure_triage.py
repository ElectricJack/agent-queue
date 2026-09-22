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
