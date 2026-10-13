"""Feature pins preserve legacy bytes and refuse unsupported effects independently."""

import hashlib
import importlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from src.commands.principal import TRUSTED_LOCAL
from src.doctor.playbook_v2_checks import _check_activation_invalid
from src.playbooks import features
from src.playbooks.activation import ActivationHealth, load_activation_health
from src.playbooks.artifact_diff import diff_artifacts
from src.playbooks.authoring import PlaybookSource
from src.playbooks.definition import (
    PlaybookDefinition,
    TrustedExecutionMetadata,
    artifact_sha256,
    canonical_bytes,
    load_definition_json,
)
from src.playbooks.engine import TimerFired
from src.playbooks.executors.base import ExecutionMode
from src.playbooks.proposal import propose
from src.playbooks.run_state import RunLifecycle
from src.playbooks.semantic_diff import diff_definitions
from src.playbooks.validation import RegistryContractLookup, validate_definition
from tests.playbook_v2_engine_helpers import artifact_ref_for, minimal_artifact
from tests.playbook_v2_helpers import GOLDEN, StubContracts, StubEvents, StubProfiles, twin
from tests.test_playbook_activation_commands import _Handler
from tests.test_playbook_nested_loops import nested_artifact, nested_engine
from tests.test_v2_engine import build, ok


def definition(**updates):
    return PlaybookDefinition.model_validate({**twin(), **updates})


def proposal(tmp_path, *, body=None, frontmatter="", contracts=None):
    path = tmp_path / "source.md"
    path.write_text(
        "---\nid: demo\nscope: system\ntriggers: [task.completed]\n"
        + frontmatter + "---\nReviewed source.\n"
    )
    source = PlaybookSource.load(path, vault_root=tmp_path)
    artifact = twin()
    return propose(
        source, body or {"rules": artifact["rules"], "steps": artifact["steps"]},
        contracts=contracts or StubContracts(), profiles=StubProfiles(), events=StubEvents(),
        version=1, enforce_inventory=False,
    )


def test_legacy_golden_digest_and_empty_metadata_are_unchanged():
    legacy = load_definition_json(GOLDEN.read_text())
    # A stored, pre-B3 digest, not a freshly generated expectation.
    expected = "sha256:7563817097f26dc9246d6507c25f148b63876c5f387f264944e060ff057fa131"
    for metadata in (None, {}):
        explicit = PlaybookDefinition.model_validate({
            **json.loads(GOLDEN.read_text()), "required_features": [],
            "trusted_execution": metadata,
        })
        assert canonical_bytes(explicit) == canonical_bytes(legacy)
        assert artifact_sha256(explicit) == expected
        assert "required_features" not in explicit.model_dump(mode="json")
        assert "trusted_execution" not in explicit.model_dump(mode="json")


@pytest.mark.parametrize("path", sorted(Path("src/prompts/reviewed_playbooks").glob("*/artifact.json")))
def test_shipped_canonical_artifacts_keep_exact_bytes_and_recorded_hash(path):
    payload = path.read_bytes()
    assert canonical_bytes(load_definition_json(payload.decode())) == payload
    assert "sha256:" + hashlib.sha256(payload).hexdigest() == (
        path.with_name("artifact.sha256").read_text().strip()
    )


def test_feature_pins_are_stable_and_change_artifact_not_contract_identity():
    old = definition()
    new = definition(required_features=["nested_foreach_v1", "collections_v1", "collections_v1"])
    assert new.required_features == ("collections_v1", "nested_foreach_v1")
    assert load_definition_json(canonical_bytes(new).decode()) == new
    assert artifact_sha256(new) != artifact_sha256(old)
    assert new.contract_fingerprint() == old.contract_fingerprint()


def test_serialization_schema_preserves_definition_and_new_metadata():
    schema = PlaybookDefinition.model_json_schema(mode="serialization")
    assert {"rules", "steps", "required_features", "trusted_execution"} <= set(schema["properties"])
    assert "rules" in schema["required"]


def test_compiler_cannot_supply_features_or_trusted_metadata(tmp_path):
    raw = twin()
    result = proposal(tmp_path, body={
        "rules": raw["rules"], "steps": raw["steps"],
        "required_features": ["future_v9"],
        "trusted_execution": {"principal": "local", "state_schema_version": 999},
        "state_schema_version": 888,
    })
    assert result.artifact.required_features == ()
    assert result.artifact.trusted_execution is None
    assert {d.field for d in result.diagnostics if d.code == "authority_field_ignored"} == {
        "/required_features", "/trusted_execution", "/state_schema_version",
    }


