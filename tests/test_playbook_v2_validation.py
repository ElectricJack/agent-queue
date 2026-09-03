"""§6 — whole-graph validation.

Child plan ``docs/superpowers/plans/2026-09-01-playbook-v2-typed-model-compiler.md``
§6.1 (structure), §6.2 (rule ownership and closure), §6.3 (loops), §6.4 (definite
assignment), §6.5 (value typing), §6.6 (contracts and outcomes), §6.7 (profiles
and capabilities) and §6.8 (the closed diagnostic set).
"""

from __future__ import annotations

import copy
import json
import time

import pytest

from src.playbooks.definition import (
    DuplicateJsonKey,
    PlaybookDefinition,
    load_definition_json,
)
from src.playbooks.validation import (
    COMPILER_ONLY_CODES,
    DIAGNOSTIC_CODES,
    DIAGNOSTIC_SEVERITY,
    MODEL_CODES,
    VALIDATOR_CODES,
    NullContractLookup,
    NullProfileLookup,
    RegisteredEventLookup,
    RegistryContractLookup,
    severity_of,
    validate_definition,
)
from tests.playbook_v2_helpers import (
    GOLDEN,
    INVALID_DIR,
    StubEvents,
    StubInventory,
    StubProfiles,
    check,
    codes,
    errors,
    expected_invalid_files,
    source,
    twin,
)

#: Every executable name the twin and its mutations legitimately use.
TWIN_INVENTORY = StubInventory(
    {
        "demo_command",
        "other_command",
        "task.completed",
        "spec.approved",
        "project_id",
        "title",
        "task_id",
        "note",
        "bound",
        "item",
        "leaf",
        "worker",
        "wide",
        "hollow",
        "ghost",
        "risk",
        "low",
        "high",
        "agent_id",
        "not_a_field",
        "no_such_command",
        "no.such.event",
        "nonesuch",
    }
)


def build(mutation=None) -> PlaybookDefinition:
    artifact = twin()
    if mutation is not None:
        mutation(artifact)
    return PlaybookDefinition.model_validate(artifact)


def load_invalid(code: str) -> PlaybookDefinition:
    return load_definition_json((INVALID_DIR / f"{code}.json").read_text())


class TestDiagnosticSet:
    """§6.8 — the closed 48-code set.

    47 in the child plan; ``narrowing_not_subset`` is the 48th, added with the
    authoring surface for ``AgentTaskStep.capability_narrowing``.
    """

    def test_the_set_is_closed_and_partitioned(self):
        assert len(DIAGNOSTIC_CODES) == 48
        assert VALIDATOR_CODES | MODEL_CODES | COMPILER_ONLY_CODES == DIAGNOSTIC_CODES
        assert not VALIDATOR_CODES & MODEL_CODES
        assert not VALIDATOR_CODES & COMPILER_ONLY_CODES
        assert not MODEL_CODES & COMPILER_ONLY_CODES

    def test_severities_match_the_plan(self):
        assert severity_of("type_unknown") == "info"
        assert severity_of("delegation_runtime_checked") == "info"
        assert severity_of("profile_capability_empty") == "warning"
        assert severity_of("ambiguous_prose") == "question"
        assert severity_of("cross_rule_transition") == "error"
        assert set(DIAGNOSTIC_SEVERITY) <= DIAGNOSTIC_CODES

    def test_every_registered_code_has_a_fixture(self):
        """A new check cannot ship without a fixture that reaches it."""
        present = {path.stem for path in INVALID_DIR.glob("*.json")}
        assert VALIDATOR_CODES | MODEL_CODES == present

    def test_the_compiler_only_codes_are_never_emitted_here(self):
        """``validate_definition`` takes an artifact, not the Markdown it came
        from, so the source-dependent codes belong to the compiler slice."""
        emitted: set[str] = set()
        for path in sorted(INVALID_DIR.glob("*.json")):
            if path.stem in MODEL_CODES:
                continue
            emitted |= codes(check(load_definition_json(path.read_text()),
                                  inventory=TWIN_INVENTORY))
        assert not emitted & COMPILER_ONLY_CODES

    def test_no_pass_emits_an_unregistered_code(self):
        emitted: set[str] = set()
        for path in sorted(INVALID_DIR.glob("*.json")):
            if path.stem in MODEL_CODES:
                continue
            emitted |= codes(check(load_definition_json(path.read_text()),
                                   inventory=TWIN_INVENTORY))
        assert emitted <= DIAGNOSTIC_CODES


