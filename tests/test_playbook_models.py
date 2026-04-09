"""Tests for compiled playbook models and JSON Schema.

Validates the dataclasses (round-trip serialization, validation logic,
graph helpers) and the generated JSON Schema against the spec example
from ``docs/specs/design/playbooks.md`` Section 5.
"""

from __future__ import annotations

import json

import pytest

from src.playbook_models import (
    CompiledPlaybook,
    LlmConfig,
    NodeTraceEntry,
    PlaybookNode,
    PlaybookRun,
    PlaybookRunStatus,
    PlaybookScope,
    PlaybookTransition,
    generate_json_schema,
)


# ---------------------------------------------------------------------------
# Fixtures — the spec example from §5
# ---------------------------------------------------------------------------

SPEC_EXAMPLE_JSON = {
    "id": "code-quality-gate",
    "version": 1,
    "source_hash": "a1b2c3d4",
    "triggers": ["git.commit"],
    "scope": "system",
    "cooldown_seconds": 60,
    "max_tokens": 50000,
    "nodes": {
        "scan": {
            "entry": True,
            "prompt": (
                "Run vibecop_check on the files changed in this commit. "
                "Use the diff to scope the scan to only changed files, not the entire repo."
            ),
            "transitions": [
                {"when": "no findings", "goto": "done"},
                {"when": "findings exist", "goto": "triage"},
            ],
        },
        "triage": {
            "prompt": "Group the scan findings by severity (error, warning, info).",
            "transitions": [
                {"when": "has errors", "goto": "create_error_tasks"},
                {"when": "warnings only", "goto": "create_warning_task"},
                {"when": "info only", "goto": "log_to_memory"},
            ],
        },
        "create_error_tasks": {
            "prompt": (
                "Create one high-priority task per file that has errors. "
                "Include the vibecop output and file path. If the commit was "
                "made by an agent still running a task, attach as follow-ups "
                "to that agent's task."
            ),
            "goto": "create_warning_task",
        },
        "create_warning_task": {
            "prompt": "Batch all warnings into a single medium-priority task.",
            "goto": "log_to_memory",
        },
        "log_to_memory": {
            "prompt": "Record any info-level findings in project memory for reference.",
            "goto": "done",
        },
        "done": {"terminal": True},
    },
}


@pytest.fixture
def spec_example() -> dict:
    """The code-quality-gate example from the spec, as raw dict."""
    return json.loads(json.dumps(SPEC_EXAMPLE_JSON))  # deep copy


@pytest.fixture
def spec_playbook(spec_example: dict) -> CompiledPlaybook:
    """Parsed CompiledPlaybook from the spec example."""
    return CompiledPlaybook.from_dict(spec_example)


# ---------------------------------------------------------------------------
# LlmConfig
# ---------------------------------------------------------------------------


class TestLlmConfig:
    def test_round_trip(self):
        cfg = LlmConfig(provider="anthropic", model="claude-sonnet-4-20250514")
        d = cfg.to_dict()
        assert d == {"provider": "anthropic", "model": "claude-sonnet-4-20250514"}
        restored = LlmConfig.from_dict(d)
        assert restored == cfg

    def test_empty_fields_omitted(self):
        cfg = LlmConfig()
        assert cfg.to_dict() == {}

    def test_partial_fields(self):
        cfg = LlmConfig(model="gemini-2.0-flash")
        d = cfg.to_dict()
        assert d == {"model": "gemini-2.0-flash"}
        assert "provider" not in d


# ---------------------------------------------------------------------------
# PlaybookTransition
# ---------------------------------------------------------------------------


class TestPlaybookTransition:
    def test_natural_language_round_trip(self):
        t = PlaybookTransition(goto="triage", when="findings exist")
        d = t.to_dict()
        assert d == {"goto": "triage", "when": "findings exist"}
        restored = PlaybookTransition.from_dict(d)
        assert restored == t

    def test_structured_when_round_trip(self):
        expr = {"function": "has_tool_output", "contains": "no findings"}
        t = PlaybookTransition(goto="done", when=expr)
        d = t.to_dict()
        assert d["when"] == expr
        restored = PlaybookTransition.from_dict(d)
        assert restored.when == expr

    def test_otherwise_round_trip(self):
        t = PlaybookTransition(goto="fallback", otherwise=True)
        d = t.to_dict()
        assert d == {"goto": "fallback", "otherwise": True}
        assert "when" not in d
        restored = PlaybookTransition.from_dict(d)
        assert restored.otherwise is True
        assert restored.when is None

    def test_otherwise_defaults_false(self):
        t = PlaybookTransition.from_dict({"goto": "next", "when": "condition"})
        assert t.otherwise is False


# ---------------------------------------------------------------------------
# PlaybookNode
# ---------------------------------------------------------------------------


