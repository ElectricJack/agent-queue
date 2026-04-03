"""Markdown report generation from evaluation results."""

from __future__ import annotations

from tests.chat_eval.metrics import EvalRunResult, Regression
from datetime import datetime, timezone


def generate_report(run: EvalRunResult) -> str:
    """Generate a markdown report from an eval run."""
    lines: list[str] = []
    ts = datetime.fromtimestamp(run.timestamp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines.append(f"# Chat Agent Eval Report — {run.model}")
    lines.append(f"**Date:** {ts}")
    lines.append(f"**Pass rate:** {run.pass_rate:.1%} ({run.passed_cases}/{run.total_cases})")
    lines.append("")

    # Summary table
    lines.append("## Summary")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    lines.append(f"| Total cases | {run.total_cases} |")
    lines.append(f"| Passed | {run.passed_cases} |")
    lines.append(f"| Failed | {run.failed_cases} |")
    lines.append(f"| Pass rate | {run.pass_rate:.1%} |")
    lines.append("")

    # By category
    lines.append("## By Category")
    lines.append("")
    lines.append("| Category | Passed | Total | Rate |")
    lines.append("|----------|--------|-------|------|")
    for cat, stats in sorted(run.by_category.items()):
        total = stats["total"]
        passed = stats["passed"]
        rate = passed / total if total > 0 else 0
        lines.append(f"| {cat} | {passed} | {total} | {rate:.0%} |")
    lines.append("")

    # By difficulty
    lines.append("## By Difficulty")
    lines.append("")
    lines.append("| Difficulty | Passed | Total | Rate |")
    lines.append("|------------|--------|-------|------|")
    for diff, stats in sorted(run.by_difficulty.items()):
        total = stats["total"]
        passed = stats["passed"]
        rate = passed / total if total > 0 else 0
        lines.append(f"| {diff} | {passed} | {total} | {rate:.0%} |")
    lines.append("")

    # Bottom 10 tools by recall
    if run.tool_accuracy:
        lines.append("## Bottom 10 Tools by Recall")
        lines.append("")
        lines.append("| Tool | Recall | F1 | Arg Accuracy | TP | FN | FP |")
        lines.append("|------|--------|-----|-------------|----|----|-----|")
        sorted_tools = sorted(
            run.tool_accuracy.values(),
            key=lambda t: t.recall,
        )
        for ta in sorted_tools[:10]:
            lines.append(
                f"| {ta.tool_name} | {ta.recall:.0%} | {ta.f1:.0%} "
                f"| {ta.arg_accuracy:.0%} | {ta.true_positives} "
                f"| {ta.false_negatives} | {ta.false_positives} |"
            )
        lines.append("")

    # Failed cases
    failed = [r for r in run.case_results if not r.passed]
    if failed:
        lines.append("## Failed Cases")
        lines.append("")
        for case_result in failed[:50]:  # Cap at 50
            lines.append(f"### {case_result.case_id}")
            lines.append(
                f"**Category:** {case_result.category} | **Difficulty:** {case_result.difficulty}"
            )
            if case_result.error:
                lines.append(f"**Error:** {case_result.error}")
            for i, tr in enumerate(case_result.turn_results):
                if not tr.passed:
                    lines.append(f'- Turn {i + 1}: "{tr.user_message[:80]}"')
                    for m in tr.tool_matches:
                        if not m.matched:
                            lines.append(f"  - MISSING: `{m.expected_name}`")
                        elif not m.args_matched:
                            lines.append(
                                f"  - ARG MISMATCH: `{m.expected_name}` keys={m.mismatched_keys}"
                            )
                    for f in tr.forbidden_tools_called:
                        lines.append(f"  - FORBIDDEN: `{f}`")
                    for u in tr.unexpected_tools:
                        lines.append(f"  - UNEXPECTED: `{u}`")
            lines.append("")

    return "\n".join(lines)


def generate_regression_report(
    regressions: list[Regression],
    current_model: str,
    baseline_model: str,
) -> str:
    """Generate a regression report comparing two runs."""
    lines: list[str] = []
    lines.append(f"# Regression Report: {current_model} vs {baseline_model}")
    lines.append("")

    if not regressions:
        lines.append("No regressions detected.")
        return "\n".join(lines)

    lines.append(f"**{len(regressions)} regression(s) detected:**")
    lines.append("")
    lines.append("| Scope | Metric | Baseline | Current | Delta |")
    lines.append("|-------|--------|----------|---------|-------|")
    for r in regressions:
        lines.append(
            f"| {r.scope} | {r.metric} | {r.baseline:.1%} | {r.current:.1%} | {r.delta:+.1%} |"
        )
    lines.append("")

    return "\n".join(lines)
