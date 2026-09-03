"""Shared scaffolding for the Playbook V2 model, expression and validation suites.

Child plan ``docs/superpowers/plans/2026-09-01-playbook-v2-typed-model-compiler.md``
§9.  Two things live here:

* the stub ``ContractLookup`` / ``ProfileLookup`` / ``EventSchemaLookup`` /
  ``IdentifierInventory`` implementations §3.3 calls for — they are the same
  protocols the production adapters implement, populated from a small literal
  table so a suite can build a capability lattice without touching the vault;
* the valid twin and the one-defect mutations behind
  ``tests/fixtures/playbooks/v2/invalid/<code>.json`` (§9.3), kept next to the
  suite that reads them so a fixture and its intent cannot drift apart.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from src.playbooks.validation import (
    ArgumentSpec,
    ContractInfo,
    Diagnostic,
    ValueType,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "playbooks" / "v2"
INVALID_DIR = FIXTURE_DIR / "invalid"
GOLDEN = FIXTURE_DIR / "review-pipeline.artifact.json"
GOLDEN_V6 = FIXTURE_DIR / "review-pipeline.v6.artifact.json"

STRING = ValueType("string")
INTEGER = ValueType("integer")
ARRAY_OF_OBJECTS = ValueType("array", item_type=ValueType("object"))

#: A deterministic stand-in for a real ``execution_fingerprint``.
DEMO_FINGERPRINT = "sha256:" + "11" * 32
OTHER_FINGERPRINT = "sha256:" + "22" * 32


def _contract(
    name: str,
    *,
    arguments: dict[str, tuple[ValueType, bool]],
    result: dict[str, Any],
    outcomes: set[str],
    fingerprint: str = DEMO_FINGERPRINT,
) -> ContractInfo:
    return ContractInfo(
        name=name,
        arguments={
            key: ArgumentSpec(name=key, type=value[0], required=value[1])
            for key, value in arguments.items()
        },
        result_schema=result,
        outcomes=frozenset(outcomes),
        execution_fingerprint=fingerprint,
    )


STUB_CONTRACTS: dict[str, ContractInfo] = {
    "demo_command": _contract(
        "demo_command",
        arguments={"project_id": (STRING, True), "note": (STRING, False)},
        result={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "count": {"type": "integer"},
                "tasks": {"type": "array", "items": {"type": "object"}},
            },
        },
        outcomes={"done", "skipped"},
    ),
    "other_command": _contract(
        "other_command",
        arguments={"project_id": (STRING, True)},
        result={"type": "object", "properties": {"ok": {"type": "boolean"}}},
        outcomes={"done"},
        fingerprint=OTHER_FINGERPRINT,
    ),
    "session_kill": _contract(
        "session_kill",
        arguments={"project_id": (STRING, True)},
        result={"type": "object", "properties": {"killed": {"type": "boolean"}}},
        outcomes={"done"},
        fingerprint=OTHER_FINGERPRINT,
    ),
}


class StubContracts:
    """§3.3's ``ContractLookup`` seam, backed by :data:`STUB_CONTRACTS`."""

    def __init__(self, table: dict[str, ContractInfo] | None = None) -> None:
        self._table = STUB_CONTRACTS if table is None else table

    def get(self, name: str) -> ContractInfo | None:
        return self._table.get(name)


def _policy(**namespaces: frozenset[str]) -> Any:
    from src.profiles.capabilities import CapabilityPolicy

    return CapabilityPolicy.from_namespaces(**namespaces)


def stub_policies() -> dict[str, Any]:
    """A two-profile capability lattice: ``wide`` strictly contains ``worker``."""
    return {
        "worker": _policy(aq_commands=frozenset({"demo_command"})),
        "wide": _policy(aq_commands=frozenset({"demo_command", "other_command"})),
        "hollow": _policy(),
        "reviewer": _policy(aq_commands=frozenset({"demo_command"})),
    }


