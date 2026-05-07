import pytest
from src.tokens.budget import BudgetManager


class TestBudgetManager:
    def test_proportional_ratios(self):
        mgr = BudgetManager()
        weights = {"p-1": 3.0, "p-2": 1.0}
        ratios = mgr.calculate_target_ratios(weights)
        assert ratios["p-1"] == pytest.approx(0.75)
        assert ratios["p-2"] == pytest.approx(0.25)

    def test_deficit_calculation(self):
        mgr = BudgetManager()
        weights = {"p-1": 3.0, "p-2": 1.0}
        usage = {"p-1": 60000, "p-2": 40000}  # total 100k
        deficits = mgr.calculate_deficits(weights, usage)
        # p-1: target 75%, actual 60%, deficit = 0.15
        assert deficits["p-1"] == pytest.approx(0.15)
        # p-2: target 25%, actual 40%, deficit = -0.15
        assert deficits["p-2"] == pytest.approx(-0.15)

    def test_zero_usage_equal_deficit(self):
        mgr = BudgetManager()
        weights = {"p-1": 1.0, "p-2": 1.0}
        usage = {}
        deficits = mgr.calculate_deficits(weights, usage)
        assert deficits["p-1"] == pytest.approx(0.5)
        assert deficits["p-2"] == pytest.approx(0.5)

    def test_global_budget_check(self):
        mgr = BudgetManager(global_budget=100000)
        assert mgr.is_global_budget_exhausted(99999) is False
        assert mgr.is_global_budget_exhausted(100000) is True
        assert mgr.is_global_budget_exhausted(100001) is True

    def test_no_global_budget(self):
        mgr = BudgetManager(global_budget=None)
        assert mgr.is_global_budget_exhausted(999999999) is False

    def test_project_budget_check(self):
        mgr = BudgetManager()
        assert mgr.is_project_budget_exhausted(50000, budget_limit=50000) is True
        assert mgr.is_project_budget_exhausted(49999, budget_limit=50000) is False
        assert mgr.is_project_budget_exhausted(99999, budget_limit=None) is False
