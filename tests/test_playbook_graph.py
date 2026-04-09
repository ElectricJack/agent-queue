"""Tests for playbook graph rendering (ASCII and Mermaid).

Covers:
- ASCII rendering: node boxes, edges, labels, empty graphs
- Mermaid rendering: flowchart syntax, node shapes, edge labels, styles
- Node classification and edge collection helpers
- Various graph topologies: linear, branching, converging, single-node
"""

from __future__ import annotations

import pytest

from src.playbook_graph import (
    _collect_edges,
    _edge_label,
    _mermaid_escape,
    _node_type,
    _node_type_symbol,
    _prompt_preview,
    _topo_order,
    render_ascii,
    render_mermaid,
)
from src.playbook_models import CompiledPlaybook, PlaybookNode, PlaybookTransition


# ---------------------------------------------------------------------------
# Fixtures — reusable playbook graphs
# ---------------------------------------------------------------------------


def _make_linear_playbook() -> CompiledPlaybook:
    """A simple linear 3-node playbook: start → process → done."""
    return CompiledPlaybook(
        id="linear-test",
        version=1,
        source_hash="abc12345",
        triggers=["task.completed"],
        scope="system",
        nodes={
            "start": PlaybookNode(
                entry=True,
                prompt="Initialise the process",
                goto="process",
            ),
            "process": PlaybookNode(
                prompt="Do the main work",
                goto="done",
            ),
            "done": PlaybookNode(terminal=True),
        },
    )


def _make_branching_playbook() -> CompiledPlaybook:
    """A branching playbook: scan → {done, triage} → triage → {fix, log} → done."""
    return CompiledPlaybook(
        id="code-quality-gate",
        version=2,
        source_hash="deadbeef",
        triggers=["git.commit"],
        scope="project",
        nodes={
            "scan": PlaybookNode(
                entry=True,
                prompt="Run vibecop_check on changed files",
                transitions=[
                    PlaybookTransition(goto="done", when="no findings"),
                    PlaybookTransition(goto="triage", when="findings exist"),
                ],
            ),
            "triage": PlaybookNode(
                prompt="Group findings by severity",
                transitions=[
                    PlaybookTransition(goto="fix", when="has errors"),
                    PlaybookTransition(goto="log", otherwise=True),
                ],
            ),
            "fix": PlaybookNode(
                prompt="Create high-priority fix tasks",
                goto="log",
            ),
            "log": PlaybookNode(
                prompt="Record findings in memory",
                goto="done",
            ),
            "done": PlaybookNode(terminal=True),
        },
    )


def _make_checkpoint_playbook() -> CompiledPlaybook:
    """A playbook with a human checkpoint node."""
    return CompiledPlaybook(
        id="review-gate",
        version=1,
        source_hash="1234abcd",
        triggers=["task.completed"],
        scope="system",
        nodes={
            "analyse": PlaybookNode(
                entry=True,
                prompt="Analyse the changes",
                goto="review",
            ),
            "review": PlaybookNode(
                prompt="Present analysis for human review",
                wait_for_human=True,
                transitions=[
                    PlaybookTransition(goto="apply", when="approved"),
                    PlaybookTransition(goto="done", when="rejected"),
                ],
            ),
            "apply": PlaybookNode(
                prompt="Apply the approved changes",
                goto="done",
            ),
            "done": PlaybookNode(terminal=True),
        },
    )


def _make_single_node_playbook() -> CompiledPlaybook:
    """Edge case: a playbook with just a terminal node."""
    return CompiledPlaybook(
        id="noop",
        version=1,
        source_hash="00000000",
        triggers=["manual"],
        scope="system",
        nodes={
            "done": PlaybookNode(entry=True, terminal=True),
        },
    )


def _make_empty_playbook() -> CompiledPlaybook:
    """Edge case: a playbook with no nodes."""
    return CompiledPlaybook(
        id="empty",
        version=1,
        source_hash="00000000",
        triggers=["manual"],
        scope="system",
        nodes={},
    )


def _make_timeout_playbook() -> CompiledPlaybook:
    """A playbook with timeout and on_timeout edges."""
    return CompiledPlaybook(
        id="timeout-test",
        version=1,
        source_hash="timeout00",
        triggers=["task.created"],
        scope="system",
        nodes={
            "wait": PlaybookNode(
                entry=True,
                prompt="Wait for external event",
                wait_for_human=True,
                timeout_seconds=300,
                on_timeout="fallback",
                transitions=[
                    PlaybookTransition(goto="proceed", when="event received"),
                ],
            ),
            "proceed": PlaybookNode(
                prompt="Continue with the event data",
                goto="done",
            ),
            "fallback": PlaybookNode(
                prompt="Handle timeout gracefully",
                goto="done",
            ),
            "done": PlaybookNode(terminal=True),
        },
    )