def stub_routing() -> dict[str, Any]:
    """Resolved provider/model policy per stub profile, for the AI cards."""
    from src.profiles.intelligence import ProfileIntelligence

    return {
        "worker": ProfileIntelligence("standard-medium", "anthropic", "claude-sonnet-5"),
        "wide": ProfileIntelligence("deep-high", "anthropic", "claude-opus-5"),
        # A profile with no class still names its provider.
        "hollow": ProfileIntelligence(None, "anthropic", None),
        "reviewer": ProfileIntelligence("deep-high", "anthropic", "claude-opus-5"),
    }


class StubProfiles:
    """§3.3's ``ProfileLookup`` seam."""

    def __init__(
        self,
        table: dict[str, Any] | None = None,
        *,
        routing: dict[str, Any] | None = None,
    ) -> None:
        self._table = stub_policies() if table is None else table
        self._routing = stub_routing() if routing is None else routing

    def policy(self, profile_id: str) -> Any | None:
        return self._table.get(profile_id)

    def routing(self, profile_id: str) -> Any | None:
        return self._routing.get(profile_id)


STUB_EVENTS: dict[str, dict[str, Any]] = {
    "task.completed": {
        "required": ["task_id", "project_id", "title"],
        "optional": ["agent_id", "note"],
        "fields": {
            "task_id": {"type": "string"},
            "project_id": {"type": "string"},
            "title": {"type": "string"},
            "agent_id": {"type": "string"},
            "note": {"type": "string"},
            "task": {
                "type": "object",
                "fields": {"branch_name": {"type": "string"}, "pr_url": {"type": "string"}},
            },
        },
    },
    "spec.approved": {
        "required": ["project_id", "spec_path"],
        "optional": [],
        "fields": {"project_id": {"type": "string"}, "spec_path": {"type": "string"}},
    },
}


class StubEvents:
    """§3.3's ``EventSchemaLookup`` seam."""

    def __init__(self, table: dict[str, dict[str, Any]] | None = None) -> None:
        self._table = STUB_EVENTS if table is None else table

    def get(self, event_type: str) -> dict[str, Any] | None:
        return self._table.get(event_type)


class StubInventory:
    """§5.2's ``IdentifierInventory``, as a flat name set."""

    def __init__(self, names: set[str]) -> None:
        self._names = set(names)

    @property
    def names(self) -> set[str]:
        """A copy, so a test can derive a narrower inventory from a wider one."""
        return set(self._names)

    def contains(self, name: str) -> bool:
        return name in self._names

    def refs(self, name: str) -> tuple[Any, ...]:
        return ()


def codes(diagnostics: list[Diagnostic]) -> set[str]:
    return {diagnostic.code for diagnostic in diagnostics}


def errors(diagnostics: list[Diagnostic]) -> list[Diagnostic]:
    return [diagnostic for diagnostic in diagnostics if diagnostic.severity == "error"]


def check(definition: Any, **overrides: Any) -> list[Diagnostic]:
    """``validate_definition`` with the stub lookups wired in by default."""
    from src.playbooks.validation import validate_definition

    kwargs: dict[str, Any] = {
        "contracts": StubContracts(),
        "profiles": StubProfiles(),
        "events": StubEvents(),
    }
    kwargs.update(overrides)
    return validate_definition(definition, **kwargs)


# --------------------------------------------------------------------------
# §9.3 — the valid twin and one mutation per diagnostic code
# --------------------------------------------------------------------------


def source(line: int = 1) -> dict[str, Any]:
    return {"path": "system/playbooks/twin.md", "start_line": line, "end_line": line}


def twin() -> dict[str, Any]:
    """The valid two-command twin every ``invalid/`` fixture is a delta from."""
    return {
        "schema_version": 2,
        "id": "twin",
        "version": 1,
        "scope": {"type": "system"},
        "source_hash": "sha256:" + "0f" * 32,
        "compiled_at": "2026-09-01T00:00:00Z",
        "compiled_against": {"commands": {"demo_command": DEMO_FINGERPRINT}},
        "rules": [
            {
                "id": "r1",
                "name": "Rule one",
                "trigger": {"event_type": "task.completed"},
                "entry_step": "act",
                "source": source(1),
            }
        ],
        "steps": {
            "act": {
                "type": "command",
                "rule": "r1",
                "title": "Act",
                "command": "demo_command",
                "inputs": {"project_id": {"type": "event_ref", "path": "project_id"}},
                "save_result_as": "bound",
                "transitions": {"done": "end", "skipped": "end", "runtime_error": "oops"},
                "source": source(2),
            },
            "end": {
                "type": "terminal",
                "rule": "r1",
                "title": "Done",
                "outcome": "completed",
                "source": source(3),
            },
            "oops": {
                "type": "terminal",
                "rule": "r1",
                "title": "Failed",
                "outcome": "failed",
                "source": source(4),
            },
        },
    }