@pytest.mark.parametrize("location", ["input", "guard", "decision", "template"])
@pytest.mark.parametrize("path", ["observed_at", "observation_id"])
def test_infers_nested_expressions_and_observation_context(tmp_path, location, path):
    raw = twin()
    value = {"type": "len", "collection": {"type": "list", "items": [
        {"type": "context_ref", "path": path},
    ]}}
    condition = {"type": "exists", "value": value}
    if location == "guard":
        raw["rules"][0]["guard"] = condition
    elif location == "decision":
        step = raw["steps"]["act"]
        raw["steps"]["act"] = {
            "type": "decision", "rule": step["rule"], "title": step["title"],
            "source": step["source"], "cases": [{"when": condition, "goto": "done"}],
            "default": "done",
        }
    else:
        raw["steps"]["act"]["inputs"]["note"] = (
            {"type": "template", "parts": [value]} if location == "template" else value
        )
    result = proposal(tmp_path, body={"rules": raw["rules"], "steps": raw["steps"]})
    assert result.artifact.required_features == ("collections_v1", "observation_context_v1")


def test_nested_foreach_inference_uses_b2_graph_semantics():
    assert features.infer_required_features(nested_artifact()) == ("nested_foreach_v1",)
    assert features.infer_required_features(minimal_artifact()) == ()
    raw = nested_artifact().model_dump(mode="json")
    raw["steps"].pop("inner")
    raw["steps"]["outer"]["body_entry"] = "body"
    raw["steps"]["body"]["transitions"] = {"created": "outer", "reused": "outer"}
    raw["steps"]["body"]["inputs"]["title"] = {"type": "loop_ref", "binding": "row"}
    assert features.infer_required_features(PlaybookDefinition.model_validate(raw)) == ()


@pytest.mark.parametrize("namespace", ["aq_commands", "plugin_tools"])
def test_llm_tool_requirements_use_registered_contracts(namespace):
    raw = twin()
    step = raw["steps"]["act"]
    raw["steps"]["act"] = {
        "type": "llm", "rule": step["rule"], "title": step["title"], "source": step["source"],
        "profile_id": "fixture", "prompt": {"type": "literal", "value": "Inspect"},
        "output_schema": {"type": "object"}, "transitions": {"completed": "done"},
        "budget": {"max_calls": 1, "max_output_tokens": 100, "max_total_tokens": 200,
                   "timeout_seconds": 10},
        "tool_use": {"enabled": True, namespace: ["doctor", "playbook_state_get"]},
    }
    artifact = PlaybookDefinition.model_validate(raw)
    assert features.infer_required_features(artifact, contracts=RegistryContractLookup()) == (
        "generated_commands_v1", "state_v1",
    )


def test_generated_requirements_come_from_server_contracts(tmp_path):
    contracts = StubContracts()
    original = contracts.get("demo_command")
    contracts.get = lambda name: replace(original, required_features=("generated_commands_v1",))
    result = proposal(tmp_path, contracts=contracts)
    assert result.artifact.required_features == ("generated_commands_v1",)
    # Both a typed built-in and a generated built-in use the authoritative adapter.
    lookup = RegistryContractLookup()
    assert lookup.get("ensure_task").required_features == ()
    assert lookup.get("doctor").required_features == ("generated_commands_v1",)


@pytest.mark.parametrize("name,expected,supported", [
    ("playbook_state_get", "state_v1", True), ("playbook_script_run", "script_v1", False),
])
def test_command_hooks_require_complete_feature_support(name, expected, supported):
    raw = twin()
    raw["steps"]["act"]["command"] = name
    artifact = PlaybookDefinition.model_validate(raw)
    assert expected in features.infer_required_features(artifact)
    assert features.unsupported_features(artifact) == (() if supported else (expected,))


