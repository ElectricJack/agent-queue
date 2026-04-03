"""Evaluation metrics: per-tool accuracy, aggregation, regression detection."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

from tests.chat_eval.test_cases._types import TestCase, Turn, ExpectedTool
from tests.chat_eval.recording_handler import CommandCall

# Bidirectional alias map: either name in a pair is accepted as matching the other.
# This handles the duplicate git tools (old-gen vs new-gen names) and parameter
# aliases like show_all/include_completed.
TOOL_ALIASES: dict[str, str] = {
    "git_commit": "commit_changes",
    "commit_changes": "git_commit",
    "git_push": "push_branch",
    "push_branch": "git_push",
    "git_create_branch": "create_branch",
    "create_branch": "git_create_branch",
    "git_merge": "merge_branch",
    "merge_branch": "git_merge",
    # "what's running?" can reasonably use either tool
    "list_tasks": "list_active_tasks_all_projects",
    "list_active_tasks_all_projects": "list_tasks",
    # write vs append notes — model sometimes confuses these
    "write_note": "append_note",
    "append_note": "write_note",
}

# Argument name aliases: when a tool is called by an alias, the arg names may
# differ.  Map (canonical_tool, canonical_arg) -> alias_arg so we can match
# across naming conventions.
ARG_ALIASES: dict[str, dict[str, str]] = {
    "git_push": {"branch": "branch_name"},
    "push_branch": {"branch_name": "branch"},
}


@dataclass
class ToolMatch:
    """Result of matching a single expected tool against actual calls."""

    expected_name: str
    matched: bool
    args_matched: bool
    actual_args: dict = field(default_factory=dict)
    expected_args: dict = field(default_factory=dict)
    mismatched_keys: list[str] = field(default_factory=list)


@dataclass
class TurnResult:
    """Evaluation result for a single conversation turn."""

    user_message: str
    tool_matches: list[ToolMatch] = field(default_factory=list)
    unexpected_tools: list[str] = field(default_factory=list)
    forbidden_tools_called: list[str] = field(default_factory=list)
    order_correct: bool = True
    passed: bool = False
    latency: float = 0.0


@dataclass
class TestCaseResult:
    """Evaluation result for a complete test case."""

    case_id: str
    category: str
    difficulty: str
    tags: list[str]
    turn_results: list[TurnResult] = field(default_factory=list)
    passed: bool = False
    error: str | None = None


@dataclass
class ToolAccuracy:
    """Per-tool precision, recall, F1, and argument accuracy."""

    tool_name: str
    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0
    args_correct: int = 0
    args_total: int = 0

    @property
    def precision(self) -> float:
        total = self.true_positives + self.false_positives
        return self.true_positives / total if total > 0 else 0.0

    @property
    def recall(self) -> float:
        total = self.true_positives + self.false_negatives
        return self.true_positives / total if total > 0 else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) > 0 else 0.0

    @property
    def arg_accuracy(self) -> float:
        return self.args_correct / self.args_total if self.args_total > 0 else 0.0


@dataclass
class EvalRunResult:
    """Aggregated results for an entire evaluation run."""

    model: str = ""
    timestamp: float = field(default_factory=time.time)
    total_cases: int = 0
    passed_cases: int = 0
    failed_cases: int = 0
    case_results: list[TestCaseResult] = field(default_factory=list)
    tool_accuracy: dict[str, ToolAccuracy] = field(default_factory=dict)
    by_category: dict[str, dict] = field(default_factory=dict)
    by_difficulty: dict[str, dict] = field(default_factory=dict)
    total_latency: float = 0.0

    @property
    def pass_rate(self) -> float:
        return self.passed_cases / self.total_cases if self.total_cases > 0 else 0.0


def _match_args(expected: dict, actual: dict, exact: bool) -> tuple[bool, list[str]]:
    """Check if actual args match expected args (subset or exact)."""
    if exact:
        mismatched = []
        all_keys = set(expected) | set(actual)
        for key in all_keys:
            if expected.get(key) != actual.get(key):
                mismatched.append(key)
        return len(mismatched) == 0, mismatched

    # Subset match: only check keys specified in expected
    mismatched = []
    for key, val in expected.items():
        if key not in actual or actual[key] != val:
            mismatched.append(key)
    return len(mismatched) == 0, mismatched


def evaluate_turn(
    turn: Turn,
    actual_calls: list[CommandCall],
) -> TurnResult:
    """Compare expected vs actual tool calls for a turn."""
    result = TurnResult(user_message=turn.user_message)
    actual_names = [c.name for c in actual_calls]
    actual_by_name: dict[str, list[CommandCall]] = {}
    for call in actual_calls:
        actual_by_name.setdefault(call.name, []).append(call)

    # Track which actual calls have been matched
    matched_actual_indices: set[int] = set()

    # Match expected tools
    for expected in turn.expected_tools:
        # Find matching actual call
        matched = False
        args_matched = False
        actual_args = {}
        mismatched_keys: list[str] = []

        for i, call in enumerate(actual_calls):
            if i in matched_actual_indices:
                continue
            # Match by exact name or known alias
            alias = TOOL_ALIASES.get(expected.name)
            if call.name != expected.name and call.name != alias:
                continue
            matched = True
            actual_args = call.args
            # If matched via alias, remap arg names before comparison
            check_args = call.args
            if call.name != expected.name and call.name in ARG_ALIASES:
                remap = ARG_ALIASES[call.name]
                check_args = {remap.get(k, k): v for k, v in call.args.items()}
            args_matched_result, mismatched = _match_args(
                expected.args,
                check_args,
                expected.args_exact,
            )
            args_matched = args_matched_result
            mismatched_keys = mismatched
            matched_actual_indices.add(i)
            break

        result.tool_matches.append(
            ToolMatch(
                expected_name=expected.name,
                matched=matched,
                args_matched=args_matched if matched else False,
                actual_args=actual_args,
                expected_args=expected.args,
                mismatched_keys=mismatched_keys,
            )
        )

    # Check for unexpected tools (those not in expected list, accounting for aliases)
    expected_names = {e.name for e in turn.expected_tools}
    expected_with_aliases = set(expected_names)
    for n in expected_names:
        if n in TOOL_ALIASES:
            expected_with_aliases.add(TOOL_ALIASES[n])
    for i, call in enumerate(actual_calls):
        if i not in matched_actual_indices and call.name not in expected_with_aliases:
            result.unexpected_tools.append(call.name)

    # Check forbidden tools
    for forbidden_name in turn.not_expected_tools:
        if forbidden_name in actual_names:
            result.forbidden_tools_called.append(forbidden_name)

    # Check order if required
    if turn.ordered and turn.expected_tools:
        expected_order = [e.name for e in turn.expected_tools]
        actual_matched = [actual_calls[i].name for i in sorted(matched_actual_indices)]
        result.order_correct = actual_matched == expected_order

    # Determine pass/fail
    all_matched = all(m.matched for m in result.tool_matches)
    all_args_ok = all(m.args_matched for m in result.tool_matches if m.matched)
    no_forbidden = len(result.forbidden_tools_called) == 0
    order_ok = result.order_correct
    result.passed = all_matched and all_args_ok and no_forbidden and order_ok

    return result


def aggregate_results(
    case_results: list[TestCaseResult],
    model: str = "",
) -> EvalRunResult:
    """Roll up individual case results into run-level metrics."""
    run = EvalRunResult(
        model=model,
        total_cases=len(case_results),
        passed_cases=sum(1 for r in case_results if r.passed),
        failed_cases=sum(1 for r in case_results if not r.passed),
        case_results=case_results,
    )

    # Per-tool accuracy
    tool_stats: dict[str, ToolAccuracy] = {}

    for case_result in case_results:
        # Per-category stats
        cat = case_result.category
        if cat not in run.by_category:
            run.by_category[cat] = {"total": 0, "passed": 0}
        run.by_category[cat]["total"] += 1
        if case_result.passed:
            run.by_category[cat]["passed"] += 1

        # Per-difficulty stats
        diff = case_result.difficulty
        if diff not in run.by_difficulty:
            run.by_difficulty[diff] = {"total": 0, "passed": 0}
        run.by_difficulty[diff]["total"] += 1
        if case_result.passed:
            run.by_difficulty[diff]["passed"] += 1

        # Per-tool stats from turn results
        for turn_result in case_result.turn_results:
            for match in turn_result.tool_matches:
                name = match.expected_name
                if name not in tool_stats:
                    tool_stats[name] = ToolAccuracy(tool_name=name)
                if match.matched:
                    tool_stats[name].true_positives += 1
                    tool_stats[name].args_total += 1
                    if match.args_matched:
                        tool_stats[name].args_correct += 1
                else:
                    tool_stats[name].false_negatives += 1

            for unexpected in turn_result.unexpected_tools:
                if unexpected not in tool_stats:
                    tool_stats[unexpected] = ToolAccuracy(tool_name=unexpected)
                tool_stats[unexpected].false_positives += 1

    run.tool_accuracy = tool_stats
    return run


def save_run(run: EvalRunResult, output_dir: str | Path, filename: str = "") -> Path:
    """Persist an eval run as JSON for historical comparison."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not filename:
        filename = f"eval_{run.model}_{int(run.timestamp)}.json"
    path = output_dir / filename

    # Per-case details for diagnosing failures and improving prompts
    case_details = []
    for r in run.case_results:
        turns = []
        for tr in r.turn_results:
            turns.append(
                {
                    "user_message": tr.user_message,
                    "passed": tr.passed,
                    "latency": tr.latency,
                    "expected_tools": [
                        {
                            "name": m.expected_name,
                            "matched": m.matched,
                            "args_matched": m.args_matched,
                            "expected_args": m.expected_args,
                            "actual_args": m.actual_args,
                            "mismatched_keys": m.mismatched_keys,
                        }
                        for m in tr.tool_matches
                    ],
                    "unexpected_tools": tr.unexpected_tools,
                    "forbidden_tools_called": tr.forbidden_tools_called,
                    "order_correct": tr.order_correct,
                }
            )
        case_details.append(
            {
                "case_id": r.case_id,
                "category": r.category,
                "difficulty": r.difficulty,
                "tags": r.tags,
                "passed": r.passed,
                "error": r.error,
                "turns": turns,
            }
        )

    data = {
        "model": run.model,
        "timestamp": run.timestamp,
        "total_cases": run.total_cases,
        "passed_cases": run.passed_cases,
        "failed_cases": run.failed_cases,
        "pass_rate": run.pass_rate,
        "by_category": run.by_category,
        "by_difficulty": run.by_difficulty,
        "tool_accuracy": {
            name: {
                "precision": ta.precision,
                "recall": ta.recall,
                "f1": ta.f1,
                "arg_accuracy": ta.arg_accuracy,
                "true_positives": ta.true_positives,
                "false_positives": ta.false_positives,
                "false_negatives": ta.false_negatives,
            }
            for name, ta in run.tool_accuracy.items()
        },
        "failed_case_ids": [r.case_id for r in run.case_results if not r.passed],
        "case_details": case_details,
    }

    path.write_text(json.dumps(data, indent=2))
    return path


