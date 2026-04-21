"""Tests for playbook graph view — dashboard visualization data layer.

Tests the structured graph view output from ``src/playbooks/graph_view``:
nodes as positioned boxes, edges as labelled arrows, live state overlays,
run path highlighting, metrics overlays, and run history timelines.

Roadmap 5.7.2, spec §14.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

import pytest

from src.playbooks.graph_view import (
    LIVE_STATE_COLORS,
    NODE_TYPE_COLORS,
    RUN_STATUS_COLORS,
    _classify_node,
    _compute_layout,
    _edge_label,
    _parse_node_trace,
    _prompt_preview,
    _run_edges,
    _run_path,
    build_edges,
    build_graph_view,
    build_live_state,
    build_node_metrics_overlay,
    build_nodes,
    build_run_history,
    build_run_overlay,
)
from src.playbooks.models import (
    CompiledPlaybook,
    PlaybookNode,
    PlaybookTransition,
)


# ---------------------------------------------------------------------------
# Test fixtures — minimal PlaybookRun stand-in
# ---------------------------------------------------------------------------


@dataclass
class FakePlaybookRun:
    """Lightweight stand-in for PlaybookRun used in graph view tests."""

    run_id: str = "run-001"
    playbook_id: str = "test-playbook"
    playbook_version: int = 1
    trigger_event: str = "{}"
    status: str = "completed"
    current_node: str | None = None
    conversation_history: str = "[]"
    node_trace: str = "[]"
    tokens_used: int = 0
    started_at: float = 0.0
    completed_at: float | None = None
    error: str | None = None
    pinned_graph: str | None = None
    paused_at: float | None = None


def _simple_playbook() -> CompiledPlaybook:
    """Create a simple linear 3-node playbook: entry → action → done."""
    return CompiledPlaybook(
        id="test-playbook",
        version=1,
        source_hash="abc123",
        triggers=["task.completed"],
        scope="system",
        nodes={
            "start": PlaybookNode(
                prompt="Check the task results",
                entry=True,
                goto="process",
            ),
            "process": PlaybookNode(
                prompt="Process the results and generate a report",
                goto="done",
            ),
            "done": PlaybookNode(
                terminal=True,
            ),
        },
    )


def _branching_playbook() -> CompiledPlaybook:
    """Create a playbook with a decision node that branches."""
    return CompiledPlaybook(
        id="branching-playbook",
        version=2,
        source_hash="def456",
        triggers=["git.push"],
        scope="project",
        nodes={
            "evaluate": PlaybookNode(
                prompt="Evaluate the code changes",
                entry=True,
                transitions=[
                    PlaybookTransition(goto="approve", when="changes look good"),
                    PlaybookTransition(goto="review", when="changes need review"),
                    PlaybookTransition(goto="reject", otherwise=True),
                ],
            ),
            "approve": PlaybookNode(
                prompt="Approve the changes",
                goto="done",
            ),
            "review": PlaybookNode(
                prompt="Request human review",
                wait_for_human=True,
                goto="done",
            ),
            "reject": PlaybookNode(
                prompt="Reject the changes",
                goto="done",
            ),
            "done": PlaybookNode(terminal=True),
        },
    )


def _complex_playbook() -> CompiledPlaybook:
    """Create a playbook with timeouts and summarize_before."""
    return CompiledPlaybook(
        id="complex-playbook",
        version=3,
        source_hash="ghi789",
        triggers=["task.completed"],
        scope="system",
        nodes={
            "start": PlaybookNode(
                prompt="Begin analysis",
                entry=True,
                timeout_seconds=300,
                on_timeout="timeout_handler",
                goto="analyze",
            ),
            "analyze": PlaybookNode(
                prompt="Deep analysis of results",
                transitions=[
                    PlaybookTransition(goto="success", when="analysis passes"),
                    PlaybookTransition(goto="failure", otherwise=True),
                ],
            ),
            "success": PlaybookNode(terminal=True),
            "failure": PlaybookNode(terminal=True),
            "timeout_handler": PlaybookNode(
                prompt="Handle timeout",
                terminal=True,
            ),
        },
    )


def _make_run(
    *,
    run_id: str = "run-001",
    playbook_id: str = "test-playbook",
    status: str = "completed",
    current_node: str | None = None,
    trace: list[dict] | None = None,
    tokens_used: int = 100,
    started_at: float | None = None,
    completed_at: float | None = None,
    error: str | None = None,
) -> FakePlaybookRun:
    """Helper to build a FakePlaybookRun with a properly serialized trace."""
    now = time.time()
    return FakePlaybookRun(
        run_id=run_id,
        playbook_id=playbook_id,
        status=status,
        current_node=current_node,
        node_trace=json.dumps(trace or []),
        tokens_used=tokens_used,
        started_at=started_at or now - 60,
        completed_at=completed_at or (now if status == "completed" else None),
        error=error,
    )


# ===========================================================================
# Node classification tests
# ===========================================================================


class TestClassifyNode:
    def test_entry_node(self):
        node = PlaybookNode(entry=True, prompt="Start here")
        assert _classify_node(node) == "entry"

    def test_entry_decision_node(self):
        node = PlaybookNode(
            entry=True,
            prompt="Decide",
            transitions=[
                PlaybookTransition(goto="a", when="yes"),
                PlaybookTransition(goto="b", otherwise=True),
            ],
        )
        assert _classify_node(node) == "entry+decision"

    def test_terminal_node(self):
        node = PlaybookNode(terminal=True)
        assert _classify_node(node) == "terminal"

    def test_checkpoint_node(self):
        node = PlaybookNode(prompt="Review", wait_for_human=True)
        assert _classify_node(node) == "checkpoint"

    def test_decision_node(self):
        node = PlaybookNode(
            prompt="Check",
            transitions=[
                PlaybookTransition(goto="a", when="yes"),
                PlaybookTransition(goto="b", otherwise=True),
            ],
        )
        assert _classify_node(node) == "decision"

    def test_action_node(self):
        node = PlaybookNode(prompt="Do something", goto="next")
        assert _classify_node(node) == "action"

    def test_terminal_takes_precedence_over_entry(self):
        """Terminal flag should take precedence (edge case)."""
        node = PlaybookNode(terminal=True, entry=True)
        assert _classify_node(node) == "terminal"


# ===========================================================================
# Edge label tests
# ===========================================================================


class TestEdgeLabel:
    def test_otherwise_transition(self):
        t = PlaybookTransition(goto="b", otherwise=True)
        assert _edge_label(t) == "otherwise"

    def test_string_condition(self):
        t = PlaybookTransition(goto="b", when="changes look good")
        assert _edge_label(t) == "changes look good"

    def test_dict_condition(self):
        t = PlaybookTransition(goto="b", when={"function": "has_output"})
        assert "has_output" in _edge_label(t)

    def test_no_condition(self):
        t = PlaybookTransition(goto="b")
        assert _edge_label(t) == ""


# ===========================================================================
# Prompt preview tests
# ===========================================================================


class TestPromptPreview:
    def test_short_prompt(self):
        node = PlaybookNode(prompt="Hello world")
        assert _prompt_preview(node) == "Hello world"

    def test_long_prompt_truncated(self):
        node = PlaybookNode(prompt="A" * 100)
        result = _prompt_preview(node, max_len=20)
        assert len(result) == 20
        assert result.endswith("...")

    def test_multiline_takes_first(self):
        node = PlaybookNode(prompt="First line\nSecond line\nThird line")
        assert _prompt_preview(node) == "First line"

    def test_empty_prompt(self):
        node = PlaybookNode(prompt="")
        assert _prompt_preview(node) == ""


# ===========================================================================
# Layout computation tests
# ===========================================================================


class TestComputeLayout:
    def test_simple_linear_layout(self):
        pb = _simple_playbook()
        positions = _compute_layout(pb, "TD")

        # Entry should be at the top (y=0)
        assert positions["start"]["y"] == 0
        assert positions["process"]["y"] == 1
        assert positions["done"]["y"] == 2

    def test_branching_layout(self):
        pb = _branching_playbook()
        positions = _compute_layout(pb, "TD")

        # Entry at depth 0
        assert positions["evaluate"]["y"] == 0

        # All branches at depth 1
        assert positions["approve"]["y"] == 1
        assert positions["review"]["y"] == 1
        assert positions["reject"]["y"] == 1

        # All branches have different x positions
        xs = {positions["approve"]["x"], positions["review"]["x"], positions["reject"]["x"]}
        assert len(xs) == 3  # all unique

        # Terminal at depth 2
        assert positions["done"]["y"] == 2

    def test_lr_direction(self):
        pb = _simple_playbook()
        positions = _compute_layout(pb, "LR")

        # In LR mode, x is depth, y is column
        assert positions["start"]["x"] == 0
        assert positions["process"]["x"] == 1
        assert positions["done"]["x"] == 2

    def test_empty_playbook(self):
        pb = CompiledPlaybook(
            id="empty", version=1, source_hash="x",
            triggers=["test"], scope="system", nodes={},
        )
        positions = _compute_layout(pb)
        assert positions == {}


# ===========================================================================
# Node trace parsing tests
# ===========================================================================


class TestParseNodeTrace:
    def test_json_string(self):
        run = _make_run(trace=[{"node_id": "a"}, {"node_id": "b"}])
        result = _parse_node_trace(run)
        assert len(result) == 2
        assert result[0]["node_id"] == "a"

    def test_empty_string(self):
        run = FakePlaybookRun(node_trace="")
        assert _parse_node_trace(run) == []

    def test_invalid_json(self):
        run = FakePlaybookRun(node_trace="not json")
        assert _parse_node_trace(run) == []

    def test_empty_list(self):
        run = _make_run(trace=[])
        assert _parse_node_trace(run) == []


class TestRunPath:
    def test_extracts_node_ids(self):
        run = _make_run(trace=[
            {"node_id": "start", "status": "completed"},
            {"node_id": "process", "status": "completed"},
            {"node_id": "done", "status": "completed"},
        ])
        assert _run_path(run) == ["start", "process", "done"]

    def test_empty_trace(self):
        run = _make_run(trace=[])
        assert _run_path(run) == []


class TestRunEdges:
    def test_extracts_edges(self):
        run = _make_run(trace=[
            {"node_id": "start"},
            {"node_id": "process"},
            {"node_id": "done"},
        ])
        edges = _run_edges(run)
        assert ("start", "process") in edges
        assert ("process", "done") in edges
        assert len(edges) == 2

    def test_single_node(self):
        run = _make_run(trace=[{"node_id": "start"}])
        assert _run_edges(run) == set()


# ===========================================================================
# Build nodes tests
# ===========================================================================


class TestBuildNodes:
    def test_simple_playbook_nodes(self):
        pb = _simple_playbook()
        positions = _compute_layout(pb)
        nodes = build_nodes(pb, positions)

        assert len(nodes) == 3

        # Check start node
        start = next(n for n in nodes if n["id"] == "start")
        assert start["type"] == "entry"
        assert start["entry"] is True
        assert start["terminal"] is False
        assert start["symbol"] == "▶"
        assert "prompt_preview" in start
        assert start["colors"] == NODE_TYPE_COLORS["entry"]
        assert "position" in start

    def test_terminal_node_has_no_prompt(self):
        pb = _simple_playbook()
        positions = _compute_layout(pb)
        nodes = build_nodes(pb, positions, show_prompts=True)

        done = next(n for n in nodes if n["id"] == "done")
        assert done["terminal"] is True
        assert "prompt_preview" not in done

    def test_decision_node(self):
        pb = _branching_playbook()
        positions = _compute_layout(pb)
        nodes = build_nodes(pb, positions)

        evaluate = next(n for n in nodes if n["id"] == "evaluate")
        assert evaluate["type"] == "entry+decision"
        assert evaluate["out_degree"] == 3  # 3 transitions

    def test_checkpoint_node(self):
        pb = _branching_playbook()
        positions = _compute_layout(pb)
        nodes = build_nodes(pb, positions)

        review = next(n for n in nodes if n["id"] == "review")
        assert review["type"] == "checkpoint"
        assert review["wait_for_human"] is True

    def test_hide_prompts(self):
        pb = _simple_playbook()
        positions = _compute_layout(pb)
        nodes = build_nodes(pb, positions, show_prompts=False)

        start = next(n for n in nodes if n["id"] == "start")
        assert "prompt_preview" not in start

    def test_timeout_metadata(self):
        pb = _complex_playbook()
        positions = _compute_layout(pb)
        nodes = build_nodes(pb, positions)

        start = next(n for n in nodes if n["id"] == "start")
        assert start["timeout_seconds"] == 300
        assert start["on_timeout"] == "timeout_handler"

# ===========================================================================
# Build edges tests
# ===========================================================================


class TestBuildEdges:
    def test_simple_goto_edges(self):
        pb = _simple_playbook()
        edges = build_edges(pb)

        assert len(edges) == 2
        edge1 = edges[0]
        assert edge1["source"] == "start"
        assert edge1["target"] == "process"
        assert edge1["edge_type"] == "goto"
        assert edge1["label"] == ""

    def test_conditional_edges(self):
        pb = _branching_playbook()
        edges = build_edges(pb)

        # Should have: 3 transitions from evaluate + 3 gotos (approve→done, review→done, reject→done)
        assert len(edges) == 6

        # Check condition labels
        condition_edges = [e for e in edges if e["edge_type"] == "condition"]
        assert len(condition_edges) == 2  # "changes look good", "changes need review"

        otherwise_edges = [e for e in edges if e["edge_type"] == "otherwise"]
        assert len(otherwise_edges) == 1
        assert otherwise_edges[0]["label"] == "otherwise"

    def test_timeout_edge(self):
        pb = _complex_playbook()
        edges = build_edges(pb)

        timeout_edges = [e for e in edges if e["edge_type"] == "timeout"]
        assert len(timeout_edges) == 1
        assert timeout_edges[0]["source"] == "start"
        assert timeout_edges[0]["target"] == "timeout_handler"
        assert timeout_edges[0]["label"] == "timeout"


# ===========================================================================
# Live state overlay tests
# ===========================================================================


class TestBuildLiveState:
    def test_no_active_runs(self):
        pb = _simple_playbook()
        result = build_live_state(pb, [])
        assert result["instances"] == []
        assert result["node_states"] == {}

    def test_running_instance(self):
        pb = _simple_playbook()
        run = _make_run(
            status="running",
            current_node="process",
            trace=[
                {"node_id": "start", "status": "completed"},
            ],
        )
        result = build_live_state(pb, [run])

        assert len(result["instances"]) == 1
        inst = result["instances"][0]
        assert inst["run_id"] == "run-001"
        assert inst["status"] == "running"
        assert inst["current_node"] == "process"

        # Start should be marked completed
        assert "start" in result["node_states"]

        # Process (current node) should be marked active
        assert "process" in result["node_states"]
        assert result["node_states"]["process"]["status"] == "active"
        assert result["node_states"]["process"]["highlight"] == LIVE_STATE_COLORS["active"]

    def test_paused_instance(self):
        pb = _simple_playbook()
        run = _make_run(
            status="paused",
            current_node="process",
            trace=[
                {"node_id": "start", "status": "completed"},
                {"node_id": "process", "status": "paused"},
            ],
        )
        result = build_live_state(pb, [run])

        assert result["node_states"]["process"]["status"] == "paused"
        assert result["node_states"]["process"]["highlight"] == LIVE_STATE_COLORS["paused"]

    def test_different_playbook_id_filtered(self):
        pb = _simple_playbook()
        run = _make_run(playbook_id="other-playbook", status="running")
        result = build_live_state(pb, [run])
        assert result["instances"] == []


# ===========================================================================
# Run overlay tests
# ===========================================================================


class TestBuildRunOverlay:
    def test_basic_run_overlay(self):
        pb = _simple_playbook()
        now = time.time()
        run = _make_run(
            trace=[
                {
                    "node_id": "start",
                    "status": "completed",
                    "started_at": now - 30,
                    "completed_at": now - 20,
                    "transition_to": "process",
                    "transition_method": "goto",
                    "tokens_used": 50,
                },
                {
                    "node_id": "process",
                    "status": "completed",
                    "started_at": now - 20,
                    "completed_at": now - 5,
                    "transition_to": "done",
                    "transition_method": "goto",
                    "tokens_used": 80,
                },
                {
                    "node_id": "done",
                    "status": "completed",
                    "started_at": now - 5,
                    "completed_at": now,
                },
            ],
            tokens_used=130,
            started_at=now - 30,
            completed_at=now,
        )

        result = build_run_overlay(pb, run)

        assert result["run_id"] == "run-001"
        assert result["status"] == "completed"
        assert result["path"] == ["start", "process", "done"]
        assert len(result["highlighted_edges"]) == 2

        # Check node details
        start_detail = result["node_details"]["start"]
        assert start_detail["visited"] is True
        assert start_detail["order"] == 0
        assert start_detail["duration_seconds"] == pytest.approx(10.0, abs=0.1)
        assert start_detail["transition_to"] == "process"
        assert start_detail["tokens_used"] == 50

    def test_failed_node_highlight(self):
        pb = _simple_playbook()
        run = _make_run(
            status="failed",
            trace=[
                {"node_id": "start", "status": "completed"},
                {"node_id": "process", "status": "failed"},
            ],
            error="LLM call failed",
        )

        result = build_run_overlay(pb, run)
        assert result["error"] == "LLM call failed"
        assert result["node_details"]["process"]["highlight"] == LIVE_STATE_COLORS["failed"]

    def test_empty_trace(self):
        pb = _simple_playbook()
        run = _make_run(trace=[])
        result = build_run_overlay(pb, run)
        assert result["path"] == []
        assert result["node_details"] == {}
        assert result["highlighted_edges"] == []


# ===========================================================================
# Run history tests
# ===========================================================================


class TestBuildRunHistory:
    def test_basic_history(self):
        now = time.time()
        runs = [
            _make_run(
                run_id="run-1",
                status="completed",
                started_at=now - 120,
                completed_at=now - 60,
                trace=[
                    {"node_id": "start", "status": "completed"},
                    {"node_id": "done", "status": "completed"},
                ],
            ),
            _make_run(
                run_id="run-2",
                status="failed",
                started_at=now - 60,
                completed_at=now,
                trace=[
                    {"node_id": "start", "status": "completed"},
                    {"node_id": "process", "status": "failed"},
                ],
                error="Something went wrong",
            ),
        ]

        history = build_run_history(runs)

        # Most recent first
        assert len(history) == 2
        assert history[0]["run_id"] == "run-2"
        assert history[1]["run_id"] == "run-1"

    def test_history_limit(self):
        runs = [
            _make_run(run_id=f"run-{i}", started_at=time.time() - i)
            for i in range(30)
        ]
        history = build_run_history(runs, limit=10)
        assert len(history) == 10

    def test_status_colors(self):
        run = _make_run(status="completed")
        history = build_run_history([run])
        assert history[0]["status_color"] == RUN_STATUS_COLORS["completed"]

    def test_duration_calculation(self):
        now = time.time()
        run = _make_run(started_at=now - 30, completed_at=now)
        history = build_run_history([run])
        assert history[0]["duration_seconds"] == pytest.approx(30.0, abs=0.1)

    def test_error_truncation(self):
        run = _make_run(error="X" * 500)
        history = build_run_history([run])
        assert len(history[0]["error"]) == 200

    def test_node_status_counts(self):
        run = _make_run(trace=[
            {"node_id": "start", "status": "completed"},
            {"node_id": "process", "status": "completed"},
            {"node_id": "check", "status": "failed"},
        ])
        history = build_run_history([run])
        assert history[0]["node_statuses"]["completed"] == 2
        assert history[0]["node_statuses"]["failed"] == 1


# ===========================================================================
# Node metrics overlay tests
# ===========================================================================


class TestBuildNodeMetricsOverlay:
    def test_basic_metrics(self):
        metrics = {
            "start": {
                "execution_count": 10,
                "failure_rate": 0.0,
                "avg_duration_seconds": 5.2,
                "p95_duration_seconds": 8.1,
                "avg_tokens": 120.5,
            },
            "process": {
                "execution_count": 8,
                "failure_rate": 0.25,
                "avg_duration_seconds": 12.3,
                "p95_duration_seconds": 20.1,
                "avg_tokens": 300.0,
            },
        }

        overlay = build_node_metrics_overlay(metrics)
        assert "start" in overlay
        assert "process" in overlay
        assert overlay["start"]["execution_count"] == 10
        assert overlay["start"]["heat_color"] == "#4CAF50"  # green (0% failure)
        assert overlay["process"]["heat_color"] == "#FF9800"  # orange (25% failure, >= 0.25)

    def test_high_failure_rate_is_red(self):
        metrics = {
            "bad_node": {
                "execution_count": 10,
                "failure_rate": 0.8,
                "avg_duration_seconds": 1.0,
                "p95_duration_seconds": 2.0,
                "avg_tokens": 50.0,
            },
        }
        overlay = build_node_metrics_overlay(metrics)
        assert overlay["bad_node"]["heat_color"] == "#F44336"  # red

    def test_none_metrics(self):
        assert build_node_metrics_overlay(None) == {}

    def test_empty_metrics(self):
        assert build_node_metrics_overlay({}) == {}

    def test_failure_rate_thresholds(self):
        """Test all failure rate color thresholds."""
        cases = [
            (0.0, "#4CAF50"),    # green
            (0.05, "#8BC34A"),   # light green
            (0.15, "#FFC107"),   # amber
            (0.35, "#FF9800"),   # orange
            (0.6, "#F44336"),    # red
        ]
        for rate, expected_color in cases:
            metrics = {
                "node": {
                    "execution_count": 100,
                    "failure_rate": rate,
                    "avg_duration_seconds": 1.0,
                    "p95_duration_seconds": 2.0,
                    "avg_tokens": 50.0,
                },
            }
            overlay = build_node_metrics_overlay(metrics)
            assert overlay["node"]["heat_color"] == expected_color, (
                f"Expected {expected_color} for failure_rate={rate}, "
                f"got {overlay['node']['heat_color']}"
            )


# ===========================================================================
# Full graph view integration tests
# ===========================================================================


class TestBuildGraphView:
    def test_simple_playbook_view(self):
        pb = _simple_playbook()
        view = build_graph_view(pb)

        # Top-level structure
        assert "playbook" in view
        assert "graph" in view
        assert "layout" in view
        assert "legend" in view

        # Playbook metadata
        assert view["playbook"]["id"] == "test-playbook"
        assert view["playbook"]["version"] == 1
        assert view["playbook"]["scope"] == "system"
        assert view["playbook"]["node_count"] == 3

        # Graph
        assert len(view["graph"]["nodes"]) == 3
        assert len(view["graph"]["edges"]) == 2

        # Layout
        assert view["layout"]["direction"] == "TD"
        assert "grid_positions" in view["layout"]

    def test_empty_playbook(self):
        pb = CompiledPlaybook(
            id="empty", version=1, source_hash="x",
            triggers=["test"], scope="system", nodes={},
        )
        view = build_graph_view(pb)
        assert view["graph"]["nodes"] == []
        assert view["graph"]["edges"] == []
        assert "legend" in view

    def test_lr_direction(self):
        pb = _simple_playbook()
        view = build_graph_view(pb, direction="LR")
        assert view["layout"]["direction"] == "LR"

    def test_with_active_runs(self):
        pb = _simple_playbook()
        run = _make_run(
            status="running",
            current_node="process",
            trace=[{"node_id": "start", "status": "completed"}],
        )
        view = build_graph_view(pb, active_runs=[run])

        assert "live_state" in view
        assert len(view["live_state"]["instances"]) == 1

    def test_with_run_overlay(self):
        pb = _simple_playbook()
        run = _make_run(
            trace=[
                {"node_id": "start", "status": "completed"},
                {"node_id": "process", "status": "completed"},
                {"node_id": "done", "status": "completed"},
            ],
        )
        view = build_graph_view(pb, run_overlay=run)

        assert "run_overlay" in view
        assert view["run_overlay"]["path"] == ["start", "process", "done"]

    def test_with_all_runs_history(self):
        pb = _simple_playbook()
        runs = [
            _make_run(run_id=f"run-{i}", started_at=time.time() - i)
            for i in range(5)
        ]
        view = build_graph_view(pb, all_runs=runs)

        assert "run_history" in view
        assert len(view["run_history"]) == 5

    def test_with_node_metrics(self):
        pb = _simple_playbook()
        metrics = {
            "start": {
                "execution_count": 10,
                "failure_rate": 0.0,
                "avg_duration_seconds": 5.0,
                "p95_duration_seconds": 8.0,
                "avg_tokens": 100.0,
            },
        }
        view = build_graph_view(pb, node_metrics=metrics)

        assert "node_metrics" in view
        assert "start" in view["node_metrics"]

    def test_no_optional_overlays(self):
        pb = _simple_playbook()
        view = build_graph_view(pb)

        assert "live_state" not in view
        assert "run_overlay" not in view
        assert "run_history" not in view
        assert "node_metrics" not in view

    def test_branching_playbook_view(self):
        pb = _branching_playbook()
        view = build_graph_view(pb)

        assert view["playbook"]["node_count"] == 5
        # 3 transitions + 3 gotos = 6 edges
        assert len(view["graph"]["edges"]) == 6

        # Verify node types
        nodes_by_id = {n["id"]: n for n in view["graph"]["nodes"]}
        assert nodes_by_id["evaluate"]["type"] == "entry+decision"
        assert nodes_by_id["review"]["type"] == "checkpoint"
        assert nodes_by_id["done"]["type"] == "terminal"

    def test_triggers_in_view(self):
        pb = _simple_playbook()
        view = build_graph_view(pb)

        assert len(view["playbook"]["triggers"]) == 1
        assert view["playbook"]["triggers"][0]["event_type"] == "task.completed"


# ===========================================================================
# Legend tests
# ===========================================================================


class TestLegend:
    def test_legend_structure(self):
        pb = _simple_playbook()
        view = build_graph_view(pb)
        legend = view["legend"]

        assert "node_types" in legend
        assert "live_states" in legend
        assert "run_statuses" in legend

    def test_all_node_types_in_legend(self):
        pb = _simple_playbook()
        view = build_graph_view(pb)
        legend = view["legend"]

        for ntype in NODE_TYPE_COLORS:
            assert ntype in legend["node_types"]
            assert "symbol" in legend["node_types"][ntype]
            assert "colors" in legend["node_types"][ntype]
            assert "label" in legend["node_types"][ntype]

    def test_all_live_states_in_legend(self):
        pb = _simple_playbook()
        view = build_graph_view(pb)
        legend = view["legend"]

        for state in LIVE_STATE_COLORS:
            assert state in legend["live_states"]

    def test_all_run_statuses_in_legend(self):
        pb = _simple_playbook()
        view = build_graph_view(pb)
        legend = view["legend"]

        for status in RUN_STATUS_COLORS:
            assert status in legend["run_statuses"]


# ===========================================================================
# Full integration: combined overlays
# ===========================================================================


class TestCombinedOverlays:
    def test_all_overlays_together(self):
        pb = _branching_playbook()
        now = time.time()

        active_run = _make_run(
            run_id="active-1",
            playbook_id="branching-playbook",
            status="running",
            current_node="review",
            trace=[
                {"node_id": "evaluate", "status": "completed"},
            ],
        )

        completed_run = _make_run(
            run_id="completed-1",
            playbook_id="branching-playbook",
            status="completed",
            started_at=now - 120,
            completed_at=now - 60,
            trace=[
                {"node_id": "evaluate", "status": "completed",
                 "started_at": now - 120, "completed_at": now - 100},
                {"node_id": "approve", "status": "completed",
                 "started_at": now - 100, "completed_at": now - 60},
                {"node_id": "done", "status": "completed",
                 "started_at": now - 60, "completed_at": now - 60},
            ],
        )

        metrics = {
            "evaluate": {
                "execution_count": 20,
                "failure_rate": 0.05,
                "avg_duration_seconds": 15.0,
                "p95_duration_seconds": 25.0,
                "avg_tokens": 200.0,
            },
            "approve": {
                "execution_count": 12,
                "failure_rate": 0.0,
                "avg_duration_seconds": 8.0,
                "p95_duration_seconds": 12.0,
                "avg_tokens": 150.0,
            },
        }

        view = build_graph_view(
            pb,
            active_runs=[active_run],
            run_overlay=completed_run,
            all_runs=[active_run, completed_run],
            node_metrics=metrics,
        )

        # All sections present
        assert "live_state" in view
        assert "run_overlay" in view
        assert "run_history" in view
        assert "node_metrics" in view

        # Live state shows the active instance
        assert len(view["live_state"]["instances"]) == 1
        assert view["live_state"]["instances"][0]["current_node"] == "review"

        # Run overlay shows the completed run's path
        assert view["run_overlay"]["path"] == ["evaluate", "approve", "done"]

        # History has both runs
        assert len(view["run_history"]) == 2

        # Metrics present for evaluate and approve
        assert "evaluate" in view["node_metrics"]
        assert "approve" in view["node_metrics"]
