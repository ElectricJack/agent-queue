"""Work-graph WG-1/WG-2: the promotion cascade and shadow mode.

Covers docs/specs/implementation/work-graph.md §6.2 and §11:

* legacy scan vs. ``is_blocked`` projection parity (shadow-mode assertion),
* which decider actually acts under ``blocked_state_authoritative``,
* divergence logging,
* the BLOCKED-recovery rule (failure-BLOCKED tasks stay put),
* ``parent-child`` replacing the ``is_plan_subtask`` special case,
* conditional auto-close behind ``conditional_autoclose``,
* the ``_sweep_gates`` re-gating.
"""

from __future__ import annotations

import logging

import pytest

from src.config import AppConfig
from src.models import DepType, Project, Task, TaskStatus
from src.orchestrator import Orchestrator


@pytest.fixture
async def orch(tmp_path):
    workspace = tmp_path / "workspaces"
    workspace.mkdir()
    config = AppConfig(
        database_path=str(tmp_path / "test.db"),
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


# ── Shadow-mode parity ───────────────────────────────────────────────────


class TestShadowParity:
    async def test_deciders_agree_on_a_plain_blocks_chain(self, orch):
        await mktask(orch, "a", status=TaskStatus.COMPLETED)
        await mktask(orch, "b")
        await mktask(orch, "c")
        await orch.db.add_dependency("b", "a")
        await orch.db.add_dependency("c", "b")

        defined = await orch.db.list_tasks(status=TaskStatus.DEFINED)
        blocked = await orch.db.list_tasks(status=TaskStatus.BLOCKED)
        legacy, deferred = await orch._legacy_promotion_decisions(defined, blocked)
        projected = await orch._projected_promotion_decisions(defined, blocked)
        assert deferred == set()
        assert set(legacy) == set(projected) == {"b"}

    async def test_deciders_agree_on_a_parent_child_plan_graph(self, orch):
        """The `parent-child` edge reproduces the old special case exactly."""
        await mktask(orch, "plan", status=TaskStatus.AWAITING_PLAN_APPROVAL)
        for child in ("s1", "s2"):
            await mktask(orch, child, parent_task_id="plan", is_plan_subtask=True)
            await orch.db.add_dependency(child, "plan", DepType.PARENT_CHILD.value)
            await orch.db.add_dependency(child, "plan", DepType.DISCOVERED_FROM.value)
        await orch.db.add_dependency("s2", "s1")  # chained

        async def decisions():
            defined = await orch.db.list_tasks(status=TaskStatus.DEFINED)
            blocked = await orch.db.list_tasks(status=TaskStatus.BLOCKED)
            legacy, deferred = await orch._legacy_promotion_decisions(defined, blocked)
            assert deferred == set(), "plan-subtask parent-child edges stay legible"
            return (
                set(legacy),
                set(await orch._projected_promotion_decisions(defined, blocked)),
            )

        # Unapproved plan: both deciders withhold every child.
        legacy, projected = await decisions()
        assert legacy == projected == set()

        # Approved (parent IN_PROGRESS): both release the head of the chain.
        await orch.db.transition_task("plan", TaskStatus.IN_PROGRESS)
        legacy, projected = await decisions()
        assert legacy == projected == {"s1"}

    async def test_deciders_agree_on_blocked_recovery(self, orch):
        await mktask(orch, "dep", status=TaskStatus.COMPLETED)
        await mktask(orch, "graph-blocked", status=TaskStatus.BLOCKED)
        await orch.db.add_dependency("graph-blocked", "dep")
        await mktask(orch, "failure-blocked", status=TaskStatus.BLOCKED)

        defined = await orch.db.list_tasks(status=TaskStatus.DEFINED)
        blocked = await orch.db.list_tasks(status=TaskStatus.BLOCKED)
        legacy, deferred = await orch._legacy_promotion_decisions(defined, blocked)
        projected = await orch._projected_promotion_decisions(defined, blocked)
        assert deferred == set()
        assert set(legacy) == set(projected) == {"graph-blocked"}


# ── Which decider acts ───────────────────────────────────────────────────


class TestAuthority:
    async def _classic_graph(self, orch):
        """A plain ``blocks`` chain — a graph both deciders fully understand."""
        await mktask(orch, "dep", status=TaskStatus.COMPLETED)
        await mktask(orch, "t")
        await orch.db.add_dependency("t", "dep")

    async def _corrupt_projection(self, orch, tid, value):
        """Force ``is_blocked`` out of sync, simulating a recompute bug."""
        from src.database.tables import tasks as tasks_t

        async with orch.db._engine.begin() as conn:
            await conn.execute(tasks_t.update().where(tasks_t.c.id == tid).values(is_blocked=value))

    async def test_shadow_mode_lets_the_legacy_scan_win(self, orch):
        await self._classic_graph(orch)
        await self._corrupt_projection(orch, "t", 1)
        assert orch.config.work_graph.blocked_state_authoritative is False
        await orch._check_defined_tasks()
        assert await status_of(orch, "t") == TaskStatus.READY

    async def test_flag_hands_authority_to_the_projection(self, orch):
        await self._classic_graph(orch)
        await self._corrupt_projection(orch, "t", 1)
        orch.config.work_graph.blocked_state_authoritative = True
        await orch._check_defined_tasks()
        assert await status_of(orch, "t") == TaskStatus.DEFINED

    async def test_divergence_is_logged(self, orch, caplog):
        await self._classic_graph(orch)
        await self._corrupt_projection(orch, "t", 1)
        with caplog.at_level(logging.WARNING, logger="src.orchestrator.monitoring"):
            await orch._check_defined_tasks()
        assert any("blocked-state divergence" in r.message for r in caplog.records)

    async def test_agreement_logs_nothing(self, orch, caplog):
        await self._classic_graph(orch)
        with caplog.at_level(logging.WARNING, logger="src.orchestrator.monitoring"):
            await orch._check_defined_tasks()
        assert not any("blocked-state divergence" in r.message for r in caplog.records)
        assert await status_of(orch, "t") == TaskStatus.READY

    async def test_typed_edges_are_deferred_not_counted_as_divergence(self, orch, caplog):
        """A `waits-for` task has no legacy opinion; it must not pollute the
        divergence signal, and it must still promote in shadow mode."""
        await mktask(orch, "container", status=TaskStatus.IN_PROGRESS)
        await mktask(orch, "finalize")
        await orch.db.add_dependency("finalize", "container", DepType.WAITS_FOR.value)

        defined = await orch.db.list_tasks(status=TaskStatus.DEFINED)
        blocked = await orch.db.list_tasks(status=TaskStatus.BLOCKED)
        _, deferred = await orch._legacy_promotion_decisions(defined, blocked)
        assert deferred == {"finalize"}

        with caplog.at_level(logging.WARNING, logger="src.orchestrator.monitoring"):
            await orch._check_defined_tasks()
        assert not any("blocked-state divergence" in r.message for r in caplog.records)
        assert await status_of(orch, "finalize") == TaskStatus.READY

    # -- divergence logging is edge-triggered (P2-7) and reports the
    #    deferred count (P2-8) ------------------------------------------

    def _warnings(self, caplog):
        return [r for r in caplog.records if r.levelno == logging.WARNING]

    async def test_a_persisting_divergence_is_logged_once(self, orch, caplog):
        """This runs every 5 s; one stuck plan parent must not emit 17 000
        identical WARNINGs a day."""
        with caplog.at_level(logging.INFO, logger="src.orchestrator.monitoring"):
            for _ in range(4):
                orch._log_promotion_divergence({"a": "deps_met"}, {}, set(), False)
        assert len(self._warnings(caplog)) == 1

    async def test_a_changed_divergence_is_logged_again(self, orch, caplog):
        with caplog.at_level(logging.INFO, logger="src.orchestrator.monitoring"):
            orch._log_promotion_divergence({"a": "deps_met"}, {}, set(), False)
            orch._log_promotion_divergence({"a": "deps_met"}, {}, set(), False)
            orch._log_promotion_divergence({"b": "deps_met"}, {}, set(), False)
        assert len(self._warnings(caplog)) == 2

    async def test_a_clearing_divergence_is_recorded(self, orch, caplog):
        """When it clears, the INFO line changes and the WARNING stops."""
        with caplog.at_level(logging.INFO, logger="src.orchestrator.monitoring"):
            orch._log_promotion_divergence({"a": "deps_met"}, {}, set(), False)
            caplog.clear()
            orch._log_promotion_divergence({}, {}, set(), False)
        assert self._warnings(caplog) == []
        assert any("0 legacy-only" in r.getMessage() for r in caplog.records)

    async def test_the_deferred_count_is_reported(self, orch, caplog):
        """Without it, "zero divergence for a week" and "the oracle judged
        nothing for a week" look identical in the log."""
        with caplog.at_level(logging.INFO, logger="src.orchestrator.monitoring"):
            orch._log_promotion_divergence({}, {}, {"x", "y"}, False)
        assert any("2 deferred to the projection" in r.getMessage() for r in caplog.records)
        assert self._warnings(caplog) == []


# ── Promotion behaviour under the projection ─────────────────────────────


class TestProjectionPromotion:
    @pytest.fixture(autouse=True)
    def _authoritative(self, orch):
        orch.config.work_graph.blocked_state_authoritative = True

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
    async def test_sweep_is_not_gated_on_the_blocked_state_flag(self, orch):
        """Flipping ``blocked_state_authoritative`` (rollout stage 2) must not
        arm the gate sweep (rollout stage 3)."""
        import inspect

        source = inspect.getsource(orch._sweep_gates)
        assert "blocked_state_authoritative" not in source.split('"""')[-1]
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

        gid = await orch.db.create_gate(
            "p-1", "human", "past", timeout_at=_t.time() - 10
        )
        await orch._sweep_gates()
        assert (await orch.db.get_gate(gid))["status"] == "open"


class TestGateSweepBehavior:
    """WG-3: sweep resolves and expires gates and unblocks waiters."""

    @pytest.fixture(autouse=True)
    def _reset_throttle(self, orch):
        orch._last_gate_sweep = 0.0
        orch.config.work_graph.blocked_state_authoritative = True

    async def test_timer_gate_resolves_when_clock_passes(self, orch):
        import time as _t

        await mktask(orch, "t")
        gid = await orch.db.create_gate(
            "p-1", "timer", "fire", timeout_at=_t.time() - 1, waiter_task_ids=["t"]
        )
        assert (await orch.db.get_task("t")).is_blocked is True
        await orch._sweep_gates()
        assert (await orch.db.get_gate(gid))["status"] == "resolved"
        assert (await orch.db.get_task("t")).is_blocked is False

    async def test_task_gate_resolves_on_dep_completion(self, orch):
        await mktask(orch, "dep", status=TaskStatus.COMPLETED)
        await mktask(orch, "t")
        gid = await orch.db.create_gate(
            "p-1", "task", "wait dep", await_id="dep", waiter_task_ids=["t"]
        )
        assert (await orch.db.get_task("t")).is_blocked is True
        await orch._sweep_gates()
        assert (await orch.db.get_gate(gid))["status"] == "resolved"
        assert (await orch.db.get_task("t")).is_blocked is False

    async def test_task_gate_stays_open_while_dep_not_completed(self, orch):
        await mktask(orch, "dep", status=TaskStatus.IN_PROGRESS)
        await mktask(orch, "t")
        gid = await orch.db.create_gate(
            "p-1", "task", "wait dep", await_id="dep", waiter_task_ids=["t"]
        )
        await orch._sweep_gates()
        assert (await orch.db.get_gate(gid))["status"] == "open"

    async def test_expiry_marks_overdue_but_keeps_waiter_blocked(self, orch):
        import time as _t

        await mktask(orch, "t")
        gid = await orch.db.create_gate(
            "p-1", "human", "past", timeout_at=_t.time() - 1, waiter_task_ids=["t"]
        )
        await orch._sweep_gates()
        assert (await orch.db.get_gate(gid))["status"] == "expired"
        # Waiter stays blocked — expiry never silently self-approves.
        assert (await orch.db.get_task("t")).is_blocked is True

    async def test_pr_merged_gate_resolves_via_stub_poller(self, orch, monkeypatch):
        await mktask(orch, "t")
        gid = await orch.db.create_gate(
            "p-1", "pr-merged", "PR", await_id="https://gh/pr/1", waiter_task_ids=["t"]
        )

        async def fake_poll(pr_url, *, project_id=None):
            return True

        monkeypatch.setattr(orch, "_poll_pr_merged", fake_poll)
        await orch._sweep_gates()
        assert (await orch.db.get_gate(gid))["status"] == "resolved"
        assert (await orch.db.get_task("t")).is_blocked is False

    async def test_check_pr_status_transitions_to_blocked_when_pr_closed(
        self, orch, monkeypatch
    ):
        """Regression: the no-workspace fallback used to swallow
        ``acheck_pr_merged``'s ``None`` (closed-without-merge) into
        ``False``, so closed-unmerged PRs never transitioned to BLOCKED.
        """
        await mktask(orch, "t", status=TaskStatus.AWAITING_APPROVAL)
        await orch.db.update_task("t", pr_url="https://gh/pr/9")

        # Force the no-per-task-workspace branch (per-task workspace
        # missing so we fall through to ``_poll_pr_merged``).
        async def _no_ws_for_task(_tid):
            return None

        async def _acheck(_path, _url):
            return None  # closed-unmerged

        monkeypatch.setattr(orch.db, "get_workspace_for_task", _no_ws_for_task)
        # Poll needs a workspace on the project (or any) to run gh;
        # stub ``list_workspaces`` so it looks like one exists.
        class _WS:
            workspace_path = "/tmp/x"

        async def _list_workspaces(project_id=None):
            return [_WS()]

        monkeypatch.setattr(orch.db, "list_workspaces", _list_workspaces)
        monkeypatch.setattr(orch.git, "acheck_pr_merged", _acheck, raising=False)

        # ``_check_pr_status`` calls ``_notify_stuck_chain`` / other helpers
        # on the BLOCKED path — stub the notifiers so we exercise only the
        # transition logic under test.
        async def _noop(*a, **k):
            return None

        monkeypatch.setattr(orch, "_emit_text_notify", _noop)
        monkeypatch.setattr(orch, "_emit_task_failure", _noop)
        monkeypatch.setattr(orch, "_notify_stuck_chain", _noop)
        monkeypatch.setattr(orch, "_resolve_profile", _noop)

        # Bypass the 60s throttle.
        orch._last_approval_check = 0
        await orch._check_awaiting_approval()

        # None from acheck_pr_merged → BLOCKED, not stuck at AWAITING_APPROVAL.
        assert (await orch.db.get_task("t")).status == TaskStatus.BLOCKED

    async def test_pr_merged_gate_stays_open_while_pr_open(self, orch, monkeypatch):
        await mktask(orch, "t")
        gid = await orch.db.create_gate(
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
        gid = await orch.db.create_gate(
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
        gid = await orch.db.create_gate(
            "p-1", "event", "await-x", await_id="my.custom", waiter_task_ids=["t"]
        )
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
