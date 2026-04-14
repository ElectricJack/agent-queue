"""Playbook health metrics — tokens per node, run duration, transition paths, failure rates.

Computes aggregate health metrics from persisted :class:`~src.models.PlaybookRun`
records.  All functions are pure — they accept lists of runs (or pre-parsed trace
data) and return metric dicts.  No database access; the caller is responsible for
fetching the data.

Roadmap 5.7.1.

Metrics computed:

- **Per-node**: average/p50/p95 duration, average tokens, failure rate
- **Per-playbook**: completion rate, failure rate, average duration, average tokens,
  most common transition paths
- **Transition paths**: frequency of each unique path through the graph
- **Failure analysis**: failure rates by node, most common error patterns

Example usage::

    from src.playbooks.health import compute_playbook_health
    runs = await db.list_playbook_runs(playbook_id="my-playbook", limit=200)
    metrics = compute_playbook_health(runs)
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass

from src.models import PlaybookRun

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes for structured results
# ---------------------------------------------------------------------------


@dataclass
class NodeMetrics:
    """Aggregate metrics for a single node across multiple runs."""

    node_id: str
    execution_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    total_duration: float = 0.0
    total_tokens: int = 0
    durations: list[float] | None = None  # raw durations for percentile calc

    @property
    def failure_rate(self) -> float:
        if self.execution_count == 0:
            return 0.0
        return self.failure_count / self.execution_count

    @property
    def avg_duration(self) -> float:
        if self.execution_count == 0:
            return 0.0
        return self.total_duration / self.execution_count

    @property
    def avg_tokens(self) -> float:
        if self.execution_count == 0:
            return 0.0
        return self.total_tokens / self.execution_count


# ---------------------------------------------------------------------------
# Percentile helper
# ---------------------------------------------------------------------------


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Compute the *pct*-th percentile from a pre-sorted list.

    Uses linear interpolation between adjacent values (same as NumPy default).
    Returns 0.0 for empty lists.
    """
    if not sorted_values:
        return 0.0
    n = len(sorted_values)
    if n == 1:
        return sorted_values[0]
    # Position in the sorted array (0-indexed, fractional)
    k = (pct / 100.0) * (n - 1)
    lo = int(k)
    hi = min(lo + 1, n - 1)
    frac = k - lo
    return sorted_values[lo] + frac * (sorted_values[hi] - sorted_values[lo])


# ---------------------------------------------------------------------------
# Trace parsing helper
# ---------------------------------------------------------------------------