class TestInvalidFixtures:
    """§9.3 — one minimal fixture per rejection, each a single-defect delta."""

    @pytest.mark.parametrize("code", sorted(VALIDATOR_CODES))
    def test_each_fixture_reports_its_own_code(self, code):
        found = check(load_invalid(code), inventory=TWIN_INVENTORY)
        assert code in codes(found), sorted(codes(found))

    def test_the_valid_twin_is_clean(self):
        found = check(build(), inventory=TWIN_INVENTORY)
        assert errors(found) == []

    @pytest.mark.parametrize("code", sorted(MODEL_CODES))
    def test_the_model_rejects_its_fixture_before_validation(self, code):
        text = (INVALID_DIR / f"{code}.json").read_text()
        with pytest.raises((DuplicateJsonKey, ValueError)) as caught:
            load_definition_json(text)
        assert code in (getattr(caught.value, "code", None) or str(caught.value))

    def test_the_checked_in_fixtures_match_their_generator(self):
        """The files are reviewable data; the table beside them is how they are
        regenerated.  A drift is a diff, not a silent divergence."""
        for name, text in expected_invalid_files().items():
            assert (INVALID_DIR / name).read_text() == text, name


class TestStructure:
    """§6.1."""

    def test_a_step_naming_no_rule_is_reported(self):
        found = check(load_invalid("step_rule_unknown"), inventory=TWIN_INVENTORY)
        assert {"step_rule_unknown", "orphan_step"} <= codes(found)

    def test_an_entry_step_owned_by_another_rule(self):
        found = check(load_invalid("rule_entry_not_owned"))
        assert "rule_entry_not_owned" in codes(found)

    def test_a_missing_transition_target_names_the_field(self):
        found = check(load_invalid("unknown_step_target"))
        diagnostic = next(d for d in found if d.code == "unknown_step_target")
        assert diagnostic.field == "/transitions/done"
        assert diagnostic.step_id == "act"
        assert diagnostic.source is not None

    def test_a_duplicate_rule_id_is_reported_once(self):
        found = [d for d in check(load_invalid("duplicate_rule_id")) if d.code == "duplicate_rule_id"]
        assert len(found) == 1


class TestRuleClosure:
    """§6.2 — a rule owns a closed subgraph."""

    def test_no_edge_may_cross_a_rule_boundary(self):
        found = check(load_invalid("cross_rule_transition"))
        assert "cross_rule_transition" in codes(found)

    def test_a_cross_rule_edge_is_not_traversed_into_the_other_rule(self):
        """One mis-wired edge must not drag another rule's subgraph in."""
        found = check(load_invalid("cross_rule_transition"))
        assert not any(d.code == "orphan_step" and d.step_id == "other-end" for d in found)

    def test_an_owned_but_unreachable_step_names_its_owner(self):
        found = check(load_invalid("unreachable_step"))
        diagnostic = next(d for d in found if d.code == "unreachable_step")
        assert diagnostic.step_id == "lonely"
        assert "r1" in diagnostic.message

    def test_a_step_with_no_terminal_path_is_an_error_not_a_warning(self):
        found = check(load_invalid("no_terminal_path"))
        diagnostic = next(d for d in found if d.code == "no_terminal_path")
        assert diagnostic.severity == "error"

    def test_the_golden_artifact_closes_both_rules(self):
        golden = load_definition_json(GOLDEN.read_text())
        found = check(golden)
        assert not {"cross_rule_transition", "orphan_step", "unreachable_step",
                    "no_terminal_path"} & codes(found)