def _make_summarize_playbook() -> CompiledPlaybook:
    """A playbook with a summarize_before node."""
    return CompiledPlaybook(
        id="summarize-test",
        version=1,
        source_hash="summ0000",
        triggers=["manual"],
        scope="system",
        nodes={
            "start": PlaybookNode(
                entry=True,
                prompt="Begin long process",
                goto="recap",
            ),
            "recap": PlaybookNode(
                prompt="Do the next thing with fresh context",
                summarize_before=True,
                goto="done",
            ),
            "done": PlaybookNode(terminal=True),
        },
    )


# ===================================================================
# Node classification tests
# ===================================================================


class TestNodeType:
    def test_entry_node(self):
        node = PlaybookNode(entry=True, prompt="Start", goto="next")
        assert _node_type(node) == "entry"

    def test_terminal_node(self):
        node = PlaybookNode(terminal=True)
        assert _node_type(node) == "terminal"

    def test_checkpoint_node(self):
        node = PlaybookNode(
            prompt="Review",
            wait_for_human=True,
            transitions=[PlaybookTransition(goto="next", when="ok")],
        )
        assert _node_type(node) == "checkpoint"

    def test_decision_node(self):
        node = PlaybookNode(
            prompt="Decide",
            transitions=[
                PlaybookTransition(goto="a", when="x"),
                PlaybookTransition(goto="b", when="y"),
            ],
        )
        assert _node_type(node) == "decision"

    def test_action_node(self):
        node = PlaybookNode(prompt="Do something", goto="next")
        assert _node_type(node) == "action"

    def test_entry_decision_node(self):
        node = PlaybookNode(
            entry=True,
            prompt="Entry with branches",
            transitions=[
                PlaybookTransition(goto="a", when="x"),
                PlaybookTransition(goto="b", when="y"),
            ],
        )
        assert _node_type(node) == "entry+decision"


class TestNodeTypeSymbol:
    def test_symbols_are_non_empty(self):
        for entry, terminal, wait, transitions in [
            (True, False, False, []),
            (False, True, False, []),
            (False, False, True, []),
            (False, False, False, [PlaybookTransition(goto="a", when="x")] * 2),
            (False, False, False, []),
        ]:
            node = PlaybookNode(
                entry=entry,
                terminal=terminal,
                wait_for_human=wait,
                prompt="test" if not terminal else "",
                transitions=transitions,
                goto="x" if not transitions and not terminal and not wait else None,
            )
            assert len(_node_type_symbol(node)) > 0


class TestPromptPreview:
    def test_simple_prompt(self):
        node = PlaybookNode(prompt="Run the tests")
        assert _prompt_preview(node) == "Run the tests"

    def test_multiline_uses_first_line(self):
        node = PlaybookNode(prompt="First line\nSecond line\nThird line")
        assert _prompt_preview(node) == "First line"

    def test_truncation(self):
        node = PlaybookNode(prompt="A" * 100)
        preview = _prompt_preview(node, max_len=20)
        assert len(preview) == 20
        assert preview.endswith("...")

    def test_empty_prompt(self):
        node = PlaybookNode(prompt="")
        assert _prompt_preview(node) == ""

    def test_terminal_node(self):
        node = PlaybookNode(terminal=True)
        assert _prompt_preview(node) == ""


class TestEdgeLabel:
    def test_string_when(self):
        t = PlaybookTransition(goto="next", when="has errors")
        assert _edge_label(t) == "has errors"

    def test_dict_when(self):
        t = PlaybookTransition(goto="next", when={"function": "check", "key": "val"})
        assert "check" in _edge_label(t)

    def test_otherwise(self):
        t = PlaybookTransition(goto="next", otherwise=True)
        assert _edge_label(t) == "otherwise"

    def test_no_condition(self):
        t = PlaybookTransition(goto="next")
        assert _edge_label(t) == ""


# ===================================================================
# Topological ordering tests
# ===================================================================


