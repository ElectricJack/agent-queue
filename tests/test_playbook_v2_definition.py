"""§4 — the strict Playbook V2 artifact model.

Child plan ``docs/superpowers/plans/2026-09-01-playbook-v2-typed-model-compiler.md``
§4.1's four invariants, §4.5's per-step shapes, §4.6's outcome vocabulary,
§4.7's canonical serialization and §4.8's executable/presentation split.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from src.playbooks import definition as D
from src.playbooks.definition import (
    LLM_RESERVED_OUTCOMES,
    RESERVED_OUTCOMES,
    RUNTIME_ERROR_KEY,
    AgentTypeScope,
    CommandStep,
    DuplicateJsonKey,
    PlaybookDefinition,
    ProjectScope,
    SourceRef,
    SystemScope,
    TerminalStep,
    WaitStep,
    artifact_sha256,
    business_outcomes,
    canonical_bytes,
    contract_fingerprint,
    is_executable_path,
    load_definition_json,
    reserved_outcomes_for,
    scope_from_v1,
    scope_to_v1,
    source_digest,
    truncate_excerpt,
)
from src.playbooks.expressions import V2Base
from tests.playbook_v2_helpers import GOLDEN, GOLDEN_V6, source, twin

#: §4.7 — pinned so a Pydantic upgrade that reorders dumps fails loudly rather
#: than silently invalidating every stored artifact hash.
GOLDEN_DIGEST = "sha256:eaac637309b22ef461f6b0ec4af0607c6baaac204f1c5369c8ef4b39866e6b6d"


@pytest.fixture
def golden() -> PlaybookDefinition:
    return load_definition_json(GOLDEN.read_text())


def _all_v2_models() -> list[type[BaseModel]]:
    seen: dict[str, type[BaseModel]] = {}
    stack: list[type] = [V2Base]
    while stack:
        model = stack.pop()
        for subclass in model.__subclasses__():
            if subclass.__name__ not in seen:
                seen[subclass.__name__] = subclass
                stack.append(subclass)
    return list(seen.values())


class TestBaseInvariants:
    """§4.1 — the four invariants every model in the package must hold."""

    def test_every_v2_model_forbids_extra(self):
        loose = [
            model.__name__
            for model in _all_v2_models()
            if model.model_config.get("extra") != "forbid"
        ]
        assert loose == []

    def test_every_v2_model_is_frozen(self):
        mutable = [
            model.__name__
            for model in _all_v2_models()
            if not model.model_config.get("frozen")
        ]
        assert mutable == []

    def test_absent_and_null_are_the_same_model(self, golden):
        """Invariant 2 — what makes ``exclude_none=True`` lossless.

        The full dump spells every optional field out as ``null``; the canonical
        dump drops them.  Both must re-validate to the same model, or a stored
        artifact's hash would depend on how its author spelled "absent".
        """
        spelled_out = golden.model_dump(mode="json", exclude_none=False)
        dropped = golden.model_dump(mode="json", exclude_none=True)
        assert json.dumps(spelled_out) != json.dumps(dropped)
        assert PlaybookDefinition.model_validate(spelled_out) == golden
        assert PlaybookDefinition.model_validate(dropped) == golden

    def test_no_optional_field_distinguishes_absent_from_null(self):
        """The same invariant, stated over the models rather than one fixture."""
        offenders = [
            f"{model.__name__}.{name}"
            for model in _all_v2_models()
            for name, field in model.model_fields.items()
            if not field.is_required()
            and field.default is not None
            and field.default_factory is None
            and type(None) in getattr(field.annotation, "__args__", ())
        ]
        assert offenders == []

    def test_round_trip_is_identity(self, golden):
        assert PlaybookDefinition.model_validate(json.loads(canonical_bytes(golden))) == golden

    @pytest.mark.parametrize("artifact", [GOLDEN, GOLDEN_V6])
    def test_every_checked_in_artifact_round_trips(self, artifact):
        loaded = load_definition_json(artifact.read_text())
        assert PlaybookDefinition.model_validate(json.loads(canonical_bytes(loaded))) == loaded

    @pytest.mark.parametrize("bad", ["Upper", "-lead", "has space", "a" * 65, ""])
    def test_identifier_syntax_is_enforced(self, bad):
        artifact = twin()
        artifact["rules"][0]["id"] = bad
        with pytest.raises(ValidationError):
            PlaybookDefinition.model_validate(artifact)

    def test_an_unknown_key_is_a_compile_error(self):
        artifact = twin()
        artifact["steps"]["act"]["exec"] = "rm -rf /"
        with pytest.raises(ValidationError, match="exec"):
            PlaybookDefinition.model_validate(artifact)


class TestCanonicalSerialization:
    """§4.7 — the four fingerprints."""

    def test_canonical_bytes_are_key_order_independent(self, golden):
        shuffled = json.loads(GOLDEN.read_text())
        shuffled["steps"] = dict(reversed(list(shuffled["steps"].items())))
        shuffled["compiled_against"] = dict(
            reversed(list(shuffled["compiled_against"].items()))
        )
        assert canonical_bytes(PlaybookDefinition.model_validate(shuffled)) == canonical_bytes(
            golden
        )

    def test_canonical_bytes_are_stable_across_processes(self, golden):
        assert artifact_sha256(golden) == GOLDEN_DIGEST

    def test_canonical_bytes_omit_nulls(self, golden):
        assert b"null" not in canonical_bytes(golden)

    def test_contract_fingerprint_covers_commands_only(self, golden):
        widened = golden.model_copy(
            update={
                "compiled_against": golden.compiled_against.model_copy(
                    update={"profiles": {"reviewer": "sha256:" + "ab" * 32}}
                )
            }
        )
        assert contract_fingerprint(widened) == contract_fingerprint(golden)
        assert golden.contract_fingerprint() == contract_fingerprint(golden)

    def test_source_digest_uses_stable_normalization(self):
        markdown = "# Title\r\n\r\n  Body \n"
        assert D.normalize_source(markdown) == "\n---\n# Title\n\n  Body"
        assert source_digest(markdown).startswith("sha256:")
        assert len(source_digest(markdown)) == len("sha256:") + 64

    def test_a_version_bump_changes_the_artifact_hash(self, golden):
        v6 = load_definition_json(GOLDEN_V6.read_text())
        assert artifact_sha256(v6) != artifact_sha256(golden)


class TestExecutableVsPresentation:
    """§4.8 — the split is declared in the model, not in a hard-coded list."""

    def test_presentation_fields_are_declared_not_hardcoded(self):
        assert ("StepBase", "title") not in D.PRESENTATION_FIELDS
        assert ("CommandStep", "title") in D.PRESENTATION_FIELDS
        for model_name, field in D.PRESENTATION_FIELDS:
            model = next(m for m in D._reachable_models() if m.__name__ == model_name)
            extra = model.model_fields[field].json_schema_extra
            assert extra == {"executable": False}

    @pytest.mark.parametrize(
        "pointer",
        [
            "/steps/act/title",
            "/steps/act/description",
            "/rules/0/name",
            "/rules/0/description",
            "/rules/0/source/start_line",
            "/steps/check/cases/0/label",
        ],
    )
    def test_presentation_pointers(self, pointer):
        assert is_executable_path(pointer) is False

    @pytest.mark.parametrize(
        "pointer",
        [
            "/steps/act/command",
            "/steps/act/transitions",
            "/rules/0/trigger/event_type",
            "/rules/0/entry_step",
            "/steps/check/cases/0/goto",
            "/version",
        ],
    )
    def test_executable_pointers(self, pointer):
        assert is_executable_path(pointer) is True

    def test_an_unresolvable_pointer_is_treated_as_executable(self):
        assert is_executable_path("/steps/act/who_knows") is True

    def test_executable_fields_excludes_the_presentation_ones(self):
        assert "CommandStep.command" in D.EXECUTABLE_FIELDS
        assert "CommandStep.title" not in D.EXECUTABLE_FIELDS
        assert "SourceRef.path" not in D.EXECUTABLE_FIELDS


class TestSourceRefs:
    """§4.4 / §10.5."""

    def test_every_step_carries_a_resolvable_source_ref(self, golden):
        for step_id, step in golden.steps.items():
            assert isinstance(step.source, SourceRef), step_id
            assert step.source.path
            assert step.source.end_line >= step.source.start_line

    def test_every_rule_carries_a_source_ref(self, golden):
        assert all(rule.source.path for rule in golden.rules)

    def test_end_line_may_not_precede_start_line(self):
        with pytest.raises(ValidationError):
            SourceRef(path="p.md", start_line=9, end_line=2)

    def test_an_over_long_excerpt_is_rejected(self):
        with pytest.raises(ValidationError):
            SourceRef(path="p.md", start_line=1, end_line=1, excerpt="x" * 401)

    def test_truncate_excerpt_cuts_on_a_character_boundary(self):
        short, cut = truncate_excerpt("hello")
        assert (short, cut) == ("hello", False)
        long, cut = truncate_excerpt("x" * 500)
        assert cut is True
        assert len(long) == D.MAX_EXCERPT_CHARS
        assert long.endswith("…")
        SourceRef(path="p.md", start_line=1, end_line=1, excerpt=long)


class TestScopeBridge:
    """§4.4 — V1's string scope round-trips through the object form."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("system", SystemScope()),
            ("", SystemScope()),
            ("project:alpha", ProjectScope(project_id="alpha")),
            ("agent-type:supervisor", AgentTypeScope(agent_type="supervisor")),
        ],
    )
    def test_scope_from_v1(self, raw, expected):
        assert scope_from_v1(raw) == expected

    @pytest.mark.parametrize(
        "scope,raw",
        [
            (SystemScope(), "system"),
            (ProjectScope(project_id="alpha"), "project:alpha"),
            (AgentTypeScope(agent_type="supervisor"), "agent-type:supervisor"),
        ],
    )
    def test_scope_to_v1(self, scope, raw):
        assert scope_to_v1(scope) == raw
        assert scope_from_v1(raw) == scope

    @pytest.mark.parametrize("raw", ["project", "agent-type:", "nonsense"])
    def test_an_unrecognised_v1_scope_is_rejected(self, raw):
        with pytest.raises(ValueError):
            scope_from_v1(raw)