class TestLoops:
    """§6.3."""

    def test_nested_loops_are_rejected(self):
        assert "nested_loop" in codes(check(load_invalid("nested_loop")))

    def test_a_body_step_may_not_leave_by_an_undeclared_exit(self):
        found = check(load_invalid("loop_body_escapes"))
        diagnostic = next(d for d in found if d.code == "loop_body_escapes")
        assert diagnostic.step_id == "body"

    def test_a_body_step_may_take_one_of_the_loops_own_exits(self):
        """The golden loop's body maps ``runtime_error`` to the loop's own
        failure terminal — a declared exit, not an escape."""
        golden = load_definition_json(GOLDEN.read_text())
        assert "loop_body_escapes" not in codes(check(golden))

    def test_a_body_step_may_re_enter_the_loop_node(self):
        golden = load_definition_json(GOLDEN.read_text())
        assert golden.steps["check-gate"].default == "for-each-task"
        assert "loop_body_escapes" not in codes(check(golden))

    def test_a_redundant_continuation_is_checked_not_trusted(self):
        assert "continuation_mismatch" in codes(check(load_invalid("continuation_mismatch")))

    def test_a_loop_variable_may_not_shadow_a_binding(self):
        assert "loop_variable_shadow" in codes(check(load_invalid("loop_variable_shadow")))

    @pytest.mark.parametrize("reserved", ["event", "context", "loop", "run", "rule", "step"])
    def test_a_loop_variable_may_not_take_a_reserved_root(self, reserved):
        def mutate(artifact):
            artifact["steps"]["act"]["transitions"].update(done="loop", skipped="loop")
            artifact["steps"]["loop"] = {
                "type": "foreach",
                "rule": "r1",
                "title": "Loop",
                "collection": {"type": "binding_ref", "binding": "bound", "path": "tasks"},
                "item_binding": reserved,
                "failure_policy": "halt",
                "body_entry": "body",
                "transitions": {"completed": "end", "failed": "oops", "runtime_error": "oops"},
                "source": source(6),
            }
            artifact["steps"]["body"] = {
                "type": "command",
                "rule": "r1",
                "title": "Body",
                "command": "demo_command",
                "inputs": {"project_id": {"type": "event_ref", "path": "project_id"}},
                "transitions": {"done": "loop", "skipped": "loop", "runtime_error": "oops"},
                "source": source(7),
            }

        assert "loop_variable_shadow" in codes(check(build(mutate)))

    def test_a_loop_ref_outside_its_loop_is_rejected(self):
        assert "loop_ref_outside_loop" in codes(check(load_invalid("loop_ref_outside_loop")))

    def test_a_loop_ref_inside_its_loop_is_accepted(self):
        golden = load_definition_json(GOLDEN.read_text())
        assert "loop_ref_outside_loop" not in codes(check(golden))


