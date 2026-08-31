"""Recovery permissions and response contracts through both HTTP surfaces."""
import pytest

from tests.test_supervisor_recovery import env, incident  # noqa: F401
from tests.test_task_comments_api import api, task_routers  # noqa: F401

pytestmark = pytest.mark.asyncio


async def test_recovery_http_scope_and_response(api):  # noqa: F811
    current = await incident(api.env)
    args = {"task_id": "t", "incident_id": current["id"], "decision": "retry", "reason": "Safe continuation after inspection"}
    for caller in ("worker", "pinned", "unassigned"):
        ok, _ = await api.post("task_recover", args, caller=caller)
        assert not ok
    ok, data = await api.post("task_recover", args, caller="global")
    assert ok, data
    assert data["status"] == "READY" and data["incident_id"] == current["id"]
    ok, data = await api.post("task_comments", {"task_id": "t"}, caller="global")
    assert ok, data
    assert data["comments"][0]["author_kind"] == "supervisor"
    assert data["comments"][0]["author_id"] == "supervisor-global"
