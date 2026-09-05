"""Source and artifact checks for the shipped ``ci-main-sentinel`` playbook."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml

from src.commands.contracts import CONTRACTS
from src.commands.contracts.builtin import CiBaselineStatusValue
from src.playbooks.authoring import PlaybookSource
from src.playbooks.definition import (
    canonical_bytes,
    contract_fingerprint,
    load_definition_json,
    source_digest,
)

FIXTURE = Path("tests/fixtures/playbooks/v2/ci-main-sentinel")
SHIPPED = Path("src/prompts/project_playbooks/agent-queue/ci-main-sentinel.md")
RULE = "keep-main-green"


def _definition():
    return load_definition_json((FIXTURE / "artifact.json").read_text(encoding="utf-8"))


def test_source_is_prose_scoped_to_the_project_on_a_fifteen_minute_timer() -> None:
    loaded = PlaybookSource.load(SHIPPED, vault_root=SHIPPED.parent)
    assert isinstance(loaded, PlaybookSource), getattr(loaded, "errors", ())
    assert loaded.frontmatter["id"] == "ci-main-sentinel"
    assert loaded.frontmatter["scope"] == "project:agent-queue"
    assert loaded.frontmatter["triggers"] == ["timer.15m"]
    assert (FIXTURE / "source.md").read_bytes() == SHIPPED.read_bytes()


def test_artifact_observes_then_repairs_or_escalates() -> None:
    definition = _definition()
    assert definition.id == "ci-main-sentinel"
    assert definition.scope.type == "project" and definition.scope.project_id == "agent-queue"
    assert [rule.id for rule in definition.rules] == [RULE]
    assert definition.rules[0].trigger.event_type == "timer.15m"

    repair_id, escalate_id = f"{RULE}--ensure_repair_task", f"{RULE}--escalate_to_human"
    observe = definition.steps[f"{RULE}--read_baseline"]
    repair = definition.steps[repair_id]
    escalate = definition.steps[escalate_id]
    done, failed = f"{RULE}--done", f"{RULE}--failed"

    assert observe.command == "ci_baseline_status"
    assert observe.save_result_as == "baseline"
    assert observe.transitions == {
        "green": done,
        "pending": done,
        "unknown": done,
        "red": repair_id,
        "red_escalated": escalate_id,
        "rejected": failed,
        "runtime_error": failed,
    }

    assert repair.command == "ensure_task"
    inputs = {name: value.model_dump(mode="json") for name, value in repair.inputs.items()}
    assert inputs["project_id"] == {"type": "literal", "value": "agent-queue"}
    assert inputs["intelligence_class"] == {"type": "literal", "value": "deep-high"}
    assert inputs["priority"] == {"type": "literal", "value": 5}
    for field in ("dedup_key", "title", "description"):
        assert inputs[field] == {"type": "binding_ref", "binding": "baseline", "path": field}
    assert repair.transitions == {
        "created": done, "reused": done, "rejected": failed, "runtime_error": failed
    }

    assert escalate.command == "gate_create"
    inputs = {name: value.model_dump(mode="json") for name, value in escalate.inputs.items()}
    assert inputs["gate_type"] == {"type": "literal", "value": "human"}
    assert inputs["await_id"]["path"] == "escalation_key"
    assert escalate.transitions == {
        "created": done, "reused": done, "skipped": done, "rejected": failed, "runtime_error": failed
    }
    assert definition.steps[done].outcome == "completed"
    assert definition.steps[failed].outcome == "failed"


def test_every_bound_field_is_one_the_command_contract_returns() -> None:
    """The playbook binds only fields ``CiBaselineStatusValue`` declares."""
    definition = _definition()
    declared = set(CiBaselineStatusValue.model_fields)
    for step in definition.steps.values():
        for value in getattr(step, "inputs", {}).values():
            payload = value.model_dump(mode="json")
            if payload.get("type") == "binding_ref" and payload["binding"] == "baseline":
                assert payload["path"] in declared, payload


def test_every_command_and_outcome_resolves_against_the_live_registry() -> None:
    definition = _definition()
    for step in definition.steps.values():
        command = getattr(step, "command", None)
        if command is None:
            continue
        registration = CONTRACTS.require(command)
        declared = {spec.name for spec in registration.contract.execution.outcomes}
        for outcome in step.transitions:
            assert outcome == "runtime_error" or outcome in declared, (command, outcome)
    assert set(definition.compiled_against.commands) == {
        "ci_baseline_status", "ensure_task", "gate_create"
    }
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
    assert manifest["capabilities_granted"]["aq_commands"] == [
        "ci_baseline_status", "ensure_task", "gate_create"
    ]
