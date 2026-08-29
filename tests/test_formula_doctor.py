from unittest.mock import MagicMock

from src.doctor.formula_checks import formula_checks
from src.doctor.models import Severity
from src.task_graph.formulas import FormulaRegistry


def _ctx(errors):
    reg = FormulaRegistry()
    reg.errors.update(errors)
    ctx = MagicMock()
    ctx.handler.orchestrator.formula_registry = reg
    return ctx


def test_check_registered():
    assert {c.id for c in formula_checks()} == {"formulas.parse"}
    assert all(c.owner == "swarm-work-model" and c.fix is None for c in formula_checks())


async def test_ok_when_no_errors():
    check = formula_checks()[0]
    assert (await check.run(_ctx({}))).severity == Severity.OK


async def test_warn_lists_files():
    check = formula_checks()[0]
    res = await check.run(_ctx({"formulas/bad.md": "formula.no_graph: ..."}))
    assert res.severity == Severity.WARN and res.data["count"] == 1
    assert "formulas/bad.md" in res.data["files"]