class TestDefiniteAssignment:
    """§6.4 — the six cases the plan names, one test each."""

    def _artifact(self, *, assign_on_both: bool, read_in_body: bool = False,
                  read_on_continuation: bool = False) -> PlaybookDefinition:
        artifact = twin()
        artifact["steps"]["act"].pop("save_result_as")
        artifact["steps"]["act"]["transitions"] = {
            "done": "left",
            "skipped": "right",
            "runtime_error": "oops",
        }

        def command(step_id: str, line: int, *, save: str | None, target: str, read: bool = False):
            step = {
                "type": "command",
                "rule": "r1",
                "title": step_id,
                "command": "demo_command",
                "inputs": {"project_id": {"type": "event_ref", "path": "project_id"}},
                "transitions": {"done": target, "skipped": target, "runtime_error": "oops"},
                "source": source(line),
            }
            if save:
                step["save_result_as"] = save
            if read:
                step["inputs"]["note"] = {
                    "type": "binding_ref",
                    "binding": "bound",
                    "path": "task_id",
                }
            return step

        artifact["steps"]["left"] = command("left", 5, save="bound", target="join")
        artifact["steps"]["right"] = command(
            "right", 6, save="bound" if assign_on_both else None, target="join"
        )
        artifact["steps"]["join"] = command("join", 7, save=None, target="end", read=True)
        return PlaybookDefinition.model_validate(artifact)

    def test_a_straight_line_read_is_legal(self):
        def mutate(artifact):
            artifact["steps"]["act"]["transitions"].update(done="read", skipped="read")
            artifact["steps"]["read"] = {
                "type": "command",
                "rule": "r1",
                "title": "Read",
                "command": "demo_command",
                "inputs": {
                    "project_id": {"type": "event_ref", "path": "project_id"},
                    "note": {"type": "binding_ref", "binding": "bound", "path": "task_id"},
                },
                "transitions": {"done": "end", "skipped": "end", "runtime_error": "oops"},
                "source": source(5),
            }

        assert "binding_not_definitely_assigned" not in codes(check(build(mutate)))

    def test_one_branch_assigning_is_an_error_naming_the_omitting_branch(self):
        found = check(self._artifact(assign_on_both=False))
        diagnostic = next(d for d in found if d.code == "binding_not_definitely_assigned")
        assert diagnostic.step_id == "join"
        assert "right" in diagnostic.message

    def test_both_branches_assigning_the_same_name_is_duplicate_binding(self):
        found = check(self._artifact(assign_on_both=True))
        assert "duplicate_binding" in codes(found)

    def test_a_loop_back_re_reads_its_own_assigner(self):
        """The golden rule loops ``await-approval`` back to the assigner of
        ``review`` and still reads ``review`` — legal, because the optimistic
        must-analysis does not pessimise a back edge."""
        golden = load_definition_json(GOLDEN.read_text())
        assert "binding_not_definitely_assigned" not in codes(check(golden))

    def test_the_aggregate_is_invisible_inside_the_loop_body(self):
        def mutate(artifact):
            artifact["steps"]["act"]["transitions"].update(done="loop", skipped="loop")
            artifact["steps"]["loop"] = {
                "type": "foreach",
                "rule": "r1",
                "title": "Loop",
                "collection": {"type": "binding_ref", "binding": "bound", "path": "tasks"},
                "item_binding": "item",
                "failure_policy": "collect",
                "body_entry": "body",
                "continuation": "end",
                "save_result_as": "aggregate",
                "transitions": {"completed": "end", "failed": "oops", "runtime_error": "oops"},
                "source": source(6),
            }
            artifact["steps"]["body"] = {
                "type": "command",
                "rule": "r1",
                "title": "Body",
                "command": "demo_command",
                "inputs": {
                    "project_id": {"type": "event_ref", "path": "project_id"},
                    "note": {"type": "binding_ref", "binding": "aggregate", "path": "total"},
                },
                "transitions": {"done": "loop", "skipped": "loop", "runtime_error": "oops"},
                "source": source(7),
            }

        found = check(build(mutate))
        diagnostic = next(d for d in found if d.code == "binding_not_definitely_assigned")
        assert diagnostic.step_id == "body"

    def test_the_aggregate_is_readable_on_the_continuation_edge(self):
        def mutate(artifact):
            artifact["steps"]["act"]["transitions"].update(done="loop", skipped="loop")
            artifact["steps"]["loop"] = {
                "type": "foreach",
                "rule": "r1",
                "title": "Loop",
                "collection": {"type": "binding_ref", "binding": "bound", "path": "tasks"},
                "item_binding": "item",
                "failure_policy": "collect",
                "body_entry": "body",
                "continuation": "after",
                "save_result_as": "aggregate",
                "transitions": {"completed": "after", "failed": "oops", "runtime_error": "oops"},
                "source": source(6),
            }
            artifact["steps"]["body"] = {
                "type": "command",
                "rule": "r1",
                "title": "Body",
                "command": "demo_command",
                "inputs": {"project_id": {"type": "event_ref", "path": "project_id"}},
                "transitions": {"done": "loop", "skipped": "loop", "runtime_error": "oops"},
                "source": source(7),
            }
            artifact["steps"]["after"] = {
                "type": "command",
                "rule": "r1",
                "title": "After",
                "command": "demo_command",
                "inputs": {
                    "project_id": {"type": "event_ref", "path": "project_id"},
                    "note": {"type": "binding_ref", "binding": "aggregate", "path": "total"},
                },
                "transitions": {"done": "end", "skipped": "end", "runtime_error": "oops"},
                "source": source(8),
            }

        assert "binding_not_definitely_assigned" not in codes(check(build(mutate)))

    def test_a_binding_colliding_with_an_enclosing_loop_variable(self):
        assert "binding_reassigned" in codes(check(load_invalid("binding_reassigned")))

    def test_a_guard_may_not_read_a_binding(self):
        def mutate(artifact):
            artifact["rules"][0]["guard"] = {
                "type": "exists",
                "value": {"type": "binding_ref", "binding": "bound"},
            }

        found = check(build(mutate))
        diagnostic = next(d for d in found if d.code == "binding_not_definitely_assigned")
        assert diagnostic.step_id is None