def test_state_metadata_is_source_derived_and_supported(tmp_path):
    result = proposal(tmp_path, frontmatter="state_schema_version: 7\n")
    assert result.artifact.trusted_execution.state_schema_version == 7
    assert result.artifact.required_features == ("state_v1",)
    assert result.activatable
    assert not any(d.code == "unsupported_feature" for d in result.diagnostics)
    warning = next(d for d in result.diagnostics if d.code == "state_namespace_migration")
    assert "resets suppression" in warning.message
    raw = twin()
    raw["steps"]["act"]["command"] = "playbook_state_get"
    result = proposal(tmp_path, body={"rules": raw["rules"], "steps": raw["steps"]})
    assert result.artifact.trusted_execution.state_schema_version == 1
    assert not any(d.code == "state_namespace_migration" for d in result.diagnostics)


@pytest.mark.parametrize("value", ["true", "0", "-1", '"2"', "1.5", "null"])
def test_source_state_version_is_strict(tmp_path, value):
    result = proposal(tmp_path, frontmatter=f"state_schema_version: {value}\n")
    assert result.artifact is None


def test_trusted_metadata_cannot_carry_authority():
    with pytest.raises(ValidationError):
        TrustedExecutionMetadata.model_validate({"state_schema_version": 1, "principal": "local"})


def test_unknown_features_load_for_inspection_but_fail_validation():
    artifact = definition(required_features=["future_v9"])
    assert load_definition_json(canonical_bytes(artifact).decode()) == artifact
    errors = validate_definition(
        artifact, contracts=StubContracts(), profiles=StubProfiles(), events=StubEvents(),
    )
    unsupported = [d for d in errors if d.code == "unsupported_feature"]
    assert len(unsupported) == 1
    assert unsupported[0].severity == "error"
    assert "future_v9" in unsupported[0].message


async def health_fixture(tmp_path):
    rows, artifacts = [], {}
    for name, pins in (("bad", ["future_v9"]), ("good", [])):
        artifact = definition(id=name, required_features=pins)
        ref = artifact_ref_for(artifact)
        path = tmp_path / f"{name}.json"
        path.write_bytes(canonical_bytes(artifact))
        artifacts[ref.artifact_sha256] = {**ref.as_dict(), "path": str(path), "validation": {}}
        rows.append({
            "playbook_id": name, "enabled": True, "scope": "system",
            "active_artifact_sha256": ref.artifact_sha256,
        })
    db = SimpleNamespace(
        list_playbook_activations=AsyncMock(return_value=rows),
        get_playbook_artifact_row=AsyncMock(side_effect=lambda sha: artifacts[sha]),
    )
    return db


async def test_health_keeps_compatible_sibling_ready_and_enabled(tmp_path):
    db = await health_fixture(tmp_path)
    bad, good = await load_activation_health(db, contracts=StubContracts(), profiles=StubProfiles())
    assert bad.health is ActivationHealth.INVALID
    assert bad.reasons[0].code == "unsupported_feature"
    assert bad.reasons[0].subject == "future_v9"
    assert good.health is ActivationHealth.READY
    assert bad.enabled and good.enabled


async def test_doctor_names_unsupported_features_and_upgrade_remedy(tmp_path, monkeypatch):
    db = await health_fixture(tmp_path)
    monkeypatch.setattr(importlib.import_module("src.doctor.playbook_v2_checks"), "_lookups", AsyncMock(
        return_value=(StubContracts(), StubProfiles()),
    ))
    result = await _check_activation_invalid(SimpleNamespace(
        db=db, config=SimpleNamespace(playbooks=SimpleNamespace(enabled=True)),
    ))
    assert result.data["count"] == 1
    assert result.data["invalid"][0]["reasons"][0]["subject"] == "future_v9"
    assert "upgrade all intended executors" in result.detail


async def test_import_refuses_unknown_features_before_writes():
    artifact = definition(required_features=["future_v9"])
    handler = _Handler(artifact, artifact_ref_for(artifact), [])
    handler.db.artifact_hash_lock = AsyncMock()
    handler.db.upsert_playbook_artifact = AsyncMock()
    result = await handler._v2_store_artifact(artifact, canonical_bytes(artifact))
    assert result["success"] is False
    assert any(d["code"] == "unsupported_feature" for d in result["diagnostics"])
    handler.db.artifact_hash_lock.assert_not_called()
    handler.db.upsert_playbook_artifact.assert_not_called()


async def test_activation_refuses_unknown_features_without_changing_current():
    artifact = definition(required_features=["future_v9"])
    ref = artifact_ref_for(artifact)
    handler = _Handler(artifact, ref, [[]])
    result = await handler._cmd_playbook_activate({
        "playbook_id": artifact.id, "artifact_sha256": ref.artifact_sha256,
    })
    assert result["blocked"] is True
    assert any("future_v9" in blocker for blocker in result["blockers"])
    handler.db.set_playbook_activation.assert_not_awaited()