class TestWaitStepShapes:
    """§4.5's per-kind table, one test per row."""

    def _wait(self, **overrides):
        base = {
            "rule": "r1",
            "title": "Wait",
            "source": source(),
            "transitions": {"runtime_error": "oops"},
        }
        base.update(overrides)
        return WaitStep(**base)

    def test_event_requires_awaited_and_correlation_key(self):
        self._wait(
            wait_kind="event",
            awaited={"type": "literal", "value": "task.completed"},
            correlation_key={"type": "event_ref", "path": "task_id"},
        )
        with pytest.raises(ValidationError, match="correlation_key"):
            self._wait(wait_kind="event", awaited={"type": "literal", "value": "x"})

    def test_human_requires_outcomes(self):
        self._wait(
            wait_kind="human",
            awaited={"type": "literal", "value": "Approve"},
            correlation_key={"type": "event_ref", "path": "task_id"},
            outcomes=["approve"],
        )
        with pytest.raises(ValidationError, match="outcomes"):
            self._wait(
                wait_kind="human",
                awaited={"type": "literal", "value": "Approve"},
                correlation_key={"type": "event_ref", "path": "task_id"},
            )

    def test_task_requires_awaited_and_correlation_key(self):
        self._wait(
            wait_kind="task",
            awaited={"type": "event_ref", "path": "task_id"},
            correlation_key={"type": "event_ref", "path": "task_id"},
        )
        with pytest.raises(ValidationError):
            self._wait(wait_kind="task")

    def test_timer_requires_a_timeout_and_takes_nothing_else(self):
        self._wait(wait_kind="timer", timeout_seconds=30)
        with pytest.raises(ValidationError, match="timeout_seconds"):
            self._wait(wait_kind="timer")
        with pytest.raises(ValidationError, match="timer wait"):
            self._wait(
                wait_kind="timer",
                timeout_seconds=30,
                awaited={"type": "literal", "value": "x"},
            )

    def test_only_a_human_wait_declares_outcomes(self):
        with pytest.raises(ValidationError, match="human wait"):
            self._wait(wait_kind="timer", timeout_seconds=5, outcomes=["fired"])

    def test_the_four_result_schemas_are_exported(self):
        assert set(D.WAIT_RESULT_SCHEMAS) == {"event", "human", "task", "timer"}
        assert D.WAIT_RESULT_SCHEMAS["human"]["required"] == ["resolution"]
        assert D.FOREACH_RESULT_SCHEMA["required"] == ["total", "succeeded", "failed", "items"]
        assert D.AGENT_TASK_RESULT_SCHEMA["required"] == ["task_id", "status"]


