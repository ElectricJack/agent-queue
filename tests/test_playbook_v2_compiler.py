"""Proposal compiler boundary tests (Package 2, §15.3 and §15.5)."""

from __future__ import annotations

import json
from pathlib import Path

from src.playbooks.authoring import PlaybookSource
from src.playbooks.pipeline_lowering import (
    _value,
    lower_assignment,
    lower_pipeline,
    shadow_compile,
)
from src.playbooks.proposal import DuplicateSemanticKey, load_semantic_body_json, propose
from src.playbooks.validation import (
    NullProfileLookup,
    RegisteredEventLookup,
    RegistryContractLookup,
)
from tests.playbook_v2_helpers import FIXTURE_DIR, StubContracts, StubEvents, StubProfiles, twin

LOWERING = FIXTURE_DIR / "lowering"


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
    hostile = json.loads((FIXTURE_DIR / "hostile-body.json").read_text())
    body.update({key: value for key, value in hostile.items() if key not in {"rules", "steps"}})
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
    ignored = {d.field for d in proposal.diagnostics if d.code == "authority_field_ignored"}
    assert {"/id", "/scope", "/enabled", "/compiled_against"} <= ignored
    assert not hasattr(proposal.artifact, "enabled")


def test_compiled_against_records_a_delegated_profile(tmp_path):
    """A literal `profile_id` argument is a profile dependency (`solid-harbor.54`).

    The shipped `default-pipeline` has no AI step at all: it depends on
    `reviewer` only by handing it a task through `ensure_task`.  Snapshotting
    just a step's *own* profile left that artifact with an empty map, so no
    capability change could ever stale it.
    """
    from tests.playbook_v2_helpers import stub_policies

    body = _body()
    body["steps"]["act"]["inputs"]["profile_id"] = {"type": "literal", "value": "reviewer"}
    path = tmp_path / "delegating.md"
    path.write_text(
        "---\nid: demo\nscope: system\ntriggers:\n  - task.completed\n---\n"
        "Use `demo_command`, `project_id`, `profile_id`, `done`, `worker`, "
        "`task_id`, and `review`.\n"
    )
    source = PlaybookSource.load(path, vault_root=tmp_path)
    assert isinstance(source, PlaybookSource)

    proposal = propose(
        source,
        body,
        contracts=StubContracts(),
        profiles=StubProfiles(),
        events=StubEvents(),
        version=1,
    )

    assert proposal.artifact is not None, [d.message for d in proposal.diagnostics]
    assert proposal.artifact.compiled_against.profiles == {
        "reviewer": stub_policies()["reviewer"].fingerprint()
    }


def test_a_computed_profile_id_is_not_fingerprinted(tmp_path):
    """Only a literal can be snapshotted: a run-chosen profile has no compile-time value."""
    body = _body()
    body["steps"]["act"]["inputs"]["profile_id"] = {"type": "event_ref", "path": "project_id"}
    path = tmp_path / "computed.md"
    path.write_text(
        "---\nid: demo\nscope: system\ntriggers:\n  - task.completed\n---\n"
        "Use `demo_command`, `project_id`, `profile_id`, `done`, `worker`, "
        "`task_id`, and `review`.\n"
    )
    source = PlaybookSource.load(path, vault_root=tmp_path)
    assert isinstance(source, PlaybookSource)

    proposal = propose(
        source,
        body,
        contracts=StubContracts(),
        profiles=StubProfiles(),
        events=StubEvents(),
        version=1,
    )

    assert proposal.artifact is not None, [d.message for d in proposal.diagnostics]
    assert proposal.artifact.compiled_against.profiles == {}


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


def _bundled(name: str) -> PlaybookSource:
    root = Path("src/prompts")
    loaded = PlaybookSource.load(root / "default_playbooks" / name, vault_root=root)
    assert isinstance(loaded, PlaybookSource)
    return loaded


def _frozen_v1_pipeline() -> PlaybookSource:
    """The pre-Package-6 `default-pipeline.md`, which still carries its graph.

    The shipped Markdown is a prose authoring source now, so the deterministic
    lowering assertions below run against the frozen snapshot — the same graph
    the reviewed V2 artifact was lowered from.  See
    `tests/fixtures/playbooks/v1/README.md`.
    """
    path = Path("tests/fixtures/playbooks/v1/default-pipeline.md")
    loaded = PlaybookSource.load(path, vault_root=path.parent)
    assert isinstance(loaded, PlaybookSource)
    return loaded


def test_shipped_pipeline_prose_needs_an_agent_proposal():
    """The shipped source has no machine graph, so lowering must ask for one."""
    row = shadow_compile(
        [_bundled("default-pipeline.md")],
        contracts=RegistryContractLookup(),
        profiles=NullProfileLookup(),
        events=RegisteredEventLookup(),
    ).rows[0]
    assert row.lowered is False
    assert row.question_count == 1
    assert row.artifact_sha256 is None


def test_default_pipeline_shadow_compile_is_clean_against_live_registries():
    row = shadow_compile(
        [_frozen_v1_pipeline()],
        contracts=RegistryContractLookup(),
        profiles=NullProfileLookup(),
        events=RegisteredEventLookup(),
    ).rows[0]
    assert row.error_count == row.warning_count == row.question_count == 0
    assert row.artifact_sha256 is not None