class TestPlaybookNode:
    def test_action_node_with_transitions(self):
        node = PlaybookNode(
            prompt="Do something.",
            entry=True,
            transitions=[
                PlaybookTransition(goto="a", when="yes"),
                PlaybookTransition(goto="b", when="no"),
            ],
        )
        d = node.to_dict()
        assert d["entry"] is True
        assert d["prompt"] == "Do something."
        assert len(d["transitions"]) == 2
        assert "goto" not in d  # mutually exclusive
        assert "terminal" not in d

    def test_action_node_with_goto(self):
        node = PlaybookNode(prompt="Step 2.", goto="step3")
        d = node.to_dict()
        assert d == {"prompt": "Step 2.", "goto": "step3"}

    def test_terminal_node(self):
        node = PlaybookNode(terminal=True)
        d = node.to_dict()
        assert d == {"terminal": True}
        assert "prompt" not in d

    def test_human_gate_node(self):
        node = PlaybookNode(
            prompt="Review this.",
            wait_for_human=True,
            goto="after_review",
        )
        d = node.to_dict()
        assert d["wait_for_human"] is True
        assert d["goto"] == "after_review"

    def test_optional_fields(self):
        node = PlaybookNode(
            prompt="Complex step.",
            goto="next",
            timeout_seconds=120,
            llm_config=LlmConfig(model="fast-model"),
            summarize_before=True,
        )
        d = node.to_dict()
        assert d["timeout_seconds"] == 120
        assert d["llm_config"] == {"model": "fast-model"}
        assert d["summarize_before"] is True

    def test_round_trip(self):
        node = PlaybookNode(
            prompt="Test.",
            entry=True,
            transitions=[PlaybookTransition(goto="end", when="done")],
            timeout_seconds=60,
            llm_config=LlmConfig(provider="anthropic"),
            summarize_before=True,
        )
        restored = PlaybookNode.from_dict(node.to_dict())
        assert restored.prompt == node.prompt
        assert restored.entry == node.entry
        assert len(restored.transitions) == 1
        assert restored.timeout_seconds == 60
        assert restored.llm_config is not None
        assert restored.llm_config.provider == "anthropic"
        assert restored.summarize_before is True

    def test_defaults(self):
        node = PlaybookNode()
        assert node.prompt == ""
        assert node.entry is False
        assert node.terminal is False
        assert node.transitions == []
        assert node.goto is None
        assert node.wait_for_human is False
        assert node.timeout_seconds is None
        assert node.llm_config is None
        assert node.summarize_before is False


# ---------------------------------------------------------------------------
# CompiledPlaybook — serialization
# ---------------------------------------------------------------------------


class TestCompiledPlaybookSerialization:
    def test_spec_example_round_trip(self, spec_example: dict, spec_playbook: CompiledPlaybook):
        """The spec example should survive a from_dict → to_dict round trip."""
        result = spec_playbook.to_dict()
        assert result == spec_example

    def test_required_fields(self, spec_playbook: CompiledPlaybook):
        assert spec_playbook.id == "code-quality-gate"
        assert spec_playbook.version == 1
        assert spec_playbook.source_hash == "a1b2c3d4"
        assert spec_playbook.triggers == ["git.commit"]
        assert spec_playbook.scope == "system"

    def test_optional_fields(self, spec_playbook: CompiledPlaybook):
        assert spec_playbook.cooldown_seconds == 60
        assert spec_playbook.max_tokens == 50000
        assert spec_playbook.llm_config is None  # not in example

    def test_nodes_parsed(self, spec_playbook: CompiledPlaybook):
        assert len(spec_playbook.nodes) == 6
        assert "scan" in spec_playbook.nodes
        assert "done" in spec_playbook.nodes

    def test_entry_node(self, spec_playbook: CompiledPlaybook):
        scan = spec_playbook.nodes["scan"]
        assert scan.entry is True
        assert scan.prompt.startswith("Run vibecop_check")
        assert len(scan.transitions) == 2

    def test_terminal_node(self, spec_playbook: CompiledPlaybook):
        done = spec_playbook.nodes["done"]
        assert done.terminal is True
        assert done.prompt == ""

    def test_goto_node(self, spec_playbook: CompiledPlaybook):
        create_err = spec_playbook.nodes["create_error_tasks"]
        assert create_err.goto == "create_warning_task"
        assert create_err.transitions == []

    def test_with_llm_config(self):
        data = {
            "id": "test",
            "version": 1,
            "source_hash": "abc",
            "triggers": ["test.event"],
            "scope": "system",
            "llm_config": {"provider": "anthropic", "model": "claude-sonnet-4-20250514"},
            "nodes": {
                "start": {"entry": True, "prompt": "Go.", "goto": "end"},
                "end": {"terminal": True},
            },
        }
        pb = CompiledPlaybook.from_dict(data)
        assert pb.llm_config is not None
        assert pb.llm_config.provider == "anthropic"
        result = pb.to_dict()
        assert result["llm_config"] == {
            "provider": "anthropic",
            "model": "claude-sonnet-4-20250514",
        }

    def test_minimal_playbook_round_trip(self):
        data = {
            "id": "minimal",
            "version": 1,
            "source_hash": "xyz",
            "triggers": ["timer.30m"],
            "scope": "project",
            "nodes": {
                "start": {"entry": True, "prompt": "Do the thing.", "goto": "end"},
                "end": {"terminal": True},
            },
        }
        pb = CompiledPlaybook.from_dict(data)
        assert pb.to_dict() == data


# ---------------------------------------------------------------------------
# CompiledPlaybook — scope helpers
# ---------------------------------------------------------------------------


class TestPlaybookScope:
    def test_system_scope(self):
        pb = CompiledPlaybook(id="t", version=1, source_hash="h", triggers=["x"], scope="system")
        scope, ident = pb.parse_scope()
        assert scope == PlaybookScope.SYSTEM
        assert ident is None

    def test_project_scope(self):
        pb = CompiledPlaybook(id="t", version=1, source_hash="h", triggers=["x"], scope="project")
        scope, ident = pb.parse_scope()
        assert scope == PlaybookScope.PROJECT
        assert ident is None

    def test_agent_type_scope(self):
        pb = CompiledPlaybook(
            id="t",
            version=1,
            source_hash="h",
            triggers=["x"],
            scope="agent-type:coding",
        )
        scope, ident = pb.parse_scope()
        assert scope == PlaybookScope.AGENT_TYPE
        assert ident == "coding"

    def test_agent_type_scope_with_complex_name(self):
        pb = CompiledPlaybook(
            id="t",
            version=1,
            source_hash="h",
            triggers=["x"],
            scope="agent-type:web-developer",
        )
        scope, ident = pb.parse_scope()
        assert scope == PlaybookScope.AGENT_TYPE
        assert ident == "web-developer"

    def test_unknown_scope_defaults_system(self):
        pb = CompiledPlaybook(id="t", version=1, source_hash="h", triggers=["x"], scope="bogus")
        scope, ident = pb.parse_scope()
        assert scope == PlaybookScope.SYSTEM
        assert ident is None


