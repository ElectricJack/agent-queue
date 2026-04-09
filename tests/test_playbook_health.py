"""Tests for playbook health metrics (roadmap 5.7.1).

Covers:
- Per-node metrics: duration, tokens, failure rates
- Run duration statistics: avg, p50, p95
- Transition path analysis: path frequency, edge counts, method distribution
- Failure analysis: by status, by node, common errors
- Composite health report
- Edge cases: empty runs, missing data, single-run scenarios
"""

import json

from src.models import PlaybookRun
from src.playbook_health import (
    _percentile,
    compute_duration_metrics,
    compute_failure_analysis,
    compute_node_metrics,
    compute_playbook_health,
    compute_token_metrics,
    compute_transition_paths,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_run(
    run_id: str = "run-1",
    playbook_id: str = "pb-test",
    status: str = "completed",
    node_trace: list | None = None,
    tokens_used: int = 100,
    started_at: float = 1000.0,
    completed_at: float | None = 1010.0,
    error: str | None = None,
) -> PlaybookRun:
    """Build a PlaybookRun with sensible defaults for health metric tests."""
    return PlaybookRun(
        run_id=run_id,
        playbook_id=playbook_id,
        playbook_version=1,
        trigger_event=json.dumps({"type": "test"}),
        status=status,
        current_node=None,
        conversation_history="[]",
        node_trace=json.dumps(node_trace or []),
        tokens_used=tokens_used,
        started_at=started_at,
        completed_at=completed_at,
        error=error,
    )


def _trace_entry(
    node_id: str,
    started_at: float,
    completed_at: float | None = None,
    status: str = "completed",
    transition_to: str | None = None,
    transition_method: str | None = None,
    tokens_used: int = 0,
) -> dict:
    """Build a node trace entry dict."""
    d = {
        "node_id": node_id,
        "started_at": started_at,
        "completed_at": completed_at,
        "status": status,
    }
    if transition_to is not None:
        d["transition_to"] = transition_to
    if transition_method is not None:
        d["transition_method"] = transition_method
    if tokens_used:
        d["tokens_used"] = tokens_used
    return d


# ---------------------------------------------------------------------------
# Percentile helper tests
# ---------------------------------------------------------------------------


class TestPercentile:
    def test_empty_list(self):
        assert _percentile([], 50) == 0.0

    def test_single_value(self):
        assert _percentile([5.0], 50) == 5.0
        assert _percentile([5.0], 95) == 5.0

    def test_median_odd(self):
        assert _percentile([1.0, 2.0, 3.0], 50) == 2.0

    def test_median_even(self):
        # p50 of [1, 2, 3, 4] => index 1.5 => interpolate between 2 and 3 => 2.5
        assert _percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.5

    def test_p95(self):
        values = sorted(float(i) for i in range(1, 101))  # 1..100
        p95 = _percentile(values, 95)
        # p95 of [1..100] => index 94.05 => ~95.05
        assert 94.0 < p95 < 96.0

    def test_p0_and_p100(self):
        values = [1.0, 5.0, 10.0]
        assert _percentile(values, 0) == 1.0
        assert _percentile(values, 100) == 10.0


# ---------------------------------------------------------------------------
# Per-node metrics
# ---------------------------------------------------------------------------


class TestComputeNodeMetrics:
    def test_empty_runs(self):
        assert compute_node_metrics([]) == {}

    def test_single_run_single_node(self):
        trace = [
            _trace_entry("scan", 1000.0, 1005.0, "completed", tokens_used=50),
        ]
        runs = [_make_run(node_trace=trace)]
        result = compute_node_metrics(runs)

        assert "scan" in result
        scan = result["scan"]
        assert scan["execution_count"] == 1
        assert scan["success_count"] == 1
        assert scan["failure_count"] == 0
        assert scan["failure_rate"] == 0.0
        assert scan["avg_duration_seconds"] == 5.0
        assert scan["p50_duration_seconds"] == 5.0
        assert scan["avg_tokens"] == 50.0
        assert scan["total_tokens"] == 50

    def test_multiple_runs_same_node(self):
        """Same node visited across multiple runs aggregates correctly."""
        runs = [
            _make_run(
                run_id="r1",
                node_trace=[
                    _trace_entry("scan", 1000.0, 1002.0, "completed", tokens_used=40),
                ],
            ),
            _make_run(
                run_id="r2",
                node_trace=[
                    _trace_entry("scan", 2000.0, 2008.0, "completed", tokens_used=60),
                ],
            ),
            _make_run(
                run_id="r3",
                node_trace=[
                    _trace_entry("scan", 3000.0, 3004.0, "failed", tokens_used=20),
                ],
            ),
        ]
        result = compute_node_metrics(runs)

        scan = result["scan"]
        assert scan["execution_count"] == 3
        assert scan["success_count"] == 2
        assert scan["failure_count"] == 1
        assert scan["failure_rate"] == round(1 / 3, 4)
        # avg duration: (2 + 8 + 4) / 3 = 4.667
        assert abs(scan["avg_duration_seconds"] - 4.667) < 0.01
        # avg tokens: (40 + 60 + 20) / 3 = 40
        assert scan["avg_tokens"] == 40.0
        assert scan["total_tokens"] == 120

    def test_multi_node_trace(self):
        """Multiple nodes in a single run are tracked separately."""
        trace = [
            _trace_entry("scan", 1000.0, 1003.0, "completed", "triage", "goto", 30),
            _trace_entry("triage", 1003.0, 1008.0, "completed", "fix", "llm", 50),
            _trace_entry("fix", 1008.0, 1020.0, "completed", tokens_used=120),
        ]
        runs = [_make_run(node_trace=trace)]
        result = compute_node_metrics(runs)

        assert set(result.keys()) == {"scan", "triage", "fix"}
        assert result["scan"]["avg_duration_seconds"] == 3.0
        assert result["triage"]["avg_duration_seconds"] == 5.0
        assert result["fix"]["avg_duration_seconds"] == 12.0
        assert result["fix"]["total_tokens"] == 120

    def test_missing_timing_data(self):
        """Nodes with missing completed_at are counted but have no duration."""
        trace = [
            _trace_entry("scan", 1000.0, None, "running"),
        ]
        runs = [_make_run(node_trace=trace)]
        result = compute_node_metrics(runs)

        assert result["scan"]["execution_count"] == 1
        assert result["scan"]["avg_duration_seconds"] == 0.0

    def test_no_tokens_field_graceful(self):
        """Pre-5.7.1 traces without tokens_used are handled gracefully."""
        trace = [
            {"node_id": "scan", "started_at": 1000, "completed_at": 1005, "status": "completed"}
        ]
        runs = [_make_run(node_trace=trace)]
        result = compute_node_metrics(runs)

        assert result["scan"]["total_tokens"] == 0
        assert result["scan"]["avg_tokens"] == 0.0

    def test_percentiles_across_many_runs(self):
        """p50 and p95 are computed from the distribution of durations."""
        runs = []
        for i in range(100):
            dur = float(i + 1)  # durations 1..100
            trace = [_trace_entry("scan", 1000.0, 1000.0 + dur, "completed")]
            runs.append(_make_run(run_id=f"r{i}", node_trace=trace))

        result = compute_node_metrics(runs)
        # p50 should be around 50.5
        assert 49.0 < result["scan"]["p50_duration_seconds"] < 52.0
        # p95 should be around 95.5
        assert 94.0 < result["scan"]["p95_duration_seconds"] < 97.0


# ---------------------------------------------------------------------------
# Transition paths
# ---------------------------------------------------------------------------


class TestComputeTransitionPaths:
    def test_empty_runs(self):
        result = compute_transition_paths([])
        assert result["paths"] == []
        assert result["unique_path_count"] == 0

    def test_single_path(self):
        trace = [
            _trace_entry("scan", 1000, 1005, "completed", "triage", "goto"),
            _trace_entry("triage", 1005, 1010, "completed", "fix", "llm"),
            _trace_entry("fix", 1010, 1015, "completed"),
        ]
        runs = [_make_run(node_trace=trace)]
        result = compute_transition_paths(runs)

        assert result["unique_path_count"] == 1
        assert result["paths"][0]["path"] == ["scan", "triage", "fix"]
        assert result["paths"][0]["count"] == 1
        assert result["transitions"]["scan -> triage"] == 1
        assert result["transitions"]["triage -> fix"] == 1
        assert result["transition_methods"]["goto"] == 1
        assert result["transition_methods"]["llm"] == 1

    def test_multiple_runs_same_path(self):
        trace = [
            _trace_entry("scan", 1000, 1005, "completed", "done", "goto"),
        ]
        runs = [
            _make_run(run_id="r1", node_trace=trace),
            _make_run(run_id="r2", node_trace=trace),
            _make_run(run_id="r3", node_trace=trace),
        ]
        result = compute_transition_paths(runs)

        assert result["unique_path_count"] == 1
        assert result["paths"][0]["count"] == 3

    def test_divergent_paths(self):
        """Two different paths through the graph are tracked separately."""
        path_a = [
            _trace_entry("entry", 1000, 1001, "completed", "A", "goto"),
            _trace_entry("A", 1001, 1002, "completed"),
        ]
        path_b = [
            _trace_entry("entry", 1000, 1001, "completed", "B", "llm"),
            _trace_entry("B", 1001, 1002, "completed"),
        ]
        runs = [
            _make_run(run_id="r1", node_trace=path_a),
            _make_run(run_id="r2", node_trace=path_b),
            _make_run(run_id="r3", node_trace=path_a),
        ]
        result = compute_transition_paths(runs)

        assert result["unique_path_count"] == 2
        # Path A is more common
        assert result["paths"][0]["path"] == ["entry", "A"]
        assert result["paths"][0]["count"] == 2
        assert result["paths"][1]["path"] == ["entry", "B"]
        assert result["paths"][1]["count"] == 1

    def test_transition_method_counts(self):
        trace = [
            _trace_entry("a", 0, 1, "completed", "b", "goto"),
            _trace_entry("b", 1, 2, "completed", "c", "llm"),
            _trace_entry("c", 2, 3, "completed", "d", "structured"),
            _trace_entry("d", 3, 4, "completed", "e", "otherwise"),
        ]
        runs = [_make_run(node_trace=trace)]
        result = compute_transition_paths(runs)
        assert result["transition_methods"] == {
            "goto": 1,
            "llm": 1,
            "structured": 1,
            "otherwise": 1,
        }


# ---------------------------------------------------------------------------
# Failure analysis
# ---------------------------------------------------------------------------


class TestComputeFailureAnalysis:
    def test_no_runs(self):
        result = compute_failure_analysis([])
        assert result["total_runs"] == 0
        assert result["failure_rate"] == 0.0

    def test_all_completed(self):
        runs = [
            _make_run(run_id="r1", status="completed"),
            _make_run(run_id="r2", status="completed"),
        ]
        result = compute_failure_analysis(runs)

        assert result["total_runs"] == 2
        assert result["failed_runs"] == 0
        assert result["failure_rate"] == 0.0
        assert result["by_status"] == {"completed": 2}

    def test_mixed_statuses(self):
        runs = [
            _make_run(run_id="r1", status="completed"),
            _make_run(run_id="r2", status="failed", error="Node 'fix' failed: timeout"),
            _make_run(run_id="r3", status="timed_out", error="Pause timeout exceeded (3600s)"),
            _make_run(run_id="r4", status="completed"),
        ]
        result = compute_failure_analysis(runs)

        assert result["total_runs"] == 4
        assert result["failed_runs"] == 2
        assert result["failure_rate"] == 0.5

    def test_failure_by_node(self):
        """Failed nodes are tracked by node_id."""
        trace_fail_at_fix = [
            _trace_entry("scan", 0, 1, "completed"),
            _trace_entry("fix", 1, 2, "failed"),
        ]
        trace_fail_at_scan = [
            _trace_entry("scan", 0, 1, "failed"),
        ]
        runs = [
            _make_run(run_id="r1", status="failed", node_trace=trace_fail_at_fix),
            _make_run(run_id="r2", status="failed", node_trace=trace_fail_at_fix),
            _make_run(run_id="r3", status="failed", node_trace=trace_fail_at_scan),
        ]
        result = compute_failure_analysis(runs)

        assert result["failures_by_node"]["fix"] == 2
        assert result["failures_by_node"]["scan"] == 1

    def test_common_errors(self):
        """Error messages are grouped by prefix for pattern detection."""
        runs = [
            _make_run(run_id="r1", status="failed", error="token_budget_exceeded: limit 5000"),
            _make_run(run_id="r2", status="failed", error="token_budget_exceeded: limit 3000"),
            _make_run(run_id="r3", status="failed", error="Node 'scan' failed: ConnectionError"),
        ]
        result = compute_failure_analysis(runs)

        assert result["common_errors"]["token_budget_exceeded"] == 2
        assert result["common_errors"]["Node 'scan' failed"] == 1


# ---------------------------------------------------------------------------
# Duration metrics
# ---------------------------------------------------------------------------


class TestComputeDurationMetrics:
    def test_no_runs(self):
        result = compute_duration_metrics([])
        assert result["count"] == 0
        assert result["avg_seconds"] == 0.0

    def test_single_run(self):
        runs = [_make_run(started_at=1000.0, completed_at=1010.0)]
        result = compute_duration_metrics(runs)

        assert result["count"] == 1
        assert result["avg_seconds"] == 10.0
        assert result["p50_seconds"] == 10.0
        assert result["max_seconds"] == 10.0

    def test_multiple_runs(self):
        runs = [
            _make_run(run_id="r1", started_at=1000, completed_at=1005),  # 5s
            _make_run(run_id="r2", started_at=2000, completed_at=2015),  # 15s
            _make_run(run_id="r3", started_at=3000, completed_at=3010),  # 10s
        ]
        result = compute_duration_metrics(runs)

        assert result["count"] == 3
        assert result["avg_seconds"] == 10.0
        assert result["max_seconds"] == 15.0
        # p50 of [5, 10, 15] = 10
        assert result["p50_seconds"] == 10.0

    def test_running_runs_excluded(self):
        """Runs without completed_at are not included in duration stats."""
        runs = [
            _make_run(run_id="r1", started_at=1000, completed_at=1005),
            _make_run(run_id="r2", started_at=2000, completed_at=None, status="running"),
        ]
        result = compute_duration_metrics(runs)
        assert result["count"] == 1


# ---------------------------------------------------------------------------
# Token metrics
# ---------------------------------------------------------------------------


class TestComputeTokenMetrics:
    def test_no_runs(self):
        result = compute_token_metrics([])
        assert result["total_tokens"] == 0

    def test_single_run(self):
        runs = [_make_run(tokens_used=500, started_at=1000, completed_at=1010)]
        result = compute_token_metrics(runs)

        assert result["total_tokens"] == 500
        assert result["avg_tokens"] == 500.0
        assert result["max_tokens"] == 500
        assert result["tokens_per_second"] == 50.0

    def test_multiple_runs(self):
        runs = [
            _make_run(run_id="r1", tokens_used=100, started_at=1000, completed_at=1010),
            _make_run(run_id="r2", tokens_used=300, started_at=2000, completed_at=2030),
            _make_run(run_id="r3", tokens_used=200, started_at=3000, completed_at=3020),
        ]
        result = compute_token_metrics(runs)

        assert result["total_tokens"] == 600
        assert result["avg_tokens"] == 200.0
        assert result["max_tokens"] == 300
        # tokens_per_second: 600 / 60 = 10.0
        assert result["tokens_per_second"] == 10.0

    def test_runs_without_timing(self):
        """Runs without completed_at are included in totals but not in rate."""
        runs = [
            _make_run(run_id="r1", tokens_used=100, started_at=1000, completed_at=1010),
            _make_run(run_id="r2", tokens_used=50, started_at=2000, completed_at=None),
        ]
        result = compute_token_metrics(runs)

        assert result["total_tokens"] == 150
        assert result["avg_tokens"] == 75.0
        # Rate is only from the one timed run: 100/10 = 10
        assert result["tokens_per_second"] == 10.0


# ---------------------------------------------------------------------------
# Composite health report
# ---------------------------------------------------------------------------


class TestComputePlaybookHealth:
    def test_no_runs(self):
        result = compute_playbook_health([])
        assert result["total_runs"] == 0
        assert "message" in result

    def test_no_runs_with_playbook_id(self):
        result = compute_playbook_health([], playbook_id="my-pb")
        assert result["playbook_id"] == "my-pb"
        assert result["total_runs"] == 0

    def test_complete_report_structure(self):
        """Verify the health report contains all expected sections."""
        trace = [
            _trace_entry("scan", 1000, 1005, "completed", "triage", "goto", 50),
            _trace_entry("triage", 1005, 1010, "completed", "done", "llm", 80),
        ]
        runs = [
            _make_run(
                run_id="r1",
                status="completed",
                node_trace=trace,
                tokens_used=130,
                started_at=1000,
                completed_at=1010,
            ),
            _make_run(
                run_id="r2",
                status="failed",
                node_trace=[
                    _trace_entry("scan", 2000, 2003, "failed"),
                ],
                tokens_used=30,
                started_at=2000,
                completed_at=2003,
                error="Node 'scan' failed: timeout",
            ),
        ]

        result = compute_playbook_health(runs, playbook_id="test-pb")

        assert result["playbook_id"] == "test-pb"
        assert result["total_runs"] == 2

        # Summary
        assert "summary" in result
        assert result["summary"]["completion_rate"] == 0.5
        assert result["summary"]["by_status"]["completed"] == 1
        assert result["summary"]["by_status"]["failed"] == 1

        # Duration
        assert "duration" in result
        assert result["duration"]["count"] == 2

        # Tokens
        assert "tokens" in result
        assert result["tokens"]["total_tokens"] == 160

        # Nodes
        assert "nodes" in result
        assert "scan" in result["nodes"]
        assert "triage" in result["nodes"]

        # Transition paths
        assert "transition_paths" in result
        assert result["transition_paths"]["unique_path_count"] >= 1

        # Failure analysis
        assert "failure_analysis" in result
        assert result["failure_analysis"]["failed_runs"] == 1

    def test_single_playbook_id_inferred(self):
        """When all runs belong to one playbook, its ID is used in the report."""
        runs = [
            _make_run(run_id="r1", playbook_id="my-scanner"),
            _make_run(run_id="r2", playbook_id="my-scanner"),
        ]
        result = compute_playbook_health(runs)
        assert result["playbook_id"] == "my-scanner"

    def test_multiple_playbook_ids(self):
        """When runs span multiple playbooks, 'all' is used."""
        runs = [
            _make_run(run_id="r1", playbook_id="scanner"),
            _make_run(run_id="r2", playbook_id="fixer"),
        ]
        result = compute_playbook_health(runs)
        assert result["playbook_id"] == "all"

    def test_node_tokens_per_node(self):
        """Per-node token tracking from trace entries works end-to-end."""
        trace = [
            _trace_entry("scan", 1000, 1002, "completed", "triage", "goto", tokens_used=40),
            _trace_entry("triage", 1002, 1005, "completed", "fix", "llm", tokens_used=80),
            _trace_entry("fix", 1005, 1020, "completed", tokens_used=200),
        ]
        runs = [_make_run(node_trace=trace, tokens_used=320)]
        result = compute_playbook_health(runs)

        nodes = result["nodes"]
        assert nodes["scan"]["avg_tokens"] == 40.0
        assert nodes["triage"]["avg_tokens"] == 80.0
        assert nodes["fix"]["avg_tokens"] == 200.0

    def test_realistic_multi_run_scenario(self):
        """Realistic scenario with 10 runs, some succeeding, some failing."""
        runs = []
        for i in range(7):
            trace = [
                _trace_entry(
                    "init", 1000 * i, 1000 * i + 2, "completed", "process", "goto", tokens_used=20
                ),
                _trace_entry(
                    "process",
                    1000 * i + 2,
                    1000 * i + 10,
                    "completed",
                    "done",
                    "llm",
                    tokens_used=80,
                ),
            ]
            runs.append(
                _make_run(
                    run_id=f"ok-{i}",
                    status="completed",
                    node_trace=trace,
                    tokens_used=100,
                    started_at=1000 * i,
                    completed_at=1000 * i + 10,
                )
            )

        for i in range(3):
            trace = [
                _trace_entry(
                    "init",
                    10000 + 1000 * i,
                    10000 + 1000 * i + 2,
                    "completed",
                    "process",
                    "goto",
                    tokens_used=20,
                ),
                _trace_entry(
                    "process", 10000 + 1000 * i + 2, 10000 + 1000 * i + 5, "failed", tokens_used=30
                ),
            ]
            runs.append(
                _make_run(
                    run_id=f"fail-{i}",
                    status="failed",
                    node_trace=trace,
                    tokens_used=50,
                    started_at=10000 + 1000 * i,
                    completed_at=10000 + 1000 * i + 5,
                    error="Node 'process' failed: API error",
                )
            )

        result = compute_playbook_health(runs)

        assert result["total_runs"] == 10
        assert result["summary"]["completion_rate"] == 0.7
        assert result["failure_analysis"]["failure_rate"] == 0.3

        # Node-level: process has 3 failures out of 10
        assert result["nodes"]["process"]["failure_count"] == 3
        assert result["nodes"]["process"]["failure_rate"] == 0.3

        # init never fails
        assert result["nodes"]["init"]["failure_count"] == 0
        assert result["nodes"]["init"]["failure_rate"] == 0.0


# ---------------------------------------------------------------------------
# Per-node token tracking in PlaybookRunner (integration with _trace_to_dict)
# ---------------------------------------------------------------------------


class TestNodeTraceEntryTokens:
    """Verify NodeTraceEntry.tokens_used flows through _trace_to_dict."""

    def test_trace_to_dict_includes_tokens(self):
        from src.playbook_runner import NodeTraceEntry, PlaybookRunner

        entry = NodeTraceEntry(
            node_id="scan",
            started_at=1000.0,
            completed_at=1005.0,
            status="completed",
            tokens_used=42,
        )
        d = PlaybookRunner._trace_to_dict(entry)
        assert d["tokens_used"] == 42

    def test_trace_to_dict_omits_zero_tokens(self):
        """Zero tokens_used is omitted (backward compat with pre-5.7.1 traces)."""
        from src.playbook_runner import NodeTraceEntry, PlaybookRunner

        entry = NodeTraceEntry(
            node_id="scan",
            started_at=1000.0,
            completed_at=1005.0,
            status="completed",
            tokens_used=0,
        )
        d = PlaybookRunner._trace_to_dict(entry)
        assert "tokens_used" not in d

    def test_trace_entry_default_tokens_zero(self):
        from src.playbook_runner import NodeTraceEntry

        entry = NodeTraceEntry(node_id="test", started_at=0)
        assert entry.tokens_used == 0


# ---------------------------------------------------------------------------
# Edge cases and robustness
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_malformed_node_trace_json(self):
        """Runs with malformed node_trace JSON are handled gracefully."""
        run = PlaybookRun(
            run_id="bad",
            playbook_id="pb",
            playbook_version=1,
            node_trace="not valid json{{{",
            tokens_used=100,
            started_at=1000.0,
            completed_at=1010.0,
            status="completed",
        )
        # Should not raise
        result = compute_node_metrics([run])
        assert result == {}

        result = compute_transition_paths([run])
        assert result["paths"] == []

    def test_empty_node_trace(self):
        """Run with empty node_trace produces no node metrics."""
        run = _make_run(node_trace=[])
        result = compute_node_metrics([run])
        assert result == {}

    def test_null_node_trace(self):
        """Run with null node_trace string is handled gracefully."""
        run = PlaybookRun(
            run_id="null",
            playbook_id="pb",
            playbook_version=1,
            node_trace="null",
            tokens_used=0,
            started_at=1000.0,
            status="completed",
        )
        result = compute_node_metrics([run])
        assert result == {}

    def test_very_large_run_set(self):
        """Health metrics handle hundreds of runs without issues."""
        runs = []
        for i in range(500):
            trace = [
                _trace_entry(
                    "step", float(i * 100), float(i * 100 + 50), "completed", tokens_used=10
                ),
            ]
            runs.append(
                _make_run(
                    run_id=f"r{i}",
                    status="completed",
                    node_trace=trace,
                    tokens_used=10,
                    started_at=float(i * 100),
                    completed_at=float(i * 100 + 50),
                )
            )

        result = compute_playbook_health(runs)
        assert result["total_runs"] == 500
        assert result["nodes"]["step"]["execution_count"] == 500

    def test_paused_run_included_in_status_counts(self):
        """Paused runs are counted in status breakdown but not in duration."""
        runs = [
            _make_run(run_id="r1", status="paused", completed_at=None),
            _make_run(run_id="r2", status="completed"),
        ]
        result = compute_playbook_health(runs)

        assert result["summary"]["by_status"]["paused"] == 1
        assert result["summary"]["by_status"]["completed"] == 1
        # Duration should only count the completed run
        assert result["duration"]["count"] == 1