class TestValueTypingDiagnostics:
    """§6.5."""

    def test_an_unregistered_event_is_an_error(self):
        found = check(load_invalid("unknown_event"))
        assert "unknown_event" in codes(found)

    def test_an_undeclared_event_field_is_an_error(self):
        assert "unknown_event_field" in codes(check(load_invalid("unknown_event_field")))

    def test_an_undeclared_trigger_filter_key_is_an_error(self):
        def mutate(artifact):
            artifact["rules"][0]["trigger"]["filter"] = {"nope": "x"}

        found = check(build(mutate))
        diagnostic = next(d for d in found if d.code == "unknown_event_field")
        assert diagnostic.field == "/trigger/filter/nope"

    def test_a_declared_trigger_filter_key_is_accepted(self):
        def mutate(artifact):
            artifact["rules"][0]["trigger"]["filter"] = {"agent_id": "a1"}

        assert "unknown_event_field" not in codes(check(build(mutate)))

    def test_an_unknown_context_path_is_an_error(self):
        assert "unknown_context_path" in codes(check(load_invalid("unknown_context_path")))

    def test_iteration_index_outside_a_loop_is_an_error(self):
        def mutate(artifact):
            artifact["steps"]["act"]["inputs"]["note"] = {
                "type": "context_ref",
                "path": "iteration_index",
            }

        found = check(build(mutate))
        assert "unknown_context_path" in codes(found)

    def test_a_concrete_mismatch_is_an_error(self):
        found = check(load_invalid("type_mismatch"))
        diagnostic = next(d for d in found if d.code == "type_mismatch")
        assert diagnostic.field == "/inputs/project_id"

    def test_a_silenced_check_is_visible_as_info(self):
        found = check(load_invalid("type_unknown"))
        diagnostic = next(d for d in found if d.code == "type_unknown")
        assert diagnostic.severity == "info"

    def test_a_coalesce_that_can_still_be_null_is_rejected(self):
        assert "coalesce_not_total" in codes(check(load_invalid("coalesce_not_total")))

    def test_a_total_coalesce_is_accepted(self):
        def mutate(artifact):
            artifact["steps"]["act"]["inputs"]["note"] = {
                "type": "coalesce",
                "options": [
                    {"type": "event_ref", "path": "note"},
                    {"type": "literal", "value": "fallback"},
                ],
            }

        assert "coalesce_not_total" not in codes(check(build(mutate)))


class TestOutcomeMapping:
    """§4.6 / §6.6."""

    def test_a_business_outcome_must_have_a_transition(self):
        found = check(load_invalid("unmapped_business_outcome"))
        diagnostic = next(d for d in found if d.code == "unmapped_business_outcome")
        assert "skipped" in diagnostic.message

    def test_reserved_outcomes_may_be_covered_by_runtime_error(self):
        assert "unmapped_reserved_outcome" not in codes(check(build()))

    def test_without_runtime_error_every_reserved_outcome_must_be_mapped(self):
        found = check(load_invalid("unmapped_reserved_outcome"))
        reported = {
            d.message.split("'")[1]
            for d in found
            if d.code == "unmapped_reserved_outcome"
        }
        from src.playbooks.definition import RESERVED_OUTCOMES

        assert reported == set(RESERVED_OUTCOMES)

    def test_a_transition_key_outside_the_closed_set_is_rejected(self):
        assert "unknown_transition_outcome" in codes(check(load_invalid(
            "unknown_transition_outcome"
        )))

    def test_an_llm_may_not_branch_without_a_declared_enum(self):
        assert "llm_branch_without_schema" in codes(check(load_invalid(
            "llm_branch_without_schema"
        )))

    def test_the_enum_must_equal_the_branching_keys_exactly(self):
        assert "outcome_enum_mismatch" in codes(check(load_invalid("outcome_enum_mismatch")))

    def test_the_outcome_field_must_be_required_in_the_schema(self):
        def mutate(artifact):
            artifact["steps"]["act"]["transitions"] = {
                "done": "classify",
                "skipped": "classify",
                "runtime_error": "oops",
            }
            artifact["steps"]["classify"] = {
                "type": "llm",
                "rule": "r1",
                "title": "Classify",
                "profile_id": "worker",
                "prompt": {"type": "literal", "value": "go"},
                "output_schema": {
                    "type": "object",
                    "properties": {"risk": {"enum": ["low", "high"]}},
                },
                "outcome_field": "risk",
                "budget": {
                    "max_calls": 1,
                    "max_output_tokens": 64,
                    "max_total_tokens": 128,
                    "timeout_seconds": 30,
                },
                "transitions": {"low": "end", "high": "end", "runtime_error": "oops"},
                "source": source(5),
            }

        found = check(build(mutate))
        assert any(
            d.code == "outcome_enum_mismatch" and d.field == "/output_schema/required"
            for d in found
        )

    def test_an_unresolved_command_suppresses_its_outcome_check(self):
        """``unknown_command`` is the actionable message; a cascade of
        ``unknown_transition_outcome`` on top of it is noise."""
        found = codes(check(load_invalid("unknown_command")))
        assert "unknown_command" in found
        assert "unknown_transition_outcome" not in found