def test_default_pipeline_matches_the_deterministic_fixture():
    expected = json.loads((LOWERING / "default-pipeline.expected.json").read_text())
    body, diagnostics = lower_pipeline(_frozen_v1_pipeline())
    loops = sorted(key for key, step in body["steps"].items() if step["type"] == "foreach")
    assert {
        "artifact_diagnostics": [diagnostic.code for diagnostic in diagnostics],
        "loop_ids": loops,
        "rule_count": len(body["rules"]),
        "step_count": len(body["steps"]),
    } == expected


def test_command_transitions_use_closed_registered_outcomes():
    body, diagnostics = lower_pipeline(_frozen_v1_pipeline())
    assert diagnostics == []
    ensure = body["steps"]["per-task-review--create-review"]
    assert set(ensure["transitions"]) == {"created", "reused", "rejected", "runtime_error"}
    commit = body["steps"]["commit-on-gate-resolve--commit_proposal"]
    assert set(commit["transitions"]) == {"committed", "rejected", "runtime_error"}


def test_foreach_bodies_reenter_the_foreach_node():
    body, _ = lower_pipeline(_frozen_v1_pipeline())
    loops = {key: step for key, step in body["steps"].items() if step["type"] == "foreach"}
    assert len(loops) == 2
    for loop_id, loop in loops.items():
        body_step = body["steps"][loop["body_entry"]]
        assert set(body_step["transitions"].values()) == {loop_id}


def test_default_assignment_lowers_to_one_ai_node():
    expected = json.loads(
        (LOWERING / "default-assignment-routing.expected.json").read_text()
    )
    body, diagnostics = lower_assignment(_bundled("default-assignment-routing.md"))
    assert diagnostics == []
    step_types = [step["type"] for step in body["steps"].values()]
    assert {
        "executable_step_count": step_types.count("llm"),
        "rule_count": len(body["rules"]),
        "step_count": len(body["steps"]),
        "step_types": step_types,
    } == expected

    choose = body["steps"]["assignment-route--choose"]
    assert choose["inputs"] == {
        "tasks": {"type": "event_ref", "path": "tasks"},
        "options": {"type": "event_ref", "path": "options"},
        "options_hash": {"type": "event_ref", "path": "options_hash"},
        "catalog_hash": {"type": "event_ref", "path": "catalog_hash"},
    }
    assert choose["save_result_as"] == "routing_result"
    schema = choose["output_schema"]
    assert schema["required"] == ["decisions"]
    assert schema["additionalProperties"] is False
    decision = schema["properties"]["decisions"]["items"]
    assert decision["required"] == [
        "task_id", "input_hash", "intelligence_class", "reason"
    ]
    assert decision["additionalProperties"] is False
    assert body["steps"]["assignment-route--done"]["result"] == {
        "type": "binding_ref",
        "binding": "routing_result",
    }


def test_non_loop_output_reference_lowers_to_binding_ref():
    source = PlaybookSource.load(
        LOWERING / "output-ref-no-loop.pipeline.md", vault_root=FIXTURE_DIR
    )
    assert isinstance(source, PlaybookSource)
    body, _ = lower_pipeline(source)
    value = body["steps"]["route--gate"]["inputs"]["waiter_task_ids"]
    assert value == {
        "type": "list",
        "items": [{"type": "binding_ref", "binding": "dep", "path": "id"}],
    }


def test_loop_item_reference_lowers_to_loop_ref_without_overcorrecting():
    source = PlaybookSource.load(
        LOWERING / "output-ref-in-loop.pipeline.md", vault_root=FIXTURE_DIR
    )
    assert isinstance(source, PlaybookSource)
    body, _ = lower_pipeline(source)
    step = body["steps"]["route--gate-body"]
    assert step["inputs"]["waiter_task_ids"]["items"][0]["type"] == "loop_ref"
    assert step["inputs"]["title"]["type"] == "binding_ref"


def test_compiler_reports_and_bounds_hostile_source_refs(tmp_path):
    hostile = json.loads((FIXTURE_DIR / "compiler-only-diagnostics.json").read_text())
    body = _body()
    body["steps"]["act"]["source"] = {**hostile["source"], "excerpt": hostile["excerpt"]}
    proposal = propose(
        _source(tmp_path),
        body,
        contracts=StubContracts(),
        profiles=StubProfiles(),
        events=StubEvents(),
        version=1,
    )
    codes = {diagnostic.code for diagnostic in proposal.diagnostics}
    assert set(hostile["expected_codes"]) <= codes
    assert proposal.artifact is not None
    assert len(proposal.artifact.steps["act"].source.excerpt or "") == 400


def test_duplicate_semantic_keys_are_rejected_before_authority_stripping():
    text = (FIXTURE_DIR / "hostile-body.duplicate-keys.json").read_text(encoding="utf-8")
    try:
        load_semantic_body_json(text)
    except DuplicateSemanticKey:
        pass
    else:  # pragma: no cover - makes the security boundary explicit
        raise AssertionError("duplicate key was accepted")