# ---------------------------------------------------------------------------
# CompiledPlaybook — graph helpers
# ---------------------------------------------------------------------------


class TestGraphHelpers:
    def test_entry_node_id(self, spec_playbook: CompiledPlaybook):
        assert spec_playbook.entry_node_id() == "scan"

    def test_entry_node_id_missing(self):
        pb = CompiledPlaybook(
            id="t",
            version=1,
            source_hash="h",
            triggers=["x"],
            scope="system",
            nodes={"a": PlaybookNode(prompt="x", terminal=True)},
        )
        assert pb.entry_node_id() is None

    def test_terminal_node_ids(self, spec_playbook: CompiledPlaybook):
        assert spec_playbook.terminal_node_ids() == ["done"]

    def test_reachable_node_ids(self, spec_playbook: CompiledPlaybook):
        reachable = spec_playbook.reachable_node_ids()
        assert reachable == {
            "scan",
            "done",
            "triage",
            "create_error_tasks",
            "create_warning_task",
            "log_to_memory",
        }

    def test_reachable_detects_unreachable(self):
        pb = CompiledPlaybook(
            id="t",
            version=1,
            source_hash="h",
            triggers=["x"],
            scope="system",
            nodes={
                "start": PlaybookNode(entry=True, prompt="Go.", goto="end"),
                "end": PlaybookNode(terminal=True),
                "orphan": PlaybookNode(prompt="Nobody reaches me.", goto="end"),
            },
        )
        reachable = pb.reachable_node_ids()
        assert "orphan" not in reachable
        assert reachable == {"start", "end"}

    def test_reachable_from_specific_node(self, spec_playbook: CompiledPlaybook):
        reachable = spec_playbook.reachable_node_ids("triage")
        assert "scan" not in reachable
        assert "triage" in reachable
        assert "done" in reachable

    def test_reachable_empty_when_no_entry(self):
        pb = CompiledPlaybook(
            id="t",
            version=1,
            source_hash="h",
            triggers=["x"],
            scope="system",
        )
        assert pb.reachable_node_ids() == set()


# ---------------------------------------------------------------------------
# CompiledPlaybook — validation
# ---------------------------------------------------------------------------