def _llm_step(**overrides: Any) -> dict[str, Any]:
    step = {
        "type": "llm",
        "rule": "r1",
        "title": "Classify",
        "profile_id": "worker",
        "prompt": {"type": "literal", "value": "classify"},
        "output_schema": {
            "type": "object",
            "properties": {"risk": {"enum": ["low", "high"]}},
            "required": ["risk"],
        },
        "outcome_field": "risk",
        "budget": {
            "max_calls": 1,
            "max_output_tokens": 256,
            "max_total_tokens": 1024,
            "timeout_seconds": 60,
        },
        "transitions": {"low": "end", "high": "end", "runtime_error": "oops"},
        "source": source(5),
    }
    step.update(overrides)
    return step


def _foreach_twin() -> dict[str, Any]:
    """A twin whose ``act`` step feeds a loop, for the §6.3 mutations."""
    artifact = twin()
    artifact["steps"]["act"]["transitions"] = {
        "done": "loop",
        "skipped": "loop",
        "runtime_error": "oops",
    }
    artifact["steps"]["loop"] = {
        "type": "foreach",
        "rule": "r1",
        "title": "Each task",
        "collection": {"type": "binding_ref", "binding": "bound", "path": "tasks"},
        "item_binding": "item",
        "failure_policy": "collect",
        "body_entry": "body",
        "continuation": "end",
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
            "note": {"type": "loop_ref", "binding": "item", "path": "title"},
        },
        "transitions": {"done": "loop", "skipped": "loop", "runtime_error": "oops"},
        "source": source(7),
    }
    return artifact


def _agent_task_twin() -> dict[str, Any]:
    """A twin whose tool-using ``llm`` step precedes an ``agent_task`` (§6.7)."""
    artifact = twin()
    artifact["steps"]["act"]["transitions"] = {
        "done": "classify",
        "skipped": "classify",
        "runtime_error": "oops",
    }
    artifact["steps"]["classify"] = _llm_step(
        tool_use={"enabled": True, "aq_commands": ["demo_command"]},
        transitions={"low": "delegate", "high": "delegate", "runtime_error": "oops"},
    )
    artifact["steps"]["delegate"] = {
        "type": "agent_task",
        "rule": "r1",
        "title": "Delegate",
        "profile_id": "worker",
        "objective": {"type": "literal", "value": "do the thing"},
        "transitions": {"completed": "end", "failed": "oops", "runtime_error": "oops"},
        "source": source(8),
    }
    return artifact


def _mutate(base: dict[str, Any], mutation: Any) -> dict[str, Any]:
    artifact = copy.deepcopy(base)
    mutation(artifact)
    return artifact


def _set_llm(artifact: dict[str, Any], **overrides: Any) -> None:
    artifact["steps"]["act"]["transitions"] = {
        "done": "classify",
        "skipped": "classify",
        "runtime_error": "oops",
    }
    artifact["steps"]["classify"] = _llm_step(**overrides)


def _deep_schema(depth: int) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "string"}
    for _ in range(depth):
        schema = {"type": "object", "properties": {"nested": schema}}
    return schema