class TestContracts:
    """§6.6 — arguments and fingerprints."""

    def test_an_unknown_command_is_an_error_not_a_pass(self):
        assert "unknown_command" in codes(check(load_invalid("unknown_command")))

    def test_the_null_lookup_degrades_toward_error(self):
        """§3.3 — there is no flag that makes an unresolvable reference pass."""
        found = validate_definition(
            build(), contracts=NullContractLookup(), profiles=NullProfileLookup(),
            events=StubEvents(),
        )
        assert "unknown_command" in codes(found)

    def test_a_missing_required_argument_is_reported(self):
        assert "argument_missing" in codes(check(load_invalid("argument_missing")))

    def test_an_undeclared_argument_is_reported(self):
        found = check(load_invalid("argument_unknown"))
        diagnostic = next(d for d in found if d.code == "argument_unknown")
        assert diagnostic.field == "/inputs/nonesuch"

    def test_a_drifted_fingerprint_is_stale_contract(self):
        assert "stale_contract" in codes(check(load_invalid("stale_contract")))

    def test_a_missing_fingerprint_is_stale_contract(self):
        def mutate(artifact):
            artifact["compiled_against"]["commands"].clear()

        assert "stale_contract" in codes(check(build(mutate)))


class TestOutputSchemaBounds:
    """§10.3 — author-supplied JSON Schema is bounded."""

    def test_an_invalid_schema_is_reported(self):
        assert "output_schema_invalid" in codes(check(load_invalid("output_schema_invalid")))

    def test_an_over_deep_schema_is_reported(self):
        assert "output_schema_too_deep" in codes(check(load_invalid("output_schema_too_deep")))

    @pytest.mark.parametrize("keyword", ["$ref", "$dynamicRef", "unevaluatedProperties"])
    def test_the_forbidden_keywords_are_rejected(self, keyword):
        def mutate(artifact):
            artifact["steps"]["act"]["transitions"] = {
                "done": "classify",
                "skipped": "classify",
                "runtime_error": "oops",
            }
            artifact["steps"]["classify"] = {
                "type": "llm",
                "rule": "r1",
                "title": "Classify",
                "profile_id": "worker",
                "prompt": {"type": "literal", "value": "go"},
                "output_schema": {
                    "type": "object",
                    "properties": {"risk": {"type": "string"}},
                    keyword: True if keyword == "unevaluatedProperties" else "#/$defs/x",
                },
                "budget": {
                    "max_calls": 1,
                    "max_output_tokens": 64,
                    "max_total_tokens": 128,
                    "timeout_seconds": 30,
                },
                "transitions": {"runtime_error": "oops"},
                "source": source(5),
            }

        found = codes(check(build(mutate)))
        assert "output_schema_too_deep" in found or "output_schema_invalid" in found


