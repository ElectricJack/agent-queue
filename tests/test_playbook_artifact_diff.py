from __future__ import annotations

from pathlib import Path

from src.playbooks.artifact_diff import diff_artifacts
from src.playbooks.artifact_ref import ArtifactRef
from src.playbooks.definition import load_definition_json
from tests.playbook_v2_helpers import StubContracts, StubProfiles

FIXTURES = Path(__file__).parent / "fixtures" / "playbooks" / "v2"


def _load(name):
    return load_definition_json((FIXTURES / name).read_text())


def _ref(definition):
    return ArtifactRef(
        definition.id,
        definition.artifact_sha256(),
        definition.schema_version,
        definition.contract_fingerprint(),
        definition.source_hash,
        definition.compiler_build or "fixture",
        definition.compiled_at.isoformat(),
        definition.version,
    )


def _diff(base, target):
    return diff_artifacts(
        base,
        target,
        base_ref=_ref(base) if base else None,
        target_ref=_ref(target),
        contracts=StubContracts(),
        profiles=StubProfiles(),
    )


def test_executable_change_detected():
    diff = _diff(_load("review-pipeline.artifact.json"), _load("review-pipeline.v6.artifact.json"))
    assert diff["executable_change"] is True
    ensure = next(item for item in diff["steps"] if item["step_id"] == "ensure-review-task")
    assert any(item["executable"] for item in ensure["field_changes"])


def test_presentation_only_change_does_not_block():
    base = _load("review-pipeline.artifact.json")
    changed_step = base.steps["classify-risk"].model_copy(update={"title": "Clearer title"})
    target = base.model_copy(update={"steps": {**base.steps, "classify-risk": changed_step}})
    diff = _diff(base, target)
    assert diff["executable_change"] is False
    assert diff["presentation_change_count"] == 1


def test_first_artifact_diffs_against_none():
    target = _load("review-pipeline.artifact.json")
    diff = _diff(None, target)
    assert diff["base"] is None
    assert diff["executable_change"] is True
    assert all(item["change"] == "added" for item in diff["rules"])
