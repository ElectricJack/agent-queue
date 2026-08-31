"""Real typed/execute HTTP authorization and operator task control."""
import pytest

from tests.test_task_controls import env  # noqa: F401
from tests.test_task_comments_api import api, task_routers  # noqa: F401

pytestmark = pytest.mark.asyncio


async def test_pause_resume_http_operator_scope(api):  # noqa: F811
    for command in ("pause_task", "resume_task"):
        for caller in ("worker", "pinned", "unassigned"):
            ok, _ = await api.post(command, {"task_id": "t"}, caller=caller)
            assert not ok
    ok, data = await api.post("pause_task", {"task_id": "t"}, caller="supervisor")
    assert ok, data
    assert data["status"] == "PAUSED"
    ok, data = await api.post("resume_task", {"task_id": "t"}, caller="local")
    assert ok, data
    assert data["status"] == "READY"