class TestPlaybookValidation:
    def test_spec_example_is_valid(self, spec_playbook: CompiledPlaybook):
        errors = spec_playbook.validate()
        assert errors == [], f"Spec example should be valid, got: {errors}"

    def test_missing_id(self):
        pb = CompiledPlaybook(
            id="",
            version=1,
            source_hash="h",
            triggers=["x"],
            scope="system",
            nodes={
                "s": PlaybookNode(entry=True, prompt="Go.", goto="e"),
                "e": PlaybookNode(terminal=True),
            },
        )
        errors = pb.validate()
        assert any("id" in e for e in errors)

    def test_missing_triggers(self):
        pb = CompiledPlaybook(
            id="t",
            version=1,
            source_hash="h",
            triggers=[],
            scope="system",
            nodes={
                "s": PlaybookNode(entry=True, prompt="Go.", goto="e"),
                "e": PlaybookNode(terminal=True),
            },
        )
        errors = pb.validate()
        assert any("triggers" in e for e in errors)

    def test_missing_scope(self):
        pb = CompiledPlaybook(
            id="t",
            version=1,
            source_hash="h",
            triggers=["x"],
            scope="",
            nodes={
                "s": PlaybookNode(entry=True, prompt="Go.", goto="e"),
                "e": PlaybookNode(terminal=True),
            },
        )
        errors = pb.validate()
        assert any("scope" in e for e in errors)

    def test_missing_source_hash(self):
        pb = CompiledPlaybook(
            id="t",
            version=1,
            source_hash="",
            triggers=["x"],
            scope="system",
            nodes={
                "s": PlaybookNode(entry=True, prompt="Go.", goto="e"),
                "e": PlaybookNode(terminal=True),
            },
        )
        errors = pb.validate()
        assert any("source_hash" in e for e in errors)

    def test_no_nodes(self):
        pb = CompiledPlaybook(
            id="t",
            version=1,
            source_hash="h",
            triggers=["x"],
            scope="system",
        )
        errors = pb.validate()
        assert any("no nodes" in e for e in errors)

    def test_no_entry_node(self):
        pb = CompiledPlaybook(
            id="t",
            version=1,
            source_hash="h",
            triggers=["x"],
            scope="system",
            nodes={
                "a": PlaybookNode(prompt="Go.", goto="b"),
                "b": PlaybookNode(terminal=True),
            },
        )
        errors = pb.validate()
        assert any("entry" in e.lower() for e in errors)

    def test_multiple_entry_nodes(self):
        pb = CompiledPlaybook(
            id="t",
            version=1,
            source_hash="h",
            triggers=["x"],
            scope="system",
            nodes={
                "a": PlaybookNode(entry=True, prompt="Go.", goto="c"),
                "b": PlaybookNode(entry=True, prompt="Also go.", goto="c"),
                "c": PlaybookNode(terminal=True),
            },
        )
        errors = pb.validate()
        assert any("multiple entry" in e.lower() for e in errors)

    def test_no_terminal_node(self):
        pb = CompiledPlaybook(
            id="t",
            version=1,
            source_hash="h",
            triggers=["x"],
            scope="system",
            nodes={
                "a": PlaybookNode(entry=True, prompt="Go.", goto="b"),
                "b": PlaybookNode(prompt="Continue.", goto="a"),  # cycle, no terminal
            },
        )
        errors = pb.validate()
        assert any("terminal" in e.lower() for e in errors)

    def test_non_terminal_without_prompt(self):
        pb = CompiledPlaybook(
            id="t",
            version=1,
            source_hash="h",
            triggers=["x"],
            scope="system",
            nodes={
                "a": PlaybookNode(entry=True, goto="b"),  # missing prompt
                "b": PlaybookNode(terminal=True),
            },
        )
        errors = pb.validate()
        assert any("prompt" in e for e in errors)

    def test_transitions_and_goto_mutually_exclusive(self):
        pb = CompiledPlaybook(
            id="t",
            version=1,
            source_hash="h",
            triggers=["x"],
            scope="system",
            nodes={
                "a": PlaybookNode(
                    entry=True,
                    prompt="Go.",
                    transitions=[PlaybookTransition(goto="b", when="yes")],
                    goto="b",  # conflict!
                ),
                "b": PlaybookNode(terminal=True),
            },
        )
        errors = pb.validate()
        assert any("mutually exclusive" in e for e in errors)

    def test_non_terminal_no_exit_path(self):
        pb = CompiledPlaybook(
            id="t",
            version=1,
            source_hash="h",
            triggers=["x"],
            scope="system",
            nodes={
                "a": PlaybookNode(entry=True, prompt="Dead end."),
                "b": PlaybookNode(terminal=True),
            },
        )
        errors = pb.validate()
        assert any("transitions" in e and "goto" in e for e in errors)

    def test_transition_target_missing(self):
        pb = CompiledPlaybook(
            id="t",
            version=1,
            source_hash="h",
            triggers=["x"],
            scope="system",
            nodes={
                "a": PlaybookNode(
                    entry=True,
                    prompt="Go.",
                    transitions=[PlaybookTransition(goto="nonexistent", when="yes")],
                ),
                "b": PlaybookNode(terminal=True),
            },
        )
        errors = pb.validate()
        assert any("nonexistent" in e for e in errors)

    def test_goto_target_missing(self):
        pb = CompiledPlaybook(
            id="t",
            version=1,
            source_hash="h",
            triggers=["x"],
            scope="system",
            nodes={
                "a": PlaybookNode(entry=True, prompt="Go.", goto="nonexistent"),
                "b": PlaybookNode(terminal=True),
            },
        )
        errors = pb.validate()
        assert any("nonexistent" in e for e in errors)

    def test_unreachable_nodes(self):
        pb = CompiledPlaybook(
            id="t",
            version=1,
            source_hash="h",
            triggers=["x"],
            scope="system",
            nodes={
                "a": PlaybookNode(entry=True, prompt="Go.", goto="b"),
                "b": PlaybookNode(terminal=True),
                "orphan": PlaybookNode(prompt="Alone.", goto="b"),
            },
        )
        errors = pb.validate()
        assert any("unreachable" in e.lower() for e in errors)
        assert any("orphan" in e for e in errors)

    def test_transition_without_when_or_otherwise(self):
        pb = CompiledPlaybook(
            id="t",
            version=1,
            source_hash="h",
            triggers=["x"],
            scope="system",
            nodes={
                "a": PlaybookNode(
                    entry=True,
                    prompt="Go.",
                    transitions=[PlaybookTransition(goto="b")],  # no when or otherwise
                ),
                "b": PlaybookNode(terminal=True),
            },
        )
        errors = pb.validate()
        assert any("when" in e and "otherwise" in e for e in errors)

    def test_valid_minimal_playbook(self):
        pb = CompiledPlaybook(
            id="minimal",
            version=1,
            source_hash="abc",
            triggers=["timer.5m"],
            scope="system",
            nodes={
                "start": PlaybookNode(entry=True, prompt="Do it.", goto="end"),
                "end": PlaybookNode(terminal=True),
            },
        )
        assert pb.validate() == []

    def test_valid_with_otherwise_transition(self):
        pb = CompiledPlaybook(
            id="t",
            version=1,
            source_hash="h",
            triggers=["x"],
            scope="system",
            nodes={
                "start": PlaybookNode(
                    entry=True,
                    prompt="Check.",
                    transitions=[
                        PlaybookTransition(goto="a", when="condition met"),
                        PlaybookTransition(goto="b", otherwise=True),
                    ],
                ),
                "a": PlaybookNode(prompt="Path A.", goto="end"),
                "b": PlaybookNode(prompt="Path B.", goto="end"),
                "end": PlaybookNode(terminal=True),
            },
        )
        assert pb.validate() == []

    def test_wait_for_human_node_needs_exit(self):
        """A wait_for_human node without goto/transitions is allowed (pauses)."""
        pb = CompiledPlaybook(
            id="t",
            version=1,
            source_hash="h",
            triggers=["x"],
            scope="system",
            nodes={
                "start": PlaybookNode(
                    entry=True,
                    prompt="Review.",
                    wait_for_human=True,
                ),
                "end": PlaybookNode(terminal=True),
            },
        )
        errors = pb.validate()
        # wait_for_human without exit is allowed — executor handles resume
        assert not any("transitions" in e and "goto" in e for e in errors)
        # wait_for_human should NOT be flagged as trapped in a cycle
        assert not any("cycles without exit" in e.lower() for e in errors)

    # -- Check 10: Cycles without exits ------------------------------------

    def test_simple_cycle_without_exit(self):
        """Two nodes forming a cycle with no path to terminal → error."""
        pb = CompiledPlaybook(
            id="t",
            version=1,
            source_hash="h",
            triggers=["x"],
            scope="system",
            nodes={
                "a": PlaybookNode(entry=True, prompt="Step A.", goto="b"),
                "b": PlaybookNode(prompt="Step B.", goto="a"),
                "done": PlaybookNode(terminal=True),
            },
        )
        errors = pb.validate()
        cycle_errors = [e for e in errors if "cycles without exit" in e.lower()]
        assert len(cycle_errors) == 1
        assert "a" in cycle_errors[0]
        assert "b" in cycle_errors[0]
        # The terminal 'done' is unreachable but NOT in the cycle error
        assert any("unreachable" in e.lower() for e in errors)

    def test_cycle_with_exit_is_valid(self):
        """A cycle that has an escape path to terminal is valid (retry loop)."""
        pb = CompiledPlaybook(
            id="t",
            version=1,
            source_hash="h",
            triggers=["x"],
            scope="system",
            nodes={
                "start": PlaybookNode(
                    entry=True,
                    prompt="Try the thing.",
                    transitions=[
                        PlaybookTransition(goto="done", when="success"),
                        PlaybookTransition(goto="retry", when="failure"),
                    ],
                ),
                "retry": PlaybookNode(prompt="Fix and try again.", goto="start"),
                "done": PlaybookNode(terminal=True),
            },
        )
        errors = pb.validate()
        assert errors == [], f"Cycle with exit should be valid, got: {errors}"

    def test_self_loop_without_exit(self):
        """A node looping to itself with no escape path → error."""
        pb = CompiledPlaybook(
            id="t",
            version=1,
            source_hash="h",
            triggers=["x"],
            scope="system",
            nodes={
                "loop": PlaybookNode(entry=True, prompt="Forever.", goto="loop"),
                "end": PlaybookNode(terminal=True),
            },
        )
        errors = pb.validate()
        cycle_errors = [e for e in errors if "cycles without exit" in e.lower()]
        assert len(cycle_errors) == 1
        assert "loop" in cycle_errors[0]

    def test_self_loop_with_exit(self):
        """A node that can loop to itself OR proceed to terminal is valid."""
        pb = CompiledPlaybook(
            id="t",
            version=1,
            source_hash="h",
            triggers=["x"],
            scope="system",
            nodes={
                "poll": PlaybookNode(
                    entry=True,
                    prompt="Check status.",
                    transitions=[
                        PlaybookTransition(goto="poll", when="not ready"),
                        PlaybookTransition(goto="done", when="ready"),
                    ],
                ),
                "done": PlaybookNode(terminal=True),
            },
        )
        errors = pb.validate()
        assert errors == [], f"Self-loop with exit should be valid, got: {errors}"

    def test_complex_graph_partial_cycle_trap(self):
        """Only nodes trapped in the cycle are reported, not the whole graph."""
        pb = CompiledPlaybook(
            id="t",
            version=1,
            source_hash="h",
            triggers=["x"],
            scope="system",
            nodes={
                "start": PlaybookNode(
                    entry=True,
                    prompt="Begin.",
                    transitions=[
                        PlaybookTransition(goto="good_path", when="option A"),
                        PlaybookTransition(goto="bad_cycle_a", when="option B"),
                    ],
                ),
                "good_path": PlaybookNode(prompt="This works.", goto="done"),
                "bad_cycle_a": PlaybookNode(prompt="Trapped A.", goto="bad_cycle_b"),
                "bad_cycle_b": PlaybookNode(prompt="Trapped B.", goto="bad_cycle_a"),
                "done": PlaybookNode(terminal=True),
            },
        )
        errors = pb.validate()
        cycle_errors = [e for e in errors if "cycles without exit" in e.lower()]
        assert len(cycle_errors) == 1
        assert "bad_cycle_a" in cycle_errors[0]
        assert "bad_cycle_b" in cycle_errors[0]
        # start and good_path should NOT be flagged
        assert "start" not in cycle_errors[0]
        assert "good_path" not in cycle_errors[0]

    def test_three_node_cycle_with_one_exit(self):
        """A→B→C→A where C also goes to terminal → all can reach terminal."""
        pb = CompiledPlaybook(
            id="t",
            version=1,
            source_hash="h",
            triggers=["x"],
            scope="system",
            nodes={
                "a": PlaybookNode(entry=True, prompt="Step A.", goto="b"),
                "b": PlaybookNode(prompt="Step B.", goto="c"),
                "c": PlaybookNode(
                    prompt="Step C.",
                    transitions=[
                        PlaybookTransition(goto="a", when="retry"),
                        PlaybookTransition(goto="done", otherwise=True),
                    ],
                ),
                "done": PlaybookNode(terminal=True),
            },
        )
        errors = pb.validate()
        assert errors == [], f"Cycle with exit should be valid, got: {errors}"

    def test_wait_for_human_with_goto_in_cycle_detected(self):
        """wait_for_human node WITH explicit goto in a cycle IS flagged."""
        pb = CompiledPlaybook(
            id="t",
            version=1,
            source_hash="h",
            triggers=["x"],
            scope="system",
            nodes={
                "review": PlaybookNode(
                    entry=True,
                    prompt="Review.",
                    wait_for_human=True,
                    goto="process",
                ),
                "process": PlaybookNode(prompt="Process.", goto="review"),
                "done": PlaybookNode(terminal=True),
            },
        )
        errors = pb.validate()
        cycle_errors = [e for e in errors if "cycles without exit" in e.lower()]
        assert len(cycle_errors) == 1
        assert "review" in cycle_errors[0]
        assert "process" in cycle_errors[0]

    # -- Check 11: Multiple otherwise transitions --------------------------

    def test_multiple_otherwise_transitions(self):
        """A node with multiple 'otherwise' transitions → error."""
        pb = CompiledPlaybook(
            id="t",
            version=1,
            source_hash="h",
            triggers=["x"],
            scope="system",
            nodes={
                "start": PlaybookNode(
                    entry=True,
                    prompt="Check.",
                    transitions=[
                        PlaybookTransition(goto="a", when="condition"),
                        PlaybookTransition(goto="b", otherwise=True),
                        PlaybookTransition(goto="c", otherwise=True),  # duplicate!
                    ],
                ),
                "a": PlaybookNode(prompt="A.", goto="end"),
                "b": PlaybookNode(prompt="B.", goto="end"),
                "c": PlaybookNode(prompt="C.", goto="end"),
                "end": PlaybookNode(terminal=True),
            },
        )
        errors = pb.validate()
        assert any("otherwise" in e and "start" in e for e in errors)

    def test_single_otherwise_transition_is_valid(self):
        """A node with exactly one 'otherwise' transition is fine."""
        pb = CompiledPlaybook(
            id="t",
            version=1,
            source_hash="h",
            triggers=["x"],
            scope="system",
            nodes={
                "start": PlaybookNode(
                    entry=True,
                    prompt="Check.",
                    transitions=[
                        PlaybookTransition(goto="a", when="condition"),
                        PlaybookTransition(goto="b", otherwise=True),
                    ],
                ),
                "a": PlaybookNode(prompt="A.", goto="end"),
                "b": PlaybookNode(prompt="B.", goto="end"),
                "end": PlaybookNode(terminal=True),
            },
        )
        errors = pb.validate()
        assert errors == [], f"Single otherwise should be valid, got: {errors}"


