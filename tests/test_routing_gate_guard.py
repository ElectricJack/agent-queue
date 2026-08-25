"""A routing gate is only for tasks that still need a profile.

The default pipeline attaches one on every ``task.created`` and then ensures a
triage task to resolve it. For a task created with an explicit profile that is
work nobody can do: an agent starts, finds nothing unrouted, and closes — once
per created task. The gate is never resolved either; it sits open until the
task finishes and expires as "all waiters terminal", which reads as a task that
ran unrouted when it was routed at creation.

Observed live: two tasks created with ``-P worker-standard`` each produced a
routing gate and a triage agent, and ``task_route`` was never called once.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.commands.gate_commands import GateCommandsMixin


def _handler(tasks: dict):
    h = GateCommandsMixin()
    h.db = MagicMock()
    h.db.get_task = AsyncMock(side_effect=lambda tid: tasks.get(tid))
    h.db.create_gate = AsyncMock(return_value=("gate-1", True))
    h.orchestrator = SimpleNamespace(bus=MagicMock(emit=AsyncMock()))
    return h


def _task(tid, profile_id=""):
    return SimpleNamespace(id=tid, profile_id=profile_id)


BASE = {"project_id": "p1", "gate_type": "routing", "title": "Route task"}


@pytest.mark.asyncio
class TestRoutingGateGuard:
    async def test_skipped_when_every_waiter_is_routed(self):
        h = _handler({"t1": _task("t1", "worker-standard")})
        res = await h._cmd_gate_create({**BASE, "waiter_task_ids": ["t1"]})
        assert res["success"] is True
        assert res["skipped"] is True
        h.db.create_gate.assert_not_awaited()

    async def test_created_when_a_waiter_has_no_profile(self):
        h = _handler({"t1": _task("t1", "")})
        res = await h._cmd_gate_create({**BASE, "waiter_task_ids": ["t1"]})
        assert res.get("skipped") is not True
        h.db.create_gate.assert_awaited_once()

    async def test_narrowed_to_the_unrouted_waiters(self):
        """A mixed batch gates only the tasks that actually need routing."""
        h = _handler({"t1": _task("t1", "worker-standard"), "t2": _task("t2", "")})
        await h._cmd_gate_create({**BASE, "waiter_task_ids": ["t1", "t2"]})
        assert h.db.create_gate.await_args.kwargs["waiter_task_ids"] == ["t2"]

    async def test_unknown_task_still_gets_a_gate(self):
        """Cannot tell => attach. A spurious gate beats an unrouted task."""
        h = _handler({})
        res = await h._cmd_gate_create({**BASE, "waiter_task_ids": ["ghost"]})
        assert res.get("skipped") is not True
        h.db.create_gate.assert_awaited_once()

    async def test_other_gate_types_are_untouched(self):
        h = _handler({"t1": _task("t1", "worker-standard")})
        res = await h._cmd_gate_create(
            {**BASE, "gate_type": "human", "waiter_task_ids": ["t1"]}
        )
        assert res.get("skipped") is not True
        h.db.create_gate.assert_awaited_once()