class TestProfilesAndCapabilities:
    """§6.7."""

    def test_an_unknown_profile_is_an_error(self):
        assert "unknown_profile" in codes(check(load_invalid("unknown_profile")))

    def test_a_deny_all_profile_is_a_warning_not_an_error(self):
        found = check(load_invalid("profile_capability_empty"))
        diagnostic = next(d for d in found if d.code == "profile_capability_empty")
        assert diagnostic.severity == "warning"

    def test_tool_use_cannot_exceed_the_step_profile(self):
        assert "tool_use_not_subset" in codes(check(load_invalid("tool_use_not_subset")))

    def test_tool_use_within_the_profile_is_accepted(self):
        def mutate(artifact):
            artifact["steps"]["act"]["transitions"] = {
                "done": "classify",
                "skipped": "classify",
                "runtime_error": "oops",
            }
            artifact["steps"]["classify"] = {
                "type": "llm",
                "rule": "r1",
                "title": "Classify",
                "profile_id": "worker",
                "prompt": {"type": "literal", "value": "go"},
                "output_schema": {"type": "object", "properties": {}},
                "tool_use": {"enabled": True, "aq_commands": ["demo_command"]},
                "budget": {
                    "max_calls": 1,
                    "max_output_tokens": 64,
                    "max_total_tokens": 128,
                    "timeout_seconds": 30,
                },
                "transitions": {"runtime_error": "oops"},
                "source": source(5),
            }

        assert "tool_use_not_subset" not in codes(check(build(mutate)))

    def test_an_agent_task_cannot_widen_the_ai_context(self):
        assert "capability_not_subset" in codes(check(load_invalid("capability_not_subset")))

    def test_a_narrowing_agent_task_is_accepted(self):
        artifact = json.loads((INVALID_DIR / "capability_not_subset.json").read_text())
        artifact["steps"]["classify"]["profile_id"] = "wide"
        artifact["steps"]["delegate"]["profile_id"] = "worker"
        found = check(PlaybookDefinition.model_validate(artifact))
        assert "capability_not_subset" not in codes(found)

    def test_a_narrowing_naming_an_ungranted_capability_is_an_error(self):
        """The executor intersects, so this would otherwise be a silent no-op."""
        found = check(load_invalid("narrowing_not_subset"), inventory=TWIN_INVENTORY)
        diagnostic = next(d for d in found if d.code == "narrowing_not_subset")
        assert diagnostic.severity == "error"
        assert diagnostic.step_id == "delegate"
        assert diagnostic.field == "/capability_narrowing/aq_commands"
        assert "other_command" in diagnostic.message

    def test_a_narrowing_inside_the_child_profile_is_accepted(self):
        artifact = json.loads((INVALID_DIR / "narrowing_not_subset.json").read_text())
        artifact["steps"]["delegate"]["capability_narrowing"] = {
            "aq_commands": ["demo_command"]
        }
        found = check(PlaybookDefinition.model_validate(artifact))
        assert "narrowing_not_subset" not in codes(found)

    def test_an_empty_narrowing_namespace_means_none_not_a_violation(self):
        """``[]`` is deny-all for that namespace, which is always a subset."""
        artifact = json.loads((INVALID_DIR / "narrowing_not_subset.json").read_text())
        artifact["steps"]["delegate"]["capability_narrowing"] = {
            "aq_commands": [],
            "harness_tools": None,
        }
        found = check(PlaybookDefinition.model_validate(artifact))
        assert "narrowing_not_subset" not in codes(found)

    def test_no_narrowing_emits_nothing(self):
        artifact = json.loads((INVALID_DIR / "narrowing_not_subset.json").read_text())
        artifact["steps"]["delegate"].pop("capability_narrowing")
        found = check(PlaybookDefinition.model_validate(artifact))
        assert "narrowing_not_subset" not in codes(found)

    def test_a_deferred_delegation_is_visible_as_info(self):
        found = check(load_invalid("delegation_runtime_checked"))
        diagnostic = next(d for d in found if d.code == "delegation_runtime_checked")
        assert diagnostic.severity == "info"


class TestIdentifierInventory:
    """§5.3 — the compiler may only wire together names a human wrote."""

    def test_a_narrowing_capability_absent_from_the_source_is_rejected(self):
        """§5.3 covers the third intersection term: a compiler may not invent a
        restriction on a delegated child any more than it may invent a grant."""
        inventory = StubInventory(TWIN_INVENTORY.names - {"other_command"})
        found = check(load_invalid("narrowing_not_subset"), inventory=inventory)
        diagnostic = next(
            d
            for d in found
            if d.code == "unknown_identifier" and "other_command" in d.message
        )
        assert diagnostic.step_id == "delegate"

    def test_a_narrowing_capability_present_in_the_source_is_accepted(self):
        found = check(load_invalid("narrowing_not_subset"), inventory=TWIN_INVENTORY)
        assert not [
            d
            for d in found
            if d.code == "unknown_identifier" and "other_command" in d.message
        ]

    def test_a_command_absent_from_the_source_is_rejected(self):
        """§10.1's load-bearing defense against an injected semantic body."""
        inventory = StubInventory(
            {"demo_command", "task.completed", "project_id", "title", "bound"}
        )
        found = check(load_invalid("unknown_identifier"), inventory=inventory)
        diagnostic = next(d for d in found if d.code == "unknown_identifier")
        assert "session_kill" in diagnostic.message
        assert diagnostic.step_id == "act"

    def test_step_and_rule_ids_are_not_required_in_the_source(self):
        found = check(build(), inventory=TWIN_INVENTORY)
        assert "unknown_identifier" not in codes(found)

    def test_a_dotted_path_is_satisfied_by_a_prefix(self):
        def mutate(artifact):
            artifact["steps"]["act"]["inputs"]["note"] = {
                "type": "event_ref",
                "path": "task.branch_name",
            }

        inventory = StubInventory(
            {"demo_command", "task.completed", "project_id", "note", "task", "bound"}
        )
        assert "unknown_identifier" not in codes(check(build(mutate), inventory=inventory))

    def test_without_an_inventory_the_pass_is_skipped(self):
        assert "unknown_identifier" not in codes(check(load_invalid("unknown_identifier")))

    def test_terminal_outcomes_and_context_paths_are_engine_vocabulary(self):
        def mutate(artifact):
            artifact["steps"]["act"]["inputs"]["note"] = {
                "type": "context_ref",
                "path": "run_id",
            }

        inventory = StubInventory(
            {"demo_command", "task.completed", "project_id", "note", "bound"}
        )
        assert "unknown_identifier" not in codes(check(build(mutate), inventory=inventory))