# ---------------------------------------------------------------------------
# Roadmap 5.1.11 — Graph validation test cases (a)-(h)
# ---------------------------------------------------------------------------


class TestRoadmapGraphValidation:
    """Roadmap 5.1.11: Graph validation test cases per §19 Q6.

    Each test maps to a specific case from the roadmap:
      (a) unreachable node names the node in the error
      (b) no entry node → validation error
      (c) nonexistent transition target names the invalid target
      (d) cycle without exit condition → validation error
      (e) cycle WITH exit condition passes
      (f) valid graph passes silently
      (g) single node (entry = terminal) is valid
      (h) duplicate node names → validation error
    """

    def test_a_unreachable_node_produces_error_naming_node(self):
        """(a) Graph with unreachable node produces error naming the unreachable node."""
        pb = CompiledPlaybook(
            id="test-a",
            version=1,
            source_hash="abc",
            triggers=["test.event"],
            scope="system",
            nodes={
                "entry": PlaybookNode(entry=True, prompt="Start.", goto="end"),
                "end": PlaybookNode(terminal=True),
                "island": PlaybookNode(prompt="No one reaches me.", goto="end"),
            },
        )
        errors = pb.validate()
        # Must mention the unreachable node by name
        unreachable_errors = [e for e in errors if "unreachable" in e.lower()]
        assert len(unreachable_errors) == 1
        assert "island" in unreachable_errors[0]

    def test_b_no_entry_node_produces_error(self):
        """(b) Graph with no entry node defined produces validation error."""
        pb = CompiledPlaybook(
            id="test-b",
            version=1,
            source_hash="abc",
            triggers=["test.event"],
            scope="system",
            nodes={
                "step": PlaybookNode(prompt="Go.", goto="end"),
                "end": PlaybookNode(terminal=True),
            },
        )
        errors = pb.validate()
        entry_errors = [e for e in errors if "entry" in e.lower()]
        assert len(entry_errors) >= 1, f"Expected entry error, got: {errors}"

    def test_c_nonexistent_target_names_invalid_target(self):
        """(c) Transition referencing non-existent target names the invalid target."""
        pb = CompiledPlaybook(
            id="test-c",
            version=1,
            source_hash="abc",
            triggers=["test.event"],
            scope="system",
            nodes={
                "start": PlaybookNode(
                    entry=True,
                    prompt="Check.",
                    transitions=[
                        PlaybookTransition(goto="real_end", when="ok"),
                        PlaybookTransition(goto="ghost_node", when="bad"),
                    ],
                ),
                "real_end": PlaybookNode(terminal=True),
            },
        )
        errors = pb.validate()
        # Must mention the invalid target name
        target_errors = [e for e in errors if "ghost_node" in e]
        assert len(target_errors) >= 1, f"Expected error naming 'ghost_node', got: {errors}"

    def test_c_nonexistent_goto_target_names_invalid_target(self):
        """(c) Node goto referencing non-existent target names the invalid target."""
        pb = CompiledPlaybook(
            id="test-c2",
            version=1,
            source_hash="abc",
            triggers=["test.event"],
            scope="system",
            nodes={
                "start": PlaybookNode(entry=True, prompt="Go.", goto="missing_target"),
                "end": PlaybookNode(terminal=True),
            },
        )
        errors = pb.validate()
        target_errors = [e for e in errors if "missing_target" in e]
        assert len(target_errors) >= 1, f"Expected error naming 'missing_target', got: {errors}"

    def test_d_cycle_without_exit_produces_error(self):
        """(d) Graph with cycle but no exit condition produces validation error."""
        pb = CompiledPlaybook(
            id="test-d",
            version=1,
            source_hash="abc",
            triggers=["test.event"],
            scope="system",
            nodes={
                "a": PlaybookNode(entry=True, prompt="Step A.", goto="b"),
                "b": PlaybookNode(prompt="Step B.", goto="a"),
                "done": PlaybookNode(terminal=True),
            },
        )
        errors = pb.validate()
        cycle_errors = [e for e in errors if "cycle" in e.lower()]
        assert len(cycle_errors) >= 1, f"Expected cycle error, got: {errors}"
        # Both trapped nodes should be mentioned
        assert "a" in cycle_errors[0]
        assert "b" in cycle_errors[0]

    def test_e_cycle_with_exit_passes(self):
        """(e) Graph with cycle AND exit condition passes validation."""
        pb = CompiledPlaybook(
            id="test-e",
            version=1,
            source_hash="abc",
            triggers=["test.event"],
            scope="system",
            nodes={
                "check": PlaybookNode(
                    entry=True,
                    prompt="Run the check.",
                    transitions=[
                        PlaybookTransition(goto="done", when="all clear"),
                        PlaybookTransition(goto="fix", when="issues found"),
                    ],
                ),
                "fix": PlaybookNode(prompt="Fix issues.", goto="check"),
                "done": PlaybookNode(terminal=True),
            },
        )
        errors = pb.validate()
        assert errors == [], f"Cycle with exit should pass, got: {errors}"

    def test_f_valid_graph_passes_silently(self):
        """(f) Valid graph (all nodes reachable, entry exists, all targets valid) passes."""
        pb = CompiledPlaybook(
            id="test-f",
            version=1,
            source_hash="abc",
            triggers=["git.push"],
            scope="project",
            nodes={
                "scan": PlaybookNode(
                    entry=True,
                    prompt="Scan for issues.",
                    transitions=[
                        PlaybookTransition(goto="report", when="findings"),
                        PlaybookTransition(goto="done", otherwise=True),
                    ],
                ),
                "report": PlaybookNode(prompt="Generate report.", goto="done"),
                "done": PlaybookNode(terminal=True),
            },
        )
        errors = pb.validate()
        assert errors == [], f"Valid graph should pass silently, got: {errors}"

    def test_g_single_node_entry_terminal_is_valid(self):
        """(g) Graph with single node (entry = terminal) is valid."""
        pb = CompiledPlaybook(
            id="test-g",
            version=1,
            source_hash="abc",
            triggers=["timer.5m"],
            scope="system",
            nodes={
                "only": PlaybookNode(entry=True, terminal=True),
            },
        )
        errors = pb.validate()
        assert errors == [], f"Single entry+terminal node should be valid, got: {errors}"

    def test_h_duplicate_node_names_produces_error(self):
        """(h) Graph with duplicate node names in JSON produces validation error.

        Python's ``json.loads`` silently keeps the last value for duplicate
        keys.  ``CompiledPlaybook.from_json`` detects this and reports it
        as a parse-level error.
        """
        # Manually construct JSON with duplicate node keys — json.dumps can't do this
        # because Python dicts already enforce unique keys.
        raw_json = (
            '{"id": "test-h", "version": 1, "source_hash": "abc", '
            '"triggers": ["test.event"], "scope": "system", '
            '"nodes": {'
            '  "step": {"entry": true, "prompt": "First definition.", "goto": "end"},'
            '  "end": {"terminal": true},'
            '  "step": {"prompt": "Second definition — overwrites first!", "goto": "end"}'
            "}}"
        )
        pb, parse_errors = CompiledPlaybook.from_json(raw_json)
        # Must report duplicate key
        assert len(parse_errors) >= 1
        assert any("step" in e.lower() or "duplicate" in e.lower() for e in parse_errors)
        # The resulting playbook is also structurally broken (lost entry node)
        validation_errors = pb.validate()
        assert any("entry" in e.lower() for e in validation_errors)


