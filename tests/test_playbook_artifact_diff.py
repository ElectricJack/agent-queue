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


def _retrigger(base, rule_id, **update):
    """Return ``base`` with one rule replaced, keeping rule order."""
    rules = [rule.model_copy(update=update) if rule.id == rule_id else rule for rule in base.rules]
    return base.model_copy(update={"rules": rules})


def test_rule_field_change_is_itemized_not_just_counted():
    # A rule's trigger filter moving is executable: it changes which events the
    # rule fires on. The count alone forces an acknowledgement of something the
    # operator cannot see, so the row has to name the field and both values.
    base = _load("review-pipeline.artifact.json")
    rule = next(item for item in base.rules if item.id == "review-on-task-completed")
    trigger = rule.trigger.model_copy(update={"filter": {"review_task": True}})
    diff = _diff(base, _retrigger(base, rule.id, trigger=trigger))
    row = next(item for item in diff["rules"] if item["rule_id"] == rule.id)
    assert row["change"] == "modified"
    assert [item["path"] for item in row["field_changes"]] == ["/trigger/filter/review_task"]
    change = row["field_changes"][0]
    assert change["executable"] is True
    assert change["before"]["display"] == "false"
    assert change["after"]["display"] == "true"
    assert diff["semantic_change_count"] == 1
    assert diff["presentation_change_count"] == 0
    assert all(not item["field_changes"] for item in diff["rules"] if item["rule_id"] != rule.id)


def test_rule_presentation_field_change_is_itemized_and_does_not_block():
    base = _load("review-pipeline.artifact.json")
    diff = _diff(base, _retrigger(base, "sweep-on-spec-approved", description="Clearer prose"))
    row = next(item for item in diff["rules"] if item["rule_id"] == "sweep-on-spec-approved")
    assert [item["path"] for item in row["field_changes"]] == ["/description"]
    assert row["field_changes"][0]["executable"] is False
    assert diff["executable_change"] is False
    assert diff["semantic_change_count"] == 0
    assert diff["presentation_change_count"] == 1


def test_added_and_removed_rules_carry_no_field_rows():
    # An added rule's whole body is implied by its change tone; itemizing it
    # would move `semantic_change_count` past what the steps already report.
    base = _load("review-pipeline.artifact.json")
    target = base.model_copy(update={"rules": [base.rules[0]]})
    diff = _diff(base, target)
    rows = {item["rule_id"]: item for item in diff["rules"]}
    assert rows["sweep-on-spec-approved"]["change"] == "removed"
    assert rows["sweep-on-spec-approved"]["field_changes"] == []
    assert rows["review-on-task-completed"]["field_changes"] == []
    assert all(item["field_changes"] == [] for item in _diff(None, base)["rules"])


def test_unchanged_rule_has_no_field_rows():
    base = _load("review-pipeline.artifact.json")
    diff = _diff(base, base)
    assert all(item["change"] == "unchanged" for item in diff["rules"])
    assert all(item["field_changes"] == [] for item in diff["rules"])
    assert diff["semantic_change_count"] == 0