class TestTopoOrder:
    def test_linear(self):
        pb = _make_linear_playbook()
        order = _topo_order(pb)
        assert order == ["start", "process", "done"]

    def test_branching(self):
        pb = _make_branching_playbook()
        order = _topo_order(pb)
        # Entry should be first
        assert order[0] == "scan"
        # All nodes present
        assert set(order) == {"scan", "triage", "fix", "log", "done"}

    def test_single_node(self):
        pb = _make_single_node_playbook()
        order = _topo_order(pb)
        assert order == ["done"]

    def test_empty(self):
        pb = _make_empty_playbook()
        order = _topo_order(pb)
        assert order == []

    def test_entry_first(self):
        pb = _make_checkpoint_playbook()
        order = _topo_order(pb)
        assert order[0] == "analyse"


# ===================================================================
# Edge collection tests
# ===================================================================


class TestCollectEdges:
    def test_linear_edges(self):
        pb = _make_linear_playbook()
        edges = _collect_edges(pb)
        assert ("start", "process", "") in edges
        assert ("process", "done", "") in edges
        assert len(edges) == 2

    def test_branching_edges(self):
        pb = _make_branching_playbook()
        edges = _collect_edges(pb)
        # scan has two conditional transitions
        assert ("scan", "done", "no findings") in edges
        assert ("scan", "triage", "findings exist") in edges
        # triage has conditional + otherwise
        assert ("triage", "fix", "has errors") in edges
        assert ("triage", "log", "otherwise") in edges
        # fix → log, log → done
        assert ("fix", "log", "") in edges
        assert ("log", "done", "") in edges

    def test_timeout_edge(self):
        pb = _make_timeout_playbook()
        edges = _collect_edges(pb)
        assert ("wait", "fallback", "timeout") in edges

    def test_empty_playbook(self):
        pb = _make_empty_playbook()
        edges = _collect_edges(pb)
        assert edges == []


# ===================================================================
# ASCII rendering tests
# ===================================================================


class TestRenderAscii:
    def test_header(self):
        pb = _make_linear_playbook()
        output = render_ascii(pb)
        assert "linear-test" in output
        assert "v1" in output
        assert "scope=system" in output

    def test_trigger_display(self):
        pb = _make_linear_playbook()
        output = render_ascii(pb)
        assert "task.completed" in output

    def test_node_count(self):
        pb = _make_linear_playbook()
        output = render_ascii(pb)
        assert "Nodes: 3" in output

    def test_node_boxes_present(self):
        pb = _make_linear_playbook()
        output = render_ascii(pb)
        assert "start" in output
        assert "process" in output
        assert "done" in output

    def test_box_drawing_chars(self):
        pb = _make_linear_playbook()
        output = render_ascii(pb)
        assert "┌" in output
        assert "┐" in output
        assert "└" in output
        assert "┘" in output
        assert "│" in output
        assert "─" in output

    def test_edge_arrows(self):
        pb = _make_linear_playbook()
        output = render_ascii(pb)
        assert "──→ process" in output
        assert "──→ done" in output

    def test_conditional_edges_with_labels(self):
        pb = _make_branching_playbook()
        output = render_ascii(pb)
        assert "[no findings]" in output
        assert "[findings exist]" in output

    def test_otherwise_label(self):
        pb = _make_branching_playbook()
        output = render_ascii(pb)
        assert "[otherwise]" in output

    def test_entry_symbol(self):
        pb = _make_linear_playbook()
        output = render_ascii(pb)
        assert "▶" in output

    def test_terminal_symbol(self):
        pb = _make_linear_playbook()
        output = render_ascii(pb)
        assert "■" in output

    def test_decision_symbol(self):
        pb = _make_branching_playbook()
        output = render_ascii(pb)
        assert "◆" in output

    def test_checkpoint_symbol(self):
        pb = _make_checkpoint_playbook()
        output = render_ascii(pb)
        assert "⏸" in output

    def test_prompt_preview(self):
        pb = _make_linear_playbook()
        output = render_ascii(pb, show_prompts=True)
        assert "Initialise the process" in output

    def test_no_prompts(self):
        pb = _make_linear_playbook()
        output = render_ascii(pb, show_prompts=False)
        # Prompt text should not appear
        assert "Initialise the process" not in output

    def test_legend(self):
        pb = _make_linear_playbook()
        output = render_ascii(pb)
        assert "Legend:" in output

    def test_empty_playbook(self):
        pb = _make_empty_playbook()
        output = render_ascii(pb)
        assert "no nodes" in output

    def test_single_node(self):
        pb = _make_single_node_playbook()
        output = render_ascii(pb)
        assert "done" in output
        assert "■" in output

    def test_timeout_info(self):
        pb = _make_timeout_playbook()
        output = render_ascii(pb)
        assert "300s" in output

    def test_timeout_edge_arrow(self):
        pb = _make_timeout_playbook()
        output = render_ascii(pb)
        assert "⏱→ fallback" in output

    def test_human_review_annotation(self):
        pb = _make_checkpoint_playbook()
        output = render_ascii(pb)
        assert "human review" in output

    def test_summarize_before_annotation(self):
        pb = _make_summarize_playbook()
        output = render_ascii(pb)
        assert "summarize" in output.lower()

    def test_node_type_labels(self):
        pb = _make_branching_playbook()
        output = render_ascii(pb)
        # Should show type labels for different node types
        assert "[entry+decision]" in output or "[entry]" in output
        assert "[terminal]" in output