# ---------------------------------------------------------------------------
# Graph helper: nodes_reaching_terminal
# ---------------------------------------------------------------------------


class TestNodesReachingTerminal:
    def test_spec_example_all_reach_terminal(self, spec_playbook: CompiledPlaybook):
        reaching = spec_playbook.nodes_reaching_terminal()
        assert reaching == set(spec_playbook.nodes.keys())

    def test_simple_linear(self):
        pb = CompiledPlaybook(
            id="t",
            version=1,
            source_hash="h",
            triggers=["x"],
            scope="system",
            nodes={
                "a": PlaybookNode(entry=True, prompt="Go.", goto="b"),
                "b": PlaybookNode(prompt="Continue.", goto="c"),
                "c": PlaybookNode(terminal=True),
            },
        )
        assert pb.nodes_reaching_terminal() == {"a", "b", "c"}

    def test_cycle_without_exit(self):
        pb = CompiledPlaybook(
            id="t",
            version=1,
            source_hash="h",
            triggers=["x"],
            scope="system",
            nodes={
                "a": PlaybookNode(entry=True, prompt="Go.", goto="b"),
                "b": PlaybookNode(prompt="Loop.", goto="a"),
                "end": PlaybookNode(terminal=True),
            },
        )
        reaching = pb.nodes_reaching_terminal()
        assert "end" in reaching
        assert "a" not in reaching
        assert "b" not in reaching

    def test_cycle_with_exit(self):
        pb = CompiledPlaybook(
            id="t",
            version=1,
            source_hash="h",
            triggers=["x"],
            scope="system",
            nodes={
                "a": PlaybookNode(
                    entry=True,
                    prompt="Try.",
                    transitions=[
                        PlaybookTransition(goto="b", when="retry"),
                        PlaybookTransition(goto="end", when="done"),
                    ],
                ),
                "b": PlaybookNode(prompt="Fix.", goto="a"),
                "end": PlaybookNode(terminal=True),
            },
        )
        assert pb.nodes_reaching_terminal() == {"a", "b", "end"}

    def test_no_terminal_nodes(self):
        pb = CompiledPlaybook(
            id="t",
            version=1,
            source_hash="h",
            triggers=["x"],
            scope="system",
            nodes={
                "a": PlaybookNode(entry=True, prompt="Go.", goto="b"),
                "b": PlaybookNode(prompt="Loop.", goto="a"),
            },
        )
        assert pb.nodes_reaching_terminal() == set()

    def test_multiple_terminals(self):
        pb = CompiledPlaybook(
            id="t",
            version=1,
            source_hash="h",
            triggers=["x"],
            scope="system",
            nodes={
                "start": PlaybookNode(
                    entry=True,
                    prompt="Check.",
                    transitions=[
                        PlaybookTransition(goto="success", when="ok"),
                        PlaybookTransition(goto="failure", when="bad"),
                    ],
                ),
                "success": PlaybookNode(terminal=True),
                "failure": PlaybookNode(terminal=True),
            },
        )
        assert pb.nodes_reaching_terminal() == {"start", "success", "failure"}