class TestOutcomeVocabulary:
    """§4.6."""

    def test_runtime_error_is_a_transition_key_not_an_outcome(self):
        assert RUNTIME_ERROR_KEY not in RESERVED_OUTCOMES
        assert RUNTIME_ERROR_KEY not in LLM_RESERVED_OUTCOMES

    def test_llm_steps_carry_the_extra_reserved_outcomes(self, golden):
        assert reserved_outcomes_for(golden.steps["classify-risk"]) == (
            RESERVED_OUTCOMES | LLM_RESERVED_OUTCOMES
        )
        assert reserved_outcomes_for(golden.steps["ensure-review-task"]) == RESERVED_OUTCOMES

    def test_business_outcomes_per_step_kind(self, golden):
        assert business_outcomes(
            golden.steps["ensure-review-task"], contract_outcomes=frozenset({"created"})
        ) == frozenset({"created"})
        assert business_outcomes(golden.steps["ensure-review-task"]) == frozenset()
        assert business_outcomes(golden.steps["classify-risk"]) == frozenset({"low", "high"})
        assert business_outcomes(golden.steps["escalate"]) == frozenset({"completed", "failed"})
        assert business_outcomes(golden.steps["await-approval"]) == frozenset(
            {"approve", "revise"}
        )
        assert business_outcomes(golden.steps["for-each-task"]) == frozenset(
            {"completed", "failed"}
        )
        assert business_outcomes(golden.steps["check-gate"]) == frozenset()
        assert business_outcomes(golden.steps["done"]) == frozenset()

    def test_a_dispatched_agent_task_has_one_business_outcome(self, golden):
        fire_and_forget = golden.steps["escalate"].model_copy(
            update={"wait_for_completion": False}
        )
        assert business_outcomes(fire_and_forget) == frozenset({"dispatched"})