def load_run(path: str | Path) -> dict:
    """Load a previously saved eval run from JSON."""
    return json.loads(Path(path).read_text())


@dataclass
class Regression:
    """A detected regression between two eval runs."""

    scope: str  # "category:projects", "tool:create_task", "overall"
    metric: str  # "pass_rate", "recall", "f1"
    baseline: float
    current: float
    delta: float


def detect_regressions(
    current: dict,
    baseline: dict,
    threshold: float = 0.05,
) -> list[Regression]:
    """Compare two run JSONs and flag degradations exceeding threshold."""
    regressions: list[Regression] = []

    # Overall pass rate
    curr_rate = current.get("pass_rate", 0)
    base_rate = baseline.get("pass_rate", 0)
    if base_rate - curr_rate > threshold:
        regressions.append(
            Regression(
                scope="overall",
                metric="pass_rate",
                baseline=base_rate,
                current=curr_rate,
                delta=curr_rate - base_rate,
            )
        )

    # Per-category pass rates
    for cat, base_stats in baseline.get("by_category", {}).items():
        curr_stats = current.get("by_category", {}).get(cat, {})
        base_cat_rate = base_stats["passed"] / base_stats["total"] if base_stats["total"] else 0
        curr_total = curr_stats.get("total", 0)
        curr_cat_rate = curr_stats["passed"] / curr_total if curr_total else 0
        if base_cat_rate - curr_cat_rate > threshold:
            regressions.append(
                Regression(
                    scope=f"category:{cat}",
                    metric="pass_rate",
                    baseline=base_cat_rate,
                    current=curr_cat_rate,
                    delta=curr_cat_rate - base_cat_rate,
                )
            )

    # Per-tool recall
    for tool, base_ta in baseline.get("tool_accuracy", {}).items():
        curr_ta = current.get("tool_accuracy", {}).get(tool, {})
        base_recall = base_ta.get("recall", 0)
        curr_recall = curr_ta.get("recall", 0)
        if base_recall - curr_recall > threshold:
            regressions.append(
                Regression(
                    scope=f"tool:{tool}",
                    metric="recall",
                    baseline=base_recall,
                    current=curr_recall,
                    delta=curr_recall - base_recall,
                )
            )

    return regressions