# ---------------------------------------------------------------------------
# NodeTraceEntry
# ---------------------------------------------------------------------------


class TestNodeTraceEntry:
    def test_round_trip(self):
        entry = NodeTraceEntry(
            node_id="scan",
            started_at=1000.0,
            completed_at=1005.0,
            status="completed",
        )
        d = entry.to_dict()
        assert d == {
            "node_id": "scan",
            "started_at": 1000.0,
            "completed_at": 1005.0,
            "status": "completed",
        }
        restored = NodeTraceEntry.from_dict(d)
        assert restored == entry

    def test_running_entry_omits_completed_at(self):
        entry = NodeTraceEntry(node_id="step1", started_at=1000.0)
        d = entry.to_dict()
        assert "completed_at" not in d
        assert d["status"] == "running"


# ---------------------------------------------------------------------------
# PlaybookRun
# ---------------------------------------------------------------------------


class TestPlaybookRun:
    def test_round_trip(self):
        run = PlaybookRun(
            run_id="run-123",
            playbook_id="code-quality-gate",
            playbook_version=1,
            trigger_event={"type": "git.commit", "commit_hash": "abc"},
            status=PlaybookRunStatus.COMPLETED,
            current_node="done",
            conversation_history=[
                {"role": "user", "content": "Event received: ..."},
                {"role": "assistant", "content": "Running scan..."},
            ],
            node_trace=[
                NodeTraceEntry(
                    node_id="scan", started_at=1000.0, completed_at=1005.0, status="completed"
                ),
                NodeTraceEntry(
                    node_id="done", started_at=1005.0, completed_at=1005.1, status="completed"
                ),
            ],
            tokens_used=2500,
            started_at=1000.0,
            completed_at=1005.1,
        )
        d = run.to_dict()
        assert d["status"] == "completed"
        assert len(d["node_trace"]) == 2
        assert d["tokens_used"] == 2500

        restored = PlaybookRun.from_dict(d)
        assert restored.run_id == "run-123"
        assert restored.status == PlaybookRunStatus.COMPLETED
        assert len(restored.node_trace) == 2
        assert restored.node_trace[0].node_id == "scan"

    def test_failed_run(self):
        run = PlaybookRun(
            run_id="run-456",
            playbook_id="test",
            playbook_version=1,
            status=PlaybookRunStatus.FAILED,
            current_node="step2",
            error="LLM call timed out",
            started_at=2000.0,
        )
        d = run.to_dict()
        assert d["status"] == "failed"
        assert d["error"] == "LLM call timed out"
        assert d["current_node"] == "step2"
        assert "completed_at" not in d

    def test_paused_run_preserves_history(self):
        history = [
            {"role": "user", "content": "prompt 1"},
            {"role": "assistant", "content": "response 1"},
        ]
        run = PlaybookRun(
            run_id="run-789",
            playbook_id="review",
            playbook_version=2,
            status=PlaybookRunStatus.PAUSED,
            current_node="human_review",
            conversation_history=history,
            started_at=3000.0,
        )
        d = run.to_dict()
        restored = PlaybookRun.from_dict(d)
        assert restored.status == PlaybookRunStatus.PAUSED
        assert restored.conversation_history == history