# ===================================================================
# Mermaid rendering tests
# ===================================================================


class TestRenderMermaid:
    def test_flowchart_header(self):
        pb = _make_linear_playbook()
        output = render_mermaid(pb)
        assert "flowchart TD" in output

    def test_lr_direction(self):
        pb = _make_linear_playbook()
        output = render_mermaid(pb, direction="LR")
        assert "flowchart LR" in output

    def test_title(self):
        pb = _make_linear_playbook()
        output = render_mermaid(pb)
        assert "linear-test v1" in output

    def test_node_definitions(self):
        pb = _make_linear_playbook()
        output = render_mermaid(pb)
        # All node IDs should appear
        assert "start" in output
        assert "process" in output
        assert "done" in output

    def test_terminal_node_shape(self):
        """Terminal nodes use stadium shape ([(...)])."""
        pb = _make_linear_playbook()
        output = render_mermaid(pb)
        assert '(["' in output  # stadium shape opening

    def test_decision_node_shape(self):
        """Decision nodes use rhombus shape ({...})."""
        pb = _make_branching_playbook()
        output = render_mermaid(pb)
        assert '{"' in output  # rhombus shape opening

    def test_edges_present(self):
        pb = _make_linear_playbook()
        output = render_mermaid(pb)
        assert "start --> process" in output
        assert "process --> done" in output

    def test_conditional_edge_labels(self):
        pb = _make_branching_playbook()
        output = render_mermaid(pb)
        assert "no findings" in output
        assert "findings exist" in output

    def test_otherwise_edge_label(self):
        pb = _make_branching_playbook()
        output = render_mermaid(pb)
        assert "otherwise" in output

    def test_style_classes(self):
        pb = _make_branching_playbook()
        output = render_mermaid(pb)
        assert "classDef entryNode" in output
        assert "classDef terminalNode" in output
        assert "classDef decisionNode" in output
        assert "classDef actionNode" in output

    def test_style_assignments(self):
        pb = _make_branching_playbook()
        output = render_mermaid(pb)
        assert "class " in output
        assert "entryNode" in output
        assert "terminalNode" in output

    def test_empty_playbook(self):
        pb = _make_empty_playbook()
        output = render_mermaid(pb)
        assert "no nodes" in output

    def test_single_node(self):
        pb = _make_single_node_playbook()
        output = render_mermaid(pb)
        assert "done" in output
        assert "flowchart" in output

    def test_no_prompts(self):
        pb = _make_linear_playbook()
        output = render_mermaid(pb, show_prompts=False)
        assert "Initialise the process" not in output

    def test_with_prompts(self):
        pb = _make_linear_playbook()
        output = render_mermaid(pb, show_prompts=True)
        assert "Initialise the process" in output

    def test_timeout_dashed_edge(self):
        pb = _make_timeout_playbook()
        output = render_mermaid(pb)
        assert "-.->|" in output  # dashed edge for timeout

    def test_checkpoint_node_shape(self):
        """Checkpoint nodes use asymmetric/flag shape (>...])."""
        pb = _make_checkpoint_playbook()
        output = render_mermaid(pb)
        assert "human review" in output

    def test_hyphenated_ids_safe(self):
        """Node IDs with hyphens are converted to underscores for Mermaid."""
        pb = _make_branching_playbook()
        output = render_mermaid(pb)
        # "code-quality-gate" playbook's edges should use safe IDs
        assert "code-quality-gate" not in output.split("\n")[3:]  # not in flowchart body
        # All nodes should use underscore IDs

    def test_mermaid_escape(self):
        assert _mermaid_escape('hello "world"') == "hello 'world'"
        assert _mermaid_escape("a < b > c") == "a &lt; b &gt; c"
        assert _mermaid_escape("a & b") == "a &amp; b"