def _parse_node_trace(run: PlaybookRun) -> list[dict]:
    """Parse a PlaybookRun's node_trace JSON string into a list of dicts.

    Returns an empty list on parse failure (malformed JSON or missing field).
    """
    raw = run.node_trace
    if not raw:
        return []
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        return parsed if isinstance(parsed, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


# ---------------------------------------------------------------------------
# Core: per-node metrics
# ---------------------------------------------------------------------------


def compute_node_metrics(runs: list[PlaybookRun]) -> dict[str, dict]:
    """Compute per-node aggregate metrics from a list of playbook runs.

    Returns a dict keyed by ``node_id``, each containing:

    - ``execution_count``: how many times the node was visited
    - ``success_count`` / ``failure_count``: by node-level status
    - ``failure_rate``: ``failure_count / execution_count``
    - ``avg_duration_seconds``: mean execution time
    - ``p50_duration_seconds`` / ``p95_duration_seconds``: percentiles
    - ``avg_tokens``: mean estimated tokens per execution
    - ``total_tokens``: cumulative tokens across all executions

    Parameters
    ----------
    runs:
        List of :class:`PlaybookRun` records (all playbooks or filtered).

    Returns
    -------
    dict[str, dict]
        Keyed by node_id.
    """
    nodes: dict[str, NodeMetrics] = {}

    for run in runs:
        trace = _parse_node_trace(run)
        for entry in trace:
            nid = entry.get("node_id", "unknown")
            if nid not in nodes:
                nodes[nid] = NodeMetrics(node_id=nid, durations=[])

            m = nodes[nid]
            m.execution_count += 1

            status = entry.get("status", "")
            if status == "completed":
                m.success_count += 1
            elif status in ("failed", "skipped"):
                m.failure_count += 1

            # Duration
            started = entry.get("started_at")
            completed = entry.get("completed_at")
            if started is not None and completed is not None:
                dur = completed - started
                if dur >= 0:
                    m.total_duration += dur
                    m.durations.append(dur)

            # Tokens (roadmap 5.7.1 — per-node tracking)
            tokens = entry.get("tokens_used", 0)
            if tokens:
                m.total_tokens += tokens

    # Build output dicts with percentiles
    result: dict[str, dict] = {}
    for nid, m in nodes.items():
        sorted_durs = sorted(m.durations) if m.durations else []
        result[nid] = {
            "node_id": nid,
            "execution_count": m.execution_count,
            "success_count": m.success_count,
            "failure_count": m.failure_count,
            "failure_rate": round(m.failure_rate, 4),
            "avg_duration_seconds": round(m.avg_duration, 3),
            "p50_duration_seconds": round(_percentile(sorted_durs, 50), 3),
            "p95_duration_seconds": round(_percentile(sorted_durs, 95), 3),
            "avg_tokens": round(m.avg_tokens, 1),
            "total_tokens": m.total_tokens,
        }

    return result


# ---------------------------------------------------------------------------
# Core: transition paths
# ---------------------------------------------------------------------------


def compute_transition_paths(runs: list[PlaybookRun]) -> dict:
    """Analyse transition paths taken through the playbook graph.

    Returns:

    - ``paths``: list of ``{"path": [node_ids...], "count": N}`` sorted by
      frequency (most common first)
    - ``transitions``: dict of ``"from_node -> to_node"`` edge counts
    - ``transition_methods``: counter of transition methods (goto, llm, etc.)

    Parameters
    ----------
    runs:
        List of :class:`PlaybookRun` records.
    """
    path_counter: Counter[tuple[str, ...]] = Counter()
    edge_counter: Counter[str] = Counter()
    method_counter: Counter[str] = Counter()

    for run in runs:
        trace = _parse_node_trace(run)
        if not trace:
            continue

        # Build path (ordered list of node IDs visited)
        path = tuple(entry.get("node_id", "unknown") for entry in trace)
        path_counter[path] += 1

        # Count edges and transition methods
        for entry in trace:
            nid = entry.get("node_id", "unknown")
            target = entry.get("transition_to")
            method = entry.get("transition_method")
            if target:
                edge_counter[f"{nid} -> {target}"] += 1
            if method:
                method_counter[method] += 1

    # Format paths as list of dicts, sorted by frequency
    paths = [{"path": list(p), "count": c} for p, c in path_counter.most_common()]

    return {
        "paths": paths,
        "unique_path_count": len(paths),
        "transitions": dict(edge_counter.most_common()),
        "transition_methods": dict(method_counter.most_common()),
    }


# ---------------------------------------------------------------------------
# Core: failure analysis
# ---------------------------------------------------------------------------


def compute_failure_analysis(runs: list[PlaybookRun]) -> dict:
    """Analyse failure patterns across playbook runs.

    Returns:

    - ``total_runs`` / ``failed_runs`` / ``failure_rate``
    - ``failures_by_status``: counts for each terminal status
    - ``failures_by_node``: which nodes most commonly fail
    - ``common_errors``: most common error message prefixes

    Parameters
    ----------
    runs:
        List of :class:`PlaybookRun` records.
    """
    total = len(runs)
    status_counter: Counter[str] = Counter()
    node_failure_counter: Counter[str] = Counter()
    error_counter: Counter[str] = Counter()

    for run in runs:
        status_counter[run.status] += 1

        if run.status in ("failed", "timed_out"):
            # Determine which node failed from the trace
            trace = _parse_node_trace(run)
            for entry in trace:
                if entry.get("status") in ("failed",):
                    node_failure_counter[entry.get("node_id", "unknown")] += 1

            # Categorise error messages (take first 80 chars as key)
            if run.error:
                # Use the error prefix (before ':') for grouping
                prefix = run.error.split(":")[0].strip()[:80]
                error_counter[prefix] += 1

    failed_count = status_counter.get("failed", 0) + status_counter.get("timed_out", 0)

    return {
        "total_runs": total,
        "failed_runs": failed_count,
        "failure_rate": round(failed_count / total, 4) if total > 0 else 0.0,
        "by_status": dict(status_counter.most_common()),
        "failures_by_node": dict(node_failure_counter.most_common(20)),
        "common_errors": dict(error_counter.most_common(10)),
    }


# ---------------------------------------------------------------------------
# Core: run duration metrics
# ---------------------------------------------------------------------------


def compute_duration_metrics(runs: list[PlaybookRun]) -> dict:
    """Compute run duration statistics across playbook runs.

    Only considers runs that have both ``started_at`` and ``completed_at``
    (i.e., terminal runs — not currently running or paused).

    Returns:

    - ``count``: number of completed runs with timing data
    - ``avg_seconds`` / ``p50_seconds`` / ``p95_seconds`` / ``max_seconds``
    - ``total_seconds``: cumulative duration
    """
    durations: list[float] = []
    for run in runs:
        if run.started_at and run.completed_at:
            dur = run.completed_at - run.started_at
            if dur >= 0:
                durations.append(dur)

    if not durations:
        return {
            "count": 0,
            "avg_seconds": 0.0,
            "p50_seconds": 0.0,
            "p95_seconds": 0.0,
            "max_seconds": 0.0,
            "total_seconds": 0.0,
        }

    sorted_durs = sorted(durations)
    return {
        "count": len(durations),
        "avg_seconds": round(sum(durations) / len(durations), 3),
        "p50_seconds": round(_percentile(sorted_durs, 50), 3),
        "p95_seconds": round(_percentile(sorted_durs, 95), 3),
        "max_seconds": round(max(durations), 3),
        "total_seconds": round(sum(durations), 3),
    }


# ---------------------------------------------------------------------------
# Core: token usage metrics
# ---------------------------------------------------------------------------


def compute_token_metrics(runs: list[PlaybookRun]) -> dict:
    """Compute token usage statistics across playbook runs.

    Returns:

    - ``total_tokens``: cumulative across all runs
    - ``avg_tokens`` / ``p50_tokens`` / ``p95_tokens`` / ``max_tokens``
    - ``tokens_per_second``: average token rate for completed runs
    """
    token_counts: list[int] = []
    tokens_with_duration: list[tuple[int, float]] = []

    for run in runs:
        token_counts.append(run.tokens_used)
        if run.started_at and run.completed_at:
            dur = run.completed_at - run.started_at
            if dur > 0:
                tokens_with_duration.append((run.tokens_used, dur))

    if not token_counts:
        return {
            "total_tokens": 0,
            "avg_tokens": 0.0,
            "p50_tokens": 0.0,
            "p95_tokens": 0.0,
            "max_tokens": 0,
            "tokens_per_second": 0.0,
        }

    sorted_tokens = sorted(float(t) for t in token_counts)
    total_tokens = sum(token_counts)

    # Tokens per second: total tokens / total duration for runs with timing
    total_timed_tokens = sum(t for t, _ in tokens_with_duration)
    total_timed_duration = sum(d for _, d in tokens_with_duration)
    tps = total_timed_tokens / total_timed_duration if total_timed_duration > 0 else 0.0

    return {
        "total_tokens": total_tokens,
        "avg_tokens": round(total_tokens / len(token_counts), 1),
        "p50_tokens": round(_percentile(sorted_tokens, 50), 1),
        "p95_tokens": round(_percentile(sorted_tokens, 95), 1),
        "max_tokens": max(token_counts),
        "tokens_per_second": round(tps, 2),
    }


# ---------------------------------------------------------------------------
# Composite: full health report
# ---------------------------------------------------------------------------


def compute_playbook_health(
    runs: list[PlaybookRun],
    *,
    playbook_id: str | None = None,
) -> dict:
    """Compute a comprehensive health report for playbook runs.

    This is the main entry point — combines all individual metric functions
    into a single report dict suitable for the ``playbook_health`` command.

    Parameters
    ----------
    runs:
        List of :class:`PlaybookRun` records to analyse.
    playbook_id:
        Optional playbook ID for labelling (cosmetic only — filtering
        should be done by the caller before passing runs).

    Returns
    -------
    dict
        Complete health report with sections: ``summary``, ``duration``,
        ``tokens``, ``nodes``, ``transition_paths``, ``failure_analysis``.
    """
    if not runs:
        return {
            "playbook_id": playbook_id or "all",
            "total_runs": 0,
            "message": "No playbook runs found for the given criteria.",
        }

    # Separate metrics by playbook if analysing multiple
    playbook_ids = {r.playbook_id for r in runs}

    # Summary counts
    status_counts: Counter[str] = Counter(r.status for r in runs)
    completion_rate = status_counts.get("completed", 0) / len(runs) if runs else 0.0

    report: dict = {
        "playbook_id": playbook_id
        or ("all" if len(playbook_ids) > 1 else next(iter(playbook_ids))),
        "total_runs": len(runs),
        "summary": {
            "by_status": dict(status_counts.most_common()),
            "completion_rate": round(completion_rate, 4),
            "playbooks_analysed": sorted(playbook_ids),
        },
        "duration": compute_duration_metrics(runs),
        "tokens": compute_token_metrics(runs),
        "nodes": compute_node_metrics(runs),
        "transition_paths": compute_transition_paths(runs),
        "failure_analysis": compute_failure_analysis(runs),
    }

    return report