class TestStepStructure:
    """§4.5 — shapes the model rejects outright."""

    def test_a_decision_step_must_declare_a_default(self):
        artifact = twin()
        artifact["steps"]["act"] = {
            "type": "decision",
            "rule": "r1",
            "title": "Choose",
            "cases": [
                {
                    "when": {"type": "exists", "value": {"type": "event_ref", "path": "title"}},
                    "goto": "end",
                }
            ],
            "source": source(2),
        }
        with pytest.raises(ValidationError, match="default"):
            PlaybookDefinition.model_validate(artifact)

    def test_a_decision_step_needs_at_least_one_case(self):
        artifact = twin()
        artifact["steps"]["act"] = {
            "type": "decision",
            "rule": "r1",
            "title": "Choose",
            "cases": [],
            "default": "end",
            "source": source(2),
        }
        with pytest.raises(ValidationError):
            PlaybookDefinition.model_validate(artifact)

    def test_a_terminal_outcome_is_a_closed_enum(self):
        with pytest.raises(ValidationError):
            TerminalStep(rule="r1", title="T", outcome="whatever", source=source())

    def test_step_targets_lists_every_outgoing_edge(self, golden):
        assert set(D.step_targets(golden.steps["check-gate"])) == {
            "/cases/0/goto",
            "/default",
        }
        loop = D.step_targets(golden.steps["for-each-task"])
        assert loop["/body_entry"] == "open-gate"
        assert loop["/continuation"] == "sweep-done"
        assert D.step_targets(golden.steps["done"]) == {}

    def test_an_ai_budget_is_entirely_required(self):
        with pytest.raises(ValidationError):
            D.AiBudget(max_calls=1, max_output_tokens=10, max_total_tokens=10)


class TestArtifactBounds:
    """§10.6 — the artifact is bounded by construction."""

    def test_rules_and_steps_are_capped(self):
        artifact = twin()
        artifact["rules"] = [dict(artifact["rules"][0], id=f"r{i}") for i in range(D.MAX_RULES + 1)]
        with pytest.raises(ValidationError):
            PlaybookDefinition.model_validate(artifact)

    def test_expression_depth_is_capped_at_load(self):
        artifact = twin()
        nested = {"type": "literal", "value": "leaf"}
        for _ in range(12):
            nested = {"type": "list", "items": [nested]}
        artifact["steps"]["act"]["inputs"]["note"] = nested
        with pytest.raises(ValidationError, match="depth"):
            PlaybookDefinition.model_validate(artifact)

    def test_compiled_at_must_be_timezone_aware(self):
        artifact = twin()
        artifact["compiled_at"] = "2026-09-01T00:00:00"
        with pytest.raises(ValidationError, match="timezone-aware"):
            PlaybookDefinition.model_validate(artifact)

    def test_a_source_hash_must_be_a_full_prefixed_digest(self):
        artifact = twin()
        artifact["source_hash"] = "abc123"
        with pytest.raises(ValidationError):
            PlaybookDefinition.model_validate(artifact)