def _invalid_artifacts() -> dict[str, dict[str, Any]]:
    """One artifact per validator diagnostic code, each a single-defect delta."""

    def rule(artifact: dict[str, Any]) -> dict[str, Any]:
        return artifact["rules"][0]

    table: dict[str, dict[str, Any]] = {
        "unknown_identifier": _mutate(
            twin(), lambda a: a["steps"]["act"].update(command="session_kill")
        ),
        "unknown_command": _mutate(
            twin(), lambda a: a["steps"]["act"].update(command="no_such_command")
        ),
        "unknown_profile": _mutate(twin(), lambda a: _set_llm(a, profile_id="ghost")),
        "unknown_event": _mutate(
            twin(), lambda a: rule(a)["trigger"].update(event_type="no.such.event")
        ),
        "unknown_event_field": _mutate(
            twin(),
            lambda a: a["steps"]["act"]["inputs"].update(
                note={"type": "event_ref", "path": "not_a_field"}
            ),
        ),
        "unknown_context_path": _mutate(
            twin(),
            lambda a: a["steps"]["act"]["inputs"].update(
                note={"type": "context_ref", "path": "wat"}
            ),
        ),
        "duplicate_rule_id": _mutate(
            twin(), lambda a: a["rules"].append(copy.deepcopy(a["rules"][0]))
        ),
        "duplicate_binding": _mutate(
            twin(),
            lambda a: (
                a["steps"]["act"]["transitions"].update(done="again", skipped="again"),
                a["steps"].update(
                    again={
                        "type": "command",
                        "rule": "r1",
                        "title": "Again",
                        "command": "demo_command",
                        "inputs": {"project_id": {"type": "event_ref", "path": "project_id"}},
                        "save_result_as": "bound",
                        "transitions": {
                            "done": "end",
                            "skipped": "end",
                            "runtime_error": "oops",
                        },
                        "source": source(9),
                    }
                ),
            ),
        ),
        "step_rule_unknown": _mutate(
            twin(),
            lambda a: (
                a["steps"]["act"]["transitions"].update(done="stray"),
                a["steps"].update(
                    stray={
                        "type": "terminal",
                        "rule": "ghost-rule",
                        "title": "Stray",
                        "outcome": "completed",
                        "source": source(9),
                    }
                ),
            ),
        ),
        "orphan_step": _mutate(
            twin(),
            lambda a: a["steps"].update(
                stray={
                    "type": "terminal",
                    "rule": "ghost-rule",
                    "title": "Stray",
                    "outcome": "completed",
                    "source": source(9),
                }
            ),
        ),
        "rule_entry_unknown": _mutate(twin(), lambda a: rule(a).update(entry_step="nope")),
        "rule_entry_not_owned": _mutate(
            twin(),
            lambda a: (
                a["rules"].append(
                    {
                        "id": "r2",
                        "name": "Rule two",
                        "trigger": {"event_type": "spec.approved"},
                        "entry_step": "act",
                        "source": source(9),
                    }
                ),
            ),
        ),
        "unknown_step_target": _mutate(
            twin(), lambda a: a["steps"]["act"]["transitions"].update(done="nope")
        ),
        "cross_rule_transition": _mutate(
            twin(),
            lambda a: (
                a["rules"].append(
                    {
                        "id": "r2",
                        "name": "Rule two",
                        "trigger": {"event_type": "spec.approved"},
                        "entry_step": "other-end",
                        "source": source(9),
                    }
                ),
                a["steps"].update(
                    {
                        "other-end": {
                            "type": "terminal",
                            "rule": "r2",
                            "title": "Other",
                            "outcome": "completed",
                            "source": source(10),
                        }
                    }
                ),
                a["steps"]["act"]["transitions"].update(done="other-end"),
            ),
        ),
        "unreachable_step": _mutate(
            twin(),
            lambda a: a["steps"].update(
                lonely={
                    "type": "terminal",
                    "rule": "r1",
                    "title": "Lonely",
                    "outcome": "completed",
                    "source": source(9),
                }
            ),
        ),
        "no_terminal_path": _mutate(
            twin(),
            lambda a: (
                a["steps"]["act"]["transitions"].update(done="spin", skipped="spin"),
                a["steps"].update(
                    spin={
                        "type": "command",
                        "rule": "r1",
                        "title": "Spin",
                        "command": "demo_command",
                        "inputs": {"project_id": {"type": "event_ref", "path": "project_id"}},
                        "transitions": {"done": "spin", "skipped": "spin"},
                        "source": source(9),
                    }
                ),
            ),
        ),
        "nested_loop": _mutate(
            _foreach_twin(),
            lambda a: (
                a["steps"]["loop"].update(body_entry="inner"),
                a["steps"].update(
                    inner={
                        "type": "foreach",
                        "rule": "r1",
                        "title": "Inner",
                        "collection": {"type": "binding_ref", "binding": "bound", "path": "tasks"},
                        "item_binding": "leaf",
                        "failure_policy": "halt",
                        "body_entry": "body",
                        "transitions": {"completed": "loop", "failed": "loop"},
                        "source": source(9),
                    }
                ),
                a["steps"]["body"].update(transitions={"done": "inner", "skipped": "inner"}),
            ),
        ),
        "loop_body_escapes": _mutate(
            # A body step ending the run at a terminal the loop never declares
            # as one of its own exits.
            _foreach_twin(),
            lambda a: (
                a["steps"]["body"]["transitions"].update(done="escaped"),
                a["steps"].update(
                    escaped={
                        "type": "terminal",
                        "rule": "r1",
                        "title": "Escaped",
                        "outcome": "completed",
                        "source": source(9),
                    }
                ),
            ),
        ),
        "continuation_mismatch": _mutate(
            _foreach_twin(), lambda a: a["steps"]["loop"].update(continuation="oops")
        ),
        "loop_variable_shadow": _mutate(
            _foreach_twin(),
            lambda a: (
                a["steps"]["loop"].update(item_binding="bound"),
                a["steps"]["body"]["inputs"].update(
                    note={"type": "loop_ref", "binding": "bound", "path": "title"}
                ),
            ),
        ),
        "loop_ref_outside_loop": _mutate(
            _foreach_twin(),
            lambda a: a["steps"]["act"]["inputs"].update(
                note={"type": "loop_ref", "binding": "item", "path": "title"}
            ),
        ),
        "binding_not_definitely_assigned": _mutate(
            twin(),
            lambda a: (
                a["steps"]["act"].pop("save_result_as"),
                a["steps"]["act"]["transitions"].update(done="maybe", skipped="read"),
                a["steps"].update(
                    maybe={
                        "type": "command",
                        "rule": "r1",
                        "title": "Maybe",
                        "command": "demo_command",
                        "inputs": {"project_id": {"type": "event_ref", "path": "project_id"}},
                        "save_result_as": "bound",
                        "transitions": {"done": "read", "skipped": "read", "runtime_error": "oops"},
                        "source": source(9),
                    },
                    read={
                        "type": "command",
                        "rule": "r1",
                        "title": "Read",
                        "command": "demo_command",
                        "inputs": {
                            "project_id": {"type": "event_ref", "path": "project_id"},
                            "note": {"type": "binding_ref", "binding": "bound", "path": "task_id"},
                        },
                        "transitions": {"done": "end", "skipped": "end", "runtime_error": "oops"},
                        "source": source(10),
                    },
                ),
            ),
        ),
        "binding_reassigned": _mutate(
            _foreach_twin(), lambda a: a["steps"]["body"].update(save_result_as="item")
        ),
        "type_mismatch": _mutate(
            twin(),
            lambda a: a["steps"]["act"]["inputs"].update(
                project_id={"type": "literal", "value": 5}
            ),
        ),
        "type_unknown": _mutate(
            _foreach_twin(),
            lambda a: None,  # `note` reads an untyped loop item — the check is silenced
        ),
        "coalesce_not_total": _mutate(
            twin(),
            lambda a: a["steps"]["act"]["inputs"].update(
                note={
                    "type": "coalesce",
                    "options": [
                        {"type": "event_ref", "path": "note"},
                        {"type": "literal", "value": None},
                    ],
                }
            ),
        ),
        "argument_missing": _mutate(twin(), lambda a: a["steps"]["act"]["inputs"].clear()),
        "argument_unknown": _mutate(
            twin(),
            lambda a: a["steps"]["act"]["inputs"].update(
                nonesuch={"type": "literal", "value": "x"}
            ),
        ),
        "unmapped_business_outcome": _mutate(
            twin(), lambda a: a["steps"]["act"]["transitions"].pop("skipped")
        ),
        "unmapped_reserved_outcome": _mutate(
            twin(), lambda a: a["steps"]["act"]["transitions"].pop("runtime_error")
        ),
        "unknown_transition_outcome": _mutate(
            twin(), lambda a: a["steps"]["act"]["transitions"].update(weird="end")
        ),
        "outcome_enum_mismatch": _mutate(
            twin(),
            lambda a: _set_llm(
                a,
                output_schema={
                    "type": "object",
                    "properties": {"risk": {"enum": ["a", "b"]}},
                    "required": ["risk"],
                },
            ),
        ),
        "llm_branch_without_schema": _mutate(
            twin(),
            lambda a: _set_llm(
                a,
                outcome_field=None,
                output_schema={"type": "object", "properties": {"risk": {"type": "string"}}},
            ),
        ),
        "output_schema_invalid": _mutate(
            twin(),
            lambda a: _set_llm(
                a,
                output_schema={"type": 5},
                outcome_field=None,
                transitions={"runtime_error": "oops"},
            ),
        ),
        "output_schema_too_deep": _mutate(
            twin(),
            lambda a: _set_llm(
                a,
                output_schema=_deep_schema(7),
                outcome_field=None,
                transitions={"runtime_error": "oops"},
            ),
        ),
        "profile_capability_empty": _mutate(twin(), lambda a: _set_llm(a, profile_id="hollow")),
        "tool_use_not_subset": _mutate(
            twin(),
            lambda a: _set_llm(
                a, tool_use={"enabled": True, "aq_commands": ["other_command"]}
            ),
        ),
        "capability_not_subset": _mutate(
            _agent_task_twin(), lambda a: a["steps"]["delegate"].update(profile_id="wide")
        ),
        "narrowing_not_subset": _mutate(
            _agent_task_twin(),
            lambda a: a["steps"]["delegate"].update(
                capability_narrowing={"aq_commands": ["other_command"]}
            ),
        ),
        "delegation_runtime_checked": _mutate(
            _agent_task_twin(),
            lambda a: a["steps"]["classify"].update(
                tool_use={"enabled": False, "aq_commands": []}
            ),
        ),
        "stale_contract": _mutate(
            twin(),
            lambda a: a["compiled_against"]["commands"].update(demo_command=OTHER_FINGERPRINT),
        ),
    }
    return table