# ---------------------------------------------------------------------------
# JSON Schema
# ---------------------------------------------------------------------------


class TestJsonSchema:
    def test_schema_is_valid_structure(self):
        schema = generate_json_schema()
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["type"] == "object"
        assert "id" in schema["required"]
        assert "version" in schema["required"]
        assert "source_hash" in schema["required"]
        assert "triggers" in schema["required"]
        assert "scope" in schema["required"]
        assert "nodes" in schema["required"]

    def test_schema_has_definitions(self):
        schema = generate_json_schema()
        assert "node" in schema["$defs"]
        assert "transition" in schema["$defs"]
        assert "llm_config" in schema["$defs"]

    def test_schema_validates_spec_example(self):
        """If jsonschema is available, validate the spec example against the schema."""
        try:
            import jsonschema
        except ImportError:
            pytest.skip("jsonschema not installed")

        schema = generate_json_schema()
        # Should not raise
        jsonschema.validate(instance=SPEC_EXAMPLE_JSON, schema=schema)

    def test_schema_rejects_missing_required_fields(self):
        """Schema should reject a playbook missing required fields."""
        try:
            import jsonschema
        except ImportError:
            pytest.skip("jsonschema not installed")

        schema = generate_json_schema()
        invalid = {"id": "test", "version": 1}  # missing triggers, scope, nodes, source_hash
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=invalid, schema=schema)

    def test_schema_rejects_bad_scope(self):
        """Schema should reject invalid scope values."""
        try:
            import jsonschema
        except ImportError:
            pytest.skip("jsonschema not installed")

        schema = generate_json_schema()
        bad = {
            "id": "test",
            "version": 1,
            "source_hash": "abc",
            "triggers": ["x"],
            "scope": "invalid-scope",
            "nodes": {"s": {"entry": True, "prompt": "Go.", "goto": "e"}, "e": {"terminal": True}},
        }
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=bad, schema=schema)

    def test_schema_accepts_agent_type_scope(self):
        """Schema should accept agent-type:X scope values."""
        try:
            import jsonschema
        except ImportError:
            pytest.skip("jsonschema not installed")

        schema = generate_json_schema()
        valid = {
            "id": "test",
            "version": 1,
            "source_hash": "abc",
            "triggers": ["x"],
            "scope": "agent-type:coding",
            "nodes": {"s": {"entry": True, "prompt": "Go.", "goto": "e"}, "e": {"terminal": True}},
        }
        jsonschema.validate(instance=valid, schema=schema)

    def test_schema_file_matches_generated(self):
        """The checked-in schema file should match the generated output."""
        import pathlib

        schema_path = pathlib.Path(__file__).parent.parent / "src" / "playbook_schema.json"
        if not schema_path.exists():
            pytest.skip("playbook_schema.json not found")

        with open(schema_path) as f:
            on_disk = json.load(f)
        generated = generate_json_schema()
        assert on_disk == generated, (
            "playbook_schema.json is out of sync with generate_json_schema(). Regenerate it."
        )