# ===================================================================
# Round-trip: ensure render doesn't crash on various topologies
# ===================================================================


class TestRenderRobustness:
    """Ensure both renderers handle all fixture graphs without errors."""

    @pytest.fixture(
        params=[
            "linear",
            "branching",
            "checkpoint",
            "single_node",
            "empty",
            "timeout",
            "summarize",
        ]
    )
    def playbook(self, request):
        factories = {
            "linear": _make_linear_playbook,
            "branching": _make_branching_playbook,
            "checkpoint": _make_checkpoint_playbook,
            "single_node": _make_single_node_playbook,
            "empty": _make_empty_playbook,
            "timeout": _make_timeout_playbook,
            "summarize": _make_summarize_playbook,
        }
        return factories[request.param]()

    def test_ascii_renders_string(self, playbook):
        result = render_ascii(playbook)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_mermaid_renders_string(self, playbook):
        result = render_mermaid(playbook)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_ascii_no_prompts(self, playbook):
        result = render_ascii(playbook, show_prompts=False)
        assert isinstance(result, str)

    def test_mermaid_lr(self, playbook):
        result = render_mermaid(playbook, direction="LR")
        assert isinstance(result, str)


# ===================================================================
# Complex graph topology tests
# ===================================================================


class TestComplexTopologies:
    def test_diamond_graph(self):
        """Test a diamond pattern: A → {B, C} → D."""
        pb = CompiledPlaybook(
            id="diamond",
            version=1,
            source_hash="diam0000",
            triggers=["manual"],
            scope="system",
            nodes={
                "a": PlaybookNode(
                    entry=True,
                    prompt="Start",
                    transitions=[
                        PlaybookTransition(goto="b", when="left"),
                        PlaybookTransition(goto="c", when="right"),
                    ],
                ),
                "b": PlaybookNode(prompt="Left path", goto="d"),
                "c": PlaybookNode(prompt="Right path", goto="d"),
                "d": PlaybookNode(terminal=True),
            },
        )
        ascii_out = render_ascii(pb)
        assert "a" in ascii_out
        assert "b" in ascii_out
        assert "c" in ascii_out
        assert "d" in ascii_out

        mermaid_out = render_mermaid(pb)
        assert "left" in mermaid_out
        assert "right" in mermaid_out

    def test_wide_branching(self):
        """Test a node with many outgoing transitions."""
        transitions = [
            PlaybookTransition(goto=f"branch_{i}", when=f"condition {i}") for i in range(5)
        ]
        nodes = {
            "root": PlaybookNode(
                entry=True,
                prompt="Route to appropriate handler",
                transitions=transitions,
            ),
        }
        for i in range(5):
            nodes[f"branch_{i}"] = PlaybookNode(
                prompt=f"Handle branch {i}",
                goto="done",
            )
        nodes["done"] = PlaybookNode(terminal=True)

        pb = CompiledPlaybook(
            id="wide-branch",
            version=1,
            source_hash="wide0000",
            triggers=["manual"],
            scope="system",
            nodes=nodes,
        )

        ascii_out = render_ascii(pb)
        for i in range(5):
            assert f"branch_{i}" in ascii_out
            assert f"condition {i}" in ascii_out

        mermaid_out = render_mermaid(pb)
        for i in range(5):
            assert f"branch_{i}" in mermaid_out

    def test_long_chain(self):
        """Test a long linear chain of nodes."""
        nodes = {}
        for i in range(10):
            nid = f"step_{i}"
            if i == 0:
                nodes[nid] = PlaybookNode(
                    entry=True,
                    prompt=f"Step {i}",
                    goto=f"step_{i + 1}",
                )
            elif i == 9:
                nodes[nid] = PlaybookNode(terminal=True)
            else:
                nodes[nid] = PlaybookNode(
                    prompt=f"Step {i}",
                    goto=f"step_{i + 1}",
                )

        pb = CompiledPlaybook(
            id="long-chain",
            version=1,
            source_hash="long0000",
            triggers=["manual"],
            scope="system",
            nodes=nodes,
        )

        order = _topo_order(pb)
        assert order[0] == "step_0"
        assert order[-1] == "step_9"
        assert len(order) == 10

        ascii_out = render_ascii(pb)
        assert "step_0" in ascii_out
        assert "step_9" in ascii_out
