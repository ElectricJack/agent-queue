"""Token budget management for fair resource allocation across projects.

BudgetManager calculates target token ratios from per-project credit weights
and tracks how far each project's actual usage deviates from its target (the
"deficit score").  The Scheduler uses these deficit scores to decide which
project should receive the next available agent -- the most under-served
project wins.

This keeps agent time proportional to credit weights over rolling windows,
even when projects have bursty workloads.

See specs/scheduler-and-budget.md for the full specification.
"""

from __future__ import annotations


class BudgetManager:
    def __init__(self, global_budget: int | None = None):
        self.global_budget = global_budget

    def calculate_target_ratios(self, weights: dict[str, float]) -> dict[str, float]:
        total = sum(weights.values())
        if total == 0:
            return {}
        return {pid: w / total for pid, w in weights.items()}

    def calculate_deficits(
        self, weights: dict[str, float], usage: dict[str, int]
    ) -> dict[str, float]:
        targets = self.calculate_target_ratios(weights)
        total_usage = sum(usage.values())
        if total_usage == 0:
            return dict(targets)
        result = {}
        for pid, target in targets.items():
            actual = usage.get(pid, 0) / total_usage
            result[pid] = target - actual
        return result

    def is_global_budget_exhausted(self, total_used: int) -> bool:
        if self.global_budget is None:
            return False
        return total_used >= self.global_budget

    def is_project_budget_exhausted(self, project_used: int, budget_limit: int | None) -> bool:
        if budget_limit is None:
            return False
        return project_used >= budget_limit