class TestGoldenArtifactGraph:
    """§9.1 — the shared golden artifact, with the stub lookups."""

    @pytest.fixture
    def golden(self):
        return load_definition_json(GOLDEN.read_text())

    def test_it_validates_without_a_single_error(self, golden):
        found = validate_definition(
            golden,
            contracts=RegistryContractLookup(),
            profiles=StubProfiles(),
            events=RegisteredEventLookup(),
        )
        assert errors(found) == []

    def test_the_only_finding_is_the_deferred_delegation(self, golden):
        found = validate_definition(
            golden,
            contracts=RegistryContractLookup(),
            profiles=StubProfiles(),
            events=RegisteredEventLookup(),
        )
        assert [d.code for d in found] == ["delegation_runtime_checked"]

    def test_validation_is_total_and_never_raises(self):
        """Every pass runs; the compiler agent's repair loop needs the whole
        list back in one call."""
        broken = twin()
        broken["steps"]["act"]["command"] = "nope"
        broken["steps"]["act"]["transitions"] = {"done": "ghost"}
        broken["rules"][0]["entry_step"] = "missing"
        found = check(PlaybookDefinition.model_validate(broken))
        assert {"unknown_command", "unknown_step_target", "rule_entry_unknown"} <= codes(found)


class TestGoldenArtifactContracts:
    """§9.1 — the same artifact against the real Package 1 registry."""

    @pytest.fixture
    def golden(self):
        return load_definition_json(GOLDEN.read_text())

    def test_the_recorded_fingerprints_match_the_live_registry(self, golden):
        """A drift here is exactly what ``stale_contract`` means in production:
        rebuild and review the artifact."""
        lookup = RegistryContractLookup()
        for name, recorded in golden.compiled_against.commands.items():
            info = lookup.get(name)
            assert info is not None, f"{name} is no longer a registered contract"
            assert info.execution_fingerprint == recorded, name

    def test_every_command_step_names_a_registered_contract(self, golden):
        lookup = RegistryContractLookup()
        for step in golden.steps.values():
            if step.type == "command":
                assert lookup.get(step.command) is not None, step.command

    def test_the_stub_contract_table_agrees_with_the_registry(self):
        """§9.1 — the stubs may not rot into a parallel truth."""
        real = RegistryContractLookup()
        for name in ("ensure_task", "gate_create", "list_tasks"):
            info = real.get(name)
            assert info is not None
            assert info.outcomes
            assert info.execution_fingerprint.startswith("sha256:")

    def test_transitions_map_the_real_contract_outcomes(self, golden):
        lookup = RegistryContractLookup()
        for step_id, step in golden.steps.items():
            if step.type != "command":
                continue
            info = lookup.get(step.command)
            assert info.outcomes <= set(step.transitions), step_id


def test_pathological_artifact_is_bounded():
    """§10.6 — a 500-step artifact validates in well under two seconds."""
    artifact = twin()
    artifact["steps"]["act"]["transitions"] = {
        "done": "s0",
        "skipped": "s0",
        "runtime_error": "oops",
    }
    total = 480
    for index in range(total):
        target = f"s{index + 1}" if index + 1 < total else "end"
        artifact["steps"][f"s{index}"] = {
            "type": "command",
            "rule": "r1",
            "title": f"Step {index}",
            "command": "demo_command",
            "inputs": {"project_id": {"type": "event_ref", "path": "project_id"}},
            "save_result_as": f"b{index}",
            "transitions": {"done": target, "skipped": target, "runtime_error": "oops"},
            "source": source(index + 10),
        }
    definition = PlaybookDefinition.model_validate(artifact)
    started = time.monotonic()
    found = check(definition)
    assert time.monotonic() - started < 2.0
    assert errors(found) == []


def test_a_deeply_shuffled_artifact_validates_identically():
    """Diagnostics are order-independent: the artifact is a graph, not a list."""
    artifact = twin()
    shuffled = copy.deepcopy(artifact)
    shuffled["steps"] = dict(reversed(list(shuffled["steps"].items())))
    assert codes(check(PlaybookDefinition.model_validate(artifact))) == codes(
        check(PlaybookDefinition.model_validate(shuffled))
    )