@pytest.mark.parametrize("mode", list(ExecutionMode))
async def test_direct_execution_rejects_before_run_or_effect(mode):
    artifact = minimal_artifact().model_copy(update={"required_features": ("future_v9",)})
    engine, adapter, runs, _, ref = build(artifact=artifact)
    with pytest.raises(features.UnsupportedArtifactFeatures, match="future_v9"):
        await engine.run_rule(ref, "r", {}, TRUSTED_LOCAL, mode=mode)
    assert runs.create_calls == 0
    assert adapter.calls == []
    with pytest.raises(features.UnsupportedArtifactFeatures):
        await engine.dry_run(ref, {}, TRUSTED_LOCAL)


async def test_omitting_pins_cannot_bypass_unsupported_semantics(monkeypatch):
    artifact = nested_artifact()
    assert not artifact.required_features
    engine, adapter, runs, _, ref = build(artifact=artifact)
    monkeypatch.setattr(features, "SUPPORTED_FEATURES", features.SUPPORTED_FEATURES - {"nested_foreach_v1"})
    with pytest.raises(features.UnsupportedArtifactFeatures, match="nested_foreach_v1"):
        await engine.run_rule(ref, "r", {}, TRUSTED_LOCAL)
    assert runs.create_calls == 0
    assert adapter.calls == []


async def test_dispatch_rejects_unsupported_sibling_and_pinned_bypass():
    payload = minimal_artifact().model_dump(mode="json")
    payload["steps"]["ensure-review-task"]["inputs"] = {
        "project_id": {"type": "literal", "value": "p"},
        "title": {"type": "literal", "value": "Compatible sibling"},
    }
    good = PlaybookDefinition.model_validate(payload)
    bad = good.model_copy(update={"id": "future", "required_features": ("future_v9",)})
    engine, adapter, runs, _, good_ref = build(artifact=good)
    bad_ref = artifact_ref_for(bad)
    engine.services.artifact_store.put(bad)
    engine.activations.refs = [bad_ref, good_ref]
    adapter.queue.append(ok())
    event = {"_event_type": "task.completed", "event_id": "mixed"}
    result = await engine.dispatch_event(event, TRUSTED_LOCAL)
    assert len(result.run_ids) == 1
    assert len(adapter.calls) == 1
    assert runs.create_calls == 1
    assert result.pending[0].reason == "unsupported_feature"
    assert engine.activations.queued[0][0] == "future"
    result = await engine.dispatch_event(event, TRUSTED_LOCAL, artifact_sha256=bad_ref.artifact_sha256)
    assert not result.run_ids
    assert len(adapter.calls) == 1


async def test_resume_rechecks_executor_support_before_effect(monkeypatch):
    engine, adapter, runs, _, ref = nested_engine()
    first = await engine.run_rule(ref, "r", {}, TRUSTED_LOCAL)
    assert first.lifecycle is RunLifecycle.PAUSED
    before = len(runs.receipts)
    monkeypatch.setattr(features, "SUPPORTED_FEATURES", features.SUPPORTED_FEATURES - {"nested_foreach_v1"})
    with pytest.raises(features.UnsupportedArtifactFeatures):
        await engine.resume(first.run_id, TimerFired(wait_id=""), TRUSTED_LOCAL)
    assert len(runs.receipts) == before
    assert adapter.calls == []


@pytest.mark.parametrize("updates", [
    {"required_features": ["collections_v1"]},
    {"trusted_execution": {"state_schema_version": 2}},
])
def test_execution_metadata_changes_are_visible_in_both_review_diffs(updates):
    old, new = definition(), definition(**updates)
    diff = diff_definitions(old, new)
    assert diff.executable_change
    assert diff.execution_metadata[0].pointer == "/" + next(iter(updates))
    projected = diff_artifacts(
        old, new, base_ref=artifact_ref_for(old), target_ref=artifact_ref_for(new),
        contracts=StubContracts(), profiles=StubProfiles(),
    )
    assert projected["executable_change"]
    row = next(row for row in projected["rules"] if row["rule_id"] == "execution-metadata")
    assert row["field_changes"][0]["executable"]