def _model_rejection_texts() -> dict[str, str]:
    """§9.3 fixtures the strict models reject before validation runs."""
    boolean = twin()
    boolean["rules"][0]["guard"] = {"type": "bool", "op": "and", "operands": []}
    duplicated = json.dumps(twin(), indent=2)
    marker = '"end": {'
    injected = (
        '"end": {"type": "terminal", "rule": "r1", "title": "Dupe", '
        '"outcome": "completed", "source": {"path": "system/playbooks/twin.md", '
        '"start_line": 3, "end_line": 3}},\n    "end": {'
    )
    return {
        "empty_boolean_operand": json.dumps(boolean, indent=2) + "\n",
        "duplicate_step_id": duplicated.replace(marker, injected, 1) + "\n",
    }


def expected_invalid_files() -> dict[str, str]:
    """``<code>.json`` -> the exact bytes that belong in ``invalid/``."""
    files = {
        f"{code}.json": json.dumps(artifact, indent=2, ensure_ascii=False) + "\n"
        for code, artifact in _invalid_artifacts().items()
    }
    files.update(
        {f"{code}.json": text for code, text in _model_rejection_texts().items()}
    )
    return files


def write_invalid_fixtures() -> None:
    """Regenerate ``tests/fixtures/playbooks/v2/invalid/`` from the table above."""
    INVALID_DIR.mkdir(parents=True, exist_ok=True)
    expected = expected_invalid_files()
    for name, text in expected.items():
        (INVALID_DIR / name).write_text(text)
    for path in INVALID_DIR.glob("*.json"):
        if path.name not in expected:
            path.unlink()


if __name__ == "__main__":  # pragma: no cover - regeneration entry point
    write_invalid_fixtures()
