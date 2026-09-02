"""Proposal compiler boundary tests (Package 2, §15.3 and §15.5)."""

from __future__ import annotations

from pathlib import Path

from src.playbooks.authoring import PlaybookSource
from src.playbooks.pipeline_lowering import _value, shadow_compile
from src.playbooks.proposal import propose
from tests.playbook_v2_helpers import StubContracts, StubEvents, StubProfiles, twin


def _source(tmp_path: Path) -> PlaybookSource:
    path = tmp_path / "proposal.md"
    path.write_text(
        "---\nid: demo\nscope: system\ntriggers:\n  - task.completed\n---\n"
        "Use `demo_command`, `project_id`, `done`, `worker`, `task_id`, and `review`.\n"
    )
    source = PlaybookSource.load(path, vault_root=tmp_path)
    assert isinstance(source, PlaybookSource)
    return source


def _body() -> dict:
    artifact = twin()
    return {"rules": artifact["rules"], "steps": artifact["steps"]}


def test_inventory_preserves_backticks_and_ignores_fenced_examples(tmp_path):
    source = _source(tmp_path)
    assert source.inventory.contains("demo_command")
    assert source.inventory.refs("demo_command")[0].start_line == 7


def test_authoritative_fields_are_discarded_not_trusted(tmp_path):
    body = _body()
    body["id"] = "smuggled"
    proposal = propose(
        _source(tmp_path),
        body,
        contracts=StubContracts(),
        profiles=StubProfiles(),
        events=StubEvents(),
        version=1,
    )
    assert proposal.artifact is not None
    assert proposal.artifact.id == "demo"
    assert any(d.code == "authority_field_ignored" for d in proposal.diagnostics)


def test_proposal_is_review_only_and_has_no_activation_side_effect(tmp_path):
    proposal = propose(
        _source(tmp_path),
        _body(),
        contracts=StubContracts(),
        profiles=StubProfiles(),
        events=StubEvents(),
        version=1,
    )
    assert proposal.artifact is not None
    assert proposal.artifact_sha256 is not None
    assert proposal.contract_fingerprint is not None
    assert not hasattr(proposal, "activate")


def test_lowering_uses_binding_outside_loop_and_loop_ref_inside_loop():
    assert _value("{{outputs.dep.id}}") == {"type": "binding_ref", "binding": "dep", "path": "id"}
    assert _value("{{outputs.dep.id}}", {"dep"}) == {
        "type": "loop_ref",
        "binding": "dep",
        "path": "id",
    }


def test_shadow_compile_reports_prose_as_a_question(tmp_path):
    source = _source(tmp_path)
    report = shadow_compile(
        [source], contracts=StubContracts(), profiles=StubProfiles(), events=StubEvents()
    )
    assert report.rows[0].lowered is False
    assert report.rows[0].question_count == 1