class TestStrictJsonLoading:
    """§7.1's parse step — duplicate object keys are rejected before anything else."""

    def test_duplicate_step_ids_are_rejected(self):
        text = json.dumps(twin())
        duplicated = text.replace(
            '"end": {', '"end": {"type": "terminal", "rule": "r1", "title": "x", '
            '"outcome": "completed", "source": {"path": "p", "start_line": 1, '
            '"end_line": 1}}, "end": {', 1,
        )
        with pytest.raises(DuplicateJsonKey) as caught:
            load_definition_json(duplicated)
        assert caught.value.code == "duplicate_step_id"

    def test_a_duplicate_top_level_key_cannot_smuggle_an_identity(self):
        text = json.dumps(twin()).replace('"id": "twin"', '"id": "twin", "id": "system-pipeline"', 1)
        with pytest.raises(DuplicateJsonKey):
            load_definition_json(text)

    def test_clean_text_loads(self):
        assert load_definition_json(json.dumps(twin())).id == "twin"


def test_the_golden_artifact_is_the_shared_fixture():
    """§9.1 — Package 5 and Package 2 read the same bytes."""
    golden = load_definition_json(GOLDEN.read_text())
    assert golden.schema_version == 2
    assert {step.type for step in golden.steps.values()} == {
        "command",
        "llm",
        "agent_task",
        "decision",
        "wait",
        "foreach",
        "terminal",
    }
    assert isinstance(golden.steps["ensure-review-task"], CommandStep)
    assert golden.compiled_at == datetime(2026, 9, 1, tzinfo=UTC)


# --------------------------------------------------------------------------
# §8 — generated JSON Schema (``scripts/generate-playbook-schema.py``)
# --------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
GENERATOR = REPO_ROOT / "scripts" / "generate-playbook-schema.py"
SCHEMA_FILE = REPO_ROOT / "src" / "playbook_v2_schema.json"
V1_SCHEMA_FILE = REPO_ROOT / "src" / "playbook_schema.json"


def _run_generator(*args: str) -> subprocess.CompletedProcess[str]:
    """Run the generator with no daemon, no database and no config (§8)."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("AQ_") and k not in {"DATABASE_URL", "POSTGRES_TEST_DSN"}
    }
    env["PYTHONPATH"] = str(REPO_ROOT)
    return subprocess.run(
        [sys.executable, str(GENERATOR), *args],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


class TestGeneratedSchema:
    """§8 — the published schema and the accepting loader are one source."""

    def test_generated_schema_matches_checked_in_file(self):
        """T-15 — the CI guard, mirroring V1's ``test_schema_file_matches_generated``."""
        result = _run_generator("--check")
        assert result.returncode == 0, result.stdout + result.stderr

    def test_generation_is_idempotent(self, tmp_path):
        """T-16 — generate twice, byte-identical (the roadmap's acceptance test)."""
        first = tmp_path / "first.json"
        second = tmp_path / "second.json"
        assert _run_generator("--output", str(first)).returncode == 0
        assert _run_generator("--output", str(second)).returncode == 0
        assert first.read_bytes() == second.read_bytes()
        assert first.read_bytes() == SCHEMA_FILE.read_bytes()

    def test_check_reports_drift(self, tmp_path):
        stale = tmp_path / "stale.json"
        stale.write_text('{"title": "not the schema"}\n')
        result = _run_generator("--check", "--output", str(stale))
        assert result.returncode == 1
        assert "playbook" in result.stdout.lower() or "---" in result.stdout

    def test_schema_is_deterministically_serialized(self):
        text = SCHEMA_FILE.read_text()
        parsed = json.loads(text)
        assert text == json.dumps(parsed, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
        assert parsed["$defs"]
        assert "PlaybookDefinition" in text

    def test_the_v1_schema_file_is_untouched(self):
        """T-17 — Package 7, not this package, retires V1's schema file."""
        v1 = json.loads(V1_SCHEMA_FILE.read_text())
        assert v1 != json.loads(SCHEMA_FILE.read_text())

    def test_output_schema_validation_has_its_validator(self):
        """T-15b — ``jsonschema`` is a declared dependency, not a transitive accident."""
        from jsonschema import Draft202012Validator

        Draft202012Validator.check_schema(json.loads(SCHEMA_FILE.read_text()))

    def test_every_fixture_validates_against_the_published_schema(self):
        from jsonschema import Draft202012Validator

        validator = Draft202012Validator(json.loads(SCHEMA_FILE.read_text()))
        fixtures = sorted(GOLDEN.parent.glob("*.artifact.json"))
        assert fixtures
        for path in fixtures:
            errors = sorted(validator.iter_errors(json.loads(path.read_text())), key=str)
            assert not errors, f"{path.name}: {[e.message for e in errors]}"
