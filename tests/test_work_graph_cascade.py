"""Work-graph promotion cascade.

Covers docs/specs/implementation/work-graph.md §6.2 and §11:

* the persisted ``is_blocked`` projection is the sole readiness authority,
* the BLOCKED-recovery rule (failure-BLOCKED tasks stay put),
* ``parent-child`` replacing the ``is_plan_subtask`` special case,
* conditional auto-close behind ``conditional_autoclose``,
* the ``_sweep_gates`` re-gating.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.commands.handler import CommandHandler
from src.config import DatabaseConfig, AppConfig
from src.models import DepType, Project, Task, TaskStatus
from src.orchestrator import Orchestrator
from src.event_bus import EventBus
from tests.db_fixtures import lease_dsn


@pytest.fixture
async def orch(tmp_path):
    workspace = tmp_path / "workspaces"
    workspace.mkdir()
    config = AppConfig(
        database=DatabaseConfig(url=lease_dsn("test.db")),
        workspace_dir=str(workspace),
        data_dir=str(tmp_path / "data"),
    )
    o = Orchestrator(config)
    await o.initialize()
    await o.db.create_project(Project(id="p-1", name="proj"))
    yield o


async def mktask(orch, tid, status=TaskStatus.DEFINED, **kw):
    await orch.db.create_task(
        Task(id=tid, project_id="p-1", title=tid, description=tid, status=status, **kw)
    )
    return tid


async def status_of(orch, tid) -> TaskStatus:
    return (await orch.db.get_task(tid)).status


@pytest.fixture
async def event_gate_listener():
    listener = SimpleNamespace(
        db=SimpleNamespace(list_open_gates_by_type=AsyncMock(return_value=[])),
        bus=EventBus(validate_events=False), _gate_event_unsub=None,
        _resolve_gate_and_emit=AsyncMock(),
    )
    await Orchestrator._subscribe_event_gates(listener)
    return listener


async def test_unmatched_notification_burst_reads_event_gates_once(event_gate_listener):
    listener = event_gate_listener
    for _ in range(200):
        await listener.bus.emit("notify.task_message", {"task_id": "t"})
    listener.db.list_open_gates_by_type.assert_awaited_once_with("event")
    listener._resolve_gate_and_emit.assert_not_awaited()


@pytest.mark.parametrize("mutation", ["gate.created", "gate.resolved", "gate.expired"])
async def test_event_gate_changes_invalidate_interest_immediately(event_gate_listener, mutation):
    listener = event_gate_listener
    await listener.bus.emit("notify.task_message")
    listener.db.list_open_gates_by_type.return_value = [
        {"id": "g", "await_id": "notify.task_message"},
    ]
    await listener.bus.emit(mutation, {"gate_type": "event"})
    await listener.bus.emit("notify.task_message")
    listener._resolve_gate_and_emit.assert_awaited_once_with(
        "g", resolved_by="bus:event", resolution="notify.task_message",
    )


async def test_matching_event_gates_are_reread_before_resolution(event_gate_listener):
    listener = event_gate_listener
    listener.db.list_open_gates_by_type.return_value = [{"id": "g", "await_id": "custom"}]
    await listener.bus.emit("unrelated")
    listener.db.list_open_gates_by_type.return_value = []  # resolved outside the bus
    await listener.bus.emit("custom")
    assert listener.db.list_open_gates_by_type.await_count == 2
    listener._resolve_gate_and_emit.assert_not_awaited()


async def test_event_gate_interest_expires_and_recovers_missed_mutations(
    event_gate_listener, monkeypatch,
):
    listener = event_gate_listener
    clock = SimpleNamespace(monotonic=lambda: 10.0)
    monkeypatch.setattr("src.orchestrator.core.time", clock)
    await listener.bus.emit("custom")
    listener.db.list_open_gates_by_type.return_value = [{"id": "g", "await_id": "custom"}]
    clock.monotonic = lambda: 15.0
    await listener.bus.emit("custom")
    listener._resolve_gate_and_emit.assert_awaited_once()


async def test_event_gate_query_failure_does_not_cache_empty_interest(event_gate_listener):
    listener = event_gate_listener
    listener.db.list_open_gates_by_type.side_effect = [RuntimeError("unavailable"), []]
    await listener.bus.emit("custom")
    await listener.bus.emit("custom")
    assert listener.db.list_open_gates_by_type.await_count == 2


async def test_event_gate_mutation_during_query_keeps_cache_invalid(event_gate_listener):
    listener = event_gate_listener

    async def read_before_creation(gate_type):
        await listener.bus.emit("gate.created", {"gate_type": "event"})
        return []

    listener.db.list_open_gates_by_type.side_effect = read_before_creation
    await listener.bus.emit("custom")
    listener.db.list_open_gates_by_type.side_effect = None
    listener.db.list_open_gates_by_type.return_value = [{"id": "g", "await_id": "custom"}]
    await listener.bus.emit("custom")
    listener._resolve_gate_and_emit.assert_awaited_once()


# ── Promotion behaviour under the projection ─────────────────────────────


class TestProjectionAuthority:
    async def test_corrupt_projection_is_not_promoted(self, orch):
        from src.database.tables import tasks as tasks_t

        await mktask(orch, "dep", status=TaskStatus.COMPLETED)
        await mktask(orch, "t")
        await orch.db.add_dependency("t", "dep")
        async with orch.db._engine.begin() as conn:
            await conn.execute(tasks_t.update().where(tasks_t.c.id == "t").values(is_blocked=True))

        await orch._check_defined_tasks()
        assert await status_of(orch, "t") == TaskStatus.DEFINED



class TestProjectionPromotion:
    async def test_defined_with_no_deps_promotes(self, orch):
        await mktask(orch, "t")
        await orch._check_defined_tasks()
        assert await status_of(orch, "t") == TaskStatus.READY

    async def test_failure_blocked_stays_put(self, orch):
        await mktask(orch, "t", status=TaskStatus.BLOCKED)
        await orch._check_defined_tasks()
        assert await status_of(orch, "t") == TaskStatus.BLOCKED

    async def test_graph_blocked_recovers(self, orch):
        await mktask(orch, "dep", status=TaskStatus.COMPLETED)
        await mktask(orch, "t", status=TaskStatus.BLOCKED)
        await orch.db.add_dependency("t", "dep")
        await orch._check_defined_tasks()
        assert await status_of(orch, "t") == TaskStatus.READY

    async def test_fan_in_finalizer_waits_for_late_children(self, orch):
        await mktask(orch, "container", status=TaskStatus.IN_PROGRESS)
        await mktask(orch, "finalize")
        await orch.db.add_dependency("finalize", "container", DepType.WAITS_FOR.value)
        await mktask(orch, "worker", status=TaskStatus.READY)
        await orch.db.add_dependency("worker", "container", DepType.PARENT_CHILD.value)

        await orch._check_defined_tasks()
        assert await status_of(orch, "finalize") == TaskStatus.DEFINED

        await orch.db.transition_task("worker", TaskStatus.COMPLETED)
        await orch._check_defined_tasks()
        assert await status_of(orch, "finalize") == TaskStatus.READY


# ── Conditional auto-close ───────────────────────────────────────────────


class TestConditionalAutoClose:
    async def _contingency(self, orch):
        await mktask(orch, "primary", status=TaskStatus.COMPLETED)
        await mktask(orch, "contingency")
        await orch.db.add_dependency("contingency", "primary", DepType.CONDITIONAL_BLOCKS.value)

    async def test_dead_contingency_is_closed_as_a_no_op(self, orch):
        await self._contingency(orch)
        await orch._check_defined_tasks()
        assert await status_of(orch, "contingency") == TaskStatus.COMPLETED
        assert await orch.db.get_task_meta("contingency", "work_outcome") == "no-op"

    async def test_it_emits_task_skipped_conditional(self, orch):
        await self._contingency(orch)
        await orch._check_defined_tasks()
        events = await orch.db.get_recent_events(limit=50, task_id="contingency")
        assert "task.skipped_conditional" in [e["event_type"] for e in events]

    async def test_the_flag_turns_it_off(self, orch):
        orch.config.work_graph.conditional_autoclose = False
        await self._contingency(orch)
        await orch._check_defined_tasks()
        assert await status_of(orch, "contingency") == TaskStatus.DEFINED

    async def test_a_live_contingency_survives(self, orch):
        await mktask(orch, "primary", status=TaskStatus.IN_PROGRESS)
        await mktask(orch, "contingency")
        await orch.db.add_dependency("contingency", "primary", DepType.CONDITIONAL_BLOCKS.value)
        await orch._check_defined_tasks()
        assert await status_of(orch, "contingency") == TaskStatus.DEFINED

    async def test_a_fired_contingency_is_promoted_not_closed(self, orch):
        await mktask(orch, "primary", status=TaskStatus.FAILED, retry_count=3, max_retries=3)
        await mktask(orch, "contingency")
        await orch.db.add_dependency("contingency", "primary", DepType.CONDITIONAL_BLOCKS.value)
        await orch._check_defined_tasks()
        assert await status_of(orch, "contingency") == TaskStatus.READY


# ── Gate sweep re-gating (finding #2) ────────────────────────────────────


class TestGateSweepGating:
    async def test_sweep_is_gated_only_on_its_interval(self, orch):
        import inspect

        source = inspect.getsource(orch._sweep_gates)
        assert "gate_sweep_interval_seconds" in source

    async def test_sweep_can_be_disabled_by_interval_zero(self, orch):
        orch.config.work_graph.gate_sweep_interval_seconds = 0
        assert orch.config.work_graph.validate() == []
        # Create a timer that would otherwise fire — disabled sweep must not.
        await mktask(orch, "t")
        import time as _t

        await orch.db.create_gate(
            "p-1", "timer", "would-fire",
            timeout_at=_t.time() - 10, waiter_task_ids=["t"],
        )
        await orch._sweep_gates()
        gate = (await orch.db.list_gates(project_id="p-1"))[0]
        assert gate["status"] == "open"

    async def test_sweep_respects_the_rate_limit(self, orch):
        orch.config.work_graph.gate_sweep_interval_seconds = 30
        orch._last_gate_sweep = 1e12  # far in the future
        # Overdue gate that a real sweep would expire.
        import time as _t

        gid, _ = await orch.db.create_gate(
            "p-1", "human", "past", timeout_at=_t.time() - 10
        )
        await orch._sweep_gates()
        assert (await orch.db.get_gate(gid))["status"] == "open"


class TestGateSweepBehavior:
    """WG-3: sweep resolves and expires gates and unblocks waiters."""

    @pytest.fixture(autouse=True)
    def _reset_throttle(self, orch):
        orch._last_gate_sweep = 0.0

    async def test_timer_gate_resolves_when_clock_passes(self, orch):
        import time as _t

        await mktask(orch, "t")
        gid, _ = await orch.db.create_gate(
            "p-1", "timer", "fire", timeout_at=_t.time() - 1, waiter_task_ids=["t"]
        )
        assert (await orch.db.get_task("t")).is_blocked is True
        await orch._sweep_gates()
        assert (await orch.db.get_gate(gid))["status"] == "resolved"
        assert (await orch.db.get_task("t")).is_blocked is False

    async def test_task_gate_resolves_on_dep_completion(self, orch):
        await mktask(orch, "dep", status=TaskStatus.COMPLETED)
        await mktask(orch, "t")
        gid, _ = await orch.db.create_gate(
            "p-1", "task", "wait dep", await_id="dep", waiter_task_ids=["t"]
        )
        assert (await orch.db.get_task("t")).is_blocked is True
        await orch._sweep_gates()
        assert (await orch.db.get_gate(gid))["status"] == "resolved"
        assert (await orch.db.get_task("t")).is_blocked is False

    async def test_resolve_emits_await_id_on_bus(self, orch):
        """gate.resolved payload must include the gate's await_id so
        pipelines can fan-in on it (e.g. commit-on-gate-resolve)."""
        captured: list[dict] = []

        def _cap(data):
            captured.append(data)

        orch.bus.subscribe("gate.resolved", _cap)

        await mktask(orch, "t")
        gid, _ = await orch.db.create_gate(
            "p-1", "human", "approve", await_id="prop-abc123", waiter_task_ids=["t"]
        )
        await orch._resolve_gate_and_emit(gid, resolved_by="tester", resolution="ok")

        assert captured, "no gate.resolved emitted"
        assert captured[-1].get("await_id") == "prop-abc123"

    async def test_task_gate_stays_open_while_dep_not_completed(self, orch):
        await mktask(orch, "dep", status=TaskStatus.IN_PROGRESS)
        await mktask(orch, "t")
        gid, _ = await orch.db.create_gate(
            "p-1", "task", "wait dep", await_id="dep", waiter_task_ids=["t"]
        )
        await orch._sweep_gates()
        assert (await orch.db.get_gate(gid))["status"] == "open"

    async def test_expiry_marks_overdue_but_keeps_waiter_blocked(self, orch):
        import time as _t

        await mktask(orch, "t")
        gid, _ = await orch.db.create_gate(
            "p-1", "human", "past", timeout_at=_t.time() - 1, waiter_task_ids=["t"]
        )
        await orch._sweep_gates()
        assert (await orch.db.get_gate(gid))["status"] == "expired"
        # Waiter stays blocked — expiry never silently self-approves.
        assert (await orch.db.get_task("t")).is_blocked is True

    async def test_pr_merged_gate_resolves_via_stub_poller(self, orch, monkeypatch):
        await mktask(orch, "t")
        gid, _ = await orch.db.create_gate(
            "p-1", "pr-merged", "PR", await_id="https://gh/pr/1", waiter_task_ids=["t"]
        )

        async def fake_poll(pr_url, *, project_id=None):
            return True

        monkeypatch.setattr(orch, "_poll_pr_merged", fake_poll)

        async def fake_reached_default(pr_url, *, project_id=None):
            assert (pr_url, project_id) == ("https://gh/pr/1", "p-1")
            return True

        monkeypatch.setattr(orch, "_pr_reached_default_branch", fake_reached_default)
        await orch._sweep_gates()
        assert (await orch.db.get_gate(gid))["status"] == "resolved"
        assert (await orch.db.get_task("t")).is_blocked is False

    async def test_pr_merged_gate_stays_open_while_pr_open(self, orch, monkeypatch):
        await mktask(orch, "t")
        gid, _ = await orch.db.create_gate(
            "p-1", "pr-merged", "PR", await_id="https://gh/pr/1", waiter_task_ids=["t"]
        )

        async def fake_poll(pr_url, *, project_id=None):
            return False

        monkeypatch.setattr(orch, "_poll_pr_merged", fake_poll)
        await orch._sweep_gates()
        assert (await orch.db.get_gate(gid))["status"] == "open"

    async def test_event_gate_resolves_via_sweep_backstop(self, orch):
        """A persisted matching event *after* gate creation resolves the gate
        even when the live bus subscription is not active."""
        await mktask(orch, "t")
        gid, _ = await orch.db.create_gate(
            "p-1", "event", "await-deploy", await_id="deploy.completed",
            waiter_task_ids=["t"],
        )
        # Persist a matching event after gate creation.
        await orch.db.log_event("deploy.completed", project_id="p-1", payload="x")
        await orch._sweep_gates()
        assert (await orch.db.get_gate(gid))["status"] == "resolved"

    async def test_event_gate_resolves_via_bus_subscription(self, orch):
        """Live path: an emitted event resolves an open event-gate
        immediately, without waiting for the sweep."""
        await orch._subscribe_event_gates()
        await mktask(orch, "t")
        # Create through the command boundary so the live listener receives
        # the committed gate.created notification, including on a warm cache.
        handler = CommandHandler(orch, orch.config)
        result = await handler.execute("gate_create", {
            "project_id": "p-1", "gate_type": "event", "title": "await-x",
            "await_id": "my.custom", "waiter_task_ids": ["t"],
        })
        assert result["success"] is True
        gid = result["gate_id"]
        await orch.bus.emit("my.custom", {"project_id": "p-1"})
        # give bus a chance to drain (sync-dispatch in tests, but async safety)
        import asyncio as _a

        await _a.sleep(0)
        assert (await orch.db.get_gate(gid))["status"] == "resolved"

    async def test_resolved_gate_unblocks_waiter_in_same_cycle(
        self, orch, monkeypatch
    ):
        """Sweep runs at step 2b, before ``_check_defined_tasks`` — a
        freshly resolved gate must let the waiter promote in one cycle.

        This test drives the *real* cascade (``run_one_cycle``) and asserts
        both (a) that ``_sweep_gates`` was called before
        ``_check_defined_tasks`` (order), and (b) that the waiter reaches
        READY in the same tick (behaviour).  The prior version called the
        two methods by hand and would silently pass if the cascade wired
        them in the wrong order.
        """
        import time as _t

        await mktask(orch, "t")
        await orch.db.create_gate(
            "p-1", "timer", "fire", timeout_at=_t.time() - 1, waiter_task_ids=["t"]
        )

        call_order: list[str] = []
        real_sweep = orch._sweep_gates
        real_check = orch._check_defined_tasks

        async def _wrap_sweep():
            call_order.append("sweep")
            await real_sweep()

        async def _wrap_check():
            call_order.append("check_defined")
            await real_check()

        monkeypatch.setattr(orch, "_sweep_gates", _wrap_sweep)
        monkeypatch.setattr(orch, "_check_defined_tasks", _wrap_check)
        # Ensure the sweep isn't rate-limited away.
        orch._last_gate_sweep = 0

        await orch.run_one_cycle()

        # Order: sweep must precede _check_defined_tasks so a resolved
        # gate lets its waiter promote inside the same tick.
        assert "sweep" in call_order and "check_defined" in call_order
        assert call_order.index("sweep") < call_order.index("check_defined")
        # Behaviour: the waiter promoted to READY on this cycle.
        assert (await orch.db.get_task("t")).status == TaskStatus.READY


@pytest.mark.parametrize("reason", ["stuck_timeout", "session_rapid_crash", "prepare_timeout"])
async def test_attention_blocked_task_does_not_auto_recover(orch, reason):
    await mktask(orch, "dep", status=TaskStatus.COMPLETED)
    for tid in ("needs-operator", "ordinary-dependent"):
        await mktask(orch, tid, status=TaskStatus.BLOCKED)
        await orch.db.add_dependency(tid, "dep")
    await orch.db.set_task_meta("needs-operator", "needs_attention", reason)

    for _ in range(2):
        await orch._check_defined_tasks()
    assert await status_of(orch, "needs-operator") == TaskStatus.BLOCKED
    assert await status_of(orch, "ordinary-dependent") == TaskStatus.READY
    assert await orch.db.get_task_meta("needs-operator", "needs_attention") == reason

    # Explicitly clearing the attention hold restores ordinary dependency recovery.
    await orch.db.delete_task_meta("needs-operator", "needs_attention")
    await orch._check_defined_tasks()
    assert await status_of(orch, "needs-operator") == TaskStatus.READY


async def test_edit_task_empty_needs_attention_clears_the_hold(orch):
    """``edit_task`` with ``needs_attention=""`` must delete the meta key, not
    store an empty code — monitoring's BLOCKED filter is presence-based, so a
    stored empty string would hold the task BLOCKED forever (regression for
    the truthiness-vs-presence review finding).
    """
    handler = CommandHandler(orch, orch.config)
    tid = await mktask(orch, "b", status=TaskStatus.BLOCKED)

    result = await handler.execute("edit_task", {"task_id": tid, "needs_attention": "stuck"})
    assert "error" not in result
    assert await orch.db.get_task_meta(tid, "needs_attention") == "stuck"

    result = await handler.execute("edit_task", {"task_id": tid, "needs_attention": ""})
    assert "error" not in result
    assert await orch.db.get_task_meta(tid, "needs_attention") is None
