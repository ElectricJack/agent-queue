from unittest.mock import AsyncMock, MagicMock

from src.doctor.models import DoctorContext, Severity
from src.doctor.plan_checks import run_check


async def test_reports_stranded_rows():
    db = MagicMock()
    db.list_tasks = AsyncMock(return_value=[MagicMock(id="t1"), MagicMock(id="t2")])
    r = await run_check("tasks.awaiting_plan_approval", DoctorContext(config=MagicMock(), db=db))
    assert r.severity == Severity.WARN
    assert r.data["tasks"] == ["t1", "t2"]
    assert "aq task reopen" in r.detail


async def test_ok_when_none():
    db = MagicMock()
    db.list_tasks = AsyncMock(return_value=[])
    r = await run_check("tasks.awaiting_plan_approval", DoctorContext(config=MagicMock(), db=db))
    assert r.severity == Severity.OK
