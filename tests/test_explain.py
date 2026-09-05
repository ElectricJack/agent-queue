"""Work-graph WG-4: explain + ready frontier.

Covers docs/specs/implementation/work-graph.md §6.3 and §11:

* one golden per reason code (blocked_dependency / blocked_gate /
  no_idle_agent / workspace_locked / budget_exhausted / rate_limited /
  held),
* ``hold:*`` label exclusion from the ready frontier,
* cross-project blocking-dep naming,
* explain works between ticks via the cached ``SchedulerState``,
* ``_describe_task_blocker`` returns ``reasons[0]["detail"]`` formatting.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.commands.handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.explain import build_capacity_reasons
from src.intelligence_classes import IntelligenceClass
from src.models import Agent, AgentProfile, AgentState, Project, Task, TaskStatus
from src.orchestrator import Orchestrator


PROJECT_ID = "proj-explain"


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "explain.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT_ID, name="Explain"))
    yield database
    await database.close()


@pytest.fixture
def config(tmp_path):
    return AppConfig(
        discord=DiscordConfig(bot_token="test-token", guild_id="123"),
        workspace_dir=str(tmp_path / "workspaces"),
        database_path=str(tmp_path / "test.db"),
        data_dir=str(tmp_path / "data"),
    )


@pytest.fixture
async def handler(db, config):
    orch = Orchestrator(config)
    orch.db = db
    orch.git = MagicMock()
    orch.session_spec_builder._intelligence_classes = {
        "fast-low": IntelligenceClass(
            "fast-low", "Fast", "", {"anthropic": {"model": "claude-sonnet-5"}}
        )
    }
    await db.create_profile(AgentProfile(id="worker", name="Worker", harness="claude"))
    # The V2 assignment router resolves the routing playbook from a *ready*
    # activation row, so seed one for the shipped default: explain then reports
    # "awaiting_intelligence_route" rather than "assignment_playbook_unavailable".
    from src.playbooks.artifact_ref import ArtifactRef

    ref = ArtifactRef(
        playbook_id="default-assignment-routing",
        artifact_sha256="sha256:" + "a" * 64,
        schema_generation=2,
        contract_fingerprint="sha256:" + "b" * 64,
        source_digest="sha256:" + "c" * 64,
        compiler_build="test-build",
    )
    await db.upsert_playbook_artifact(
        ref, scope="system", path=f"/artifacts/{ref.digest}.json", size_bytes=1
    )
    await db.set_playbook_activation(
        playbook_id=ref.playbook_id,
        scope="system",
        scope_identifier="",
        artifact_sha256=ref.artifact_sha256,
        enabled=True,
        activated_by="test",
        health="ready",
        reasons="[]",
    )
    orch.playbook_manager = SimpleNamespace(get_scope_identifier=lambda _playbook_id: None)
    return CommandHandler(orch, config)


async def mktask(db, tid, project_id=PROJECT_ID, status=TaskStatus.DEFINED, **kw):
    await db.create_task(
        Task(id=tid, project_id=project_id, title=tid, description=tid, status=status, **kw)
    )
    return tid


def make_state(**kw):
    """Minimal SchedulerState stand-in for capacity-reason tests."""
    from src.scheduler import SchedulerState

    return SchedulerState(
        projects=kw.get("projects", []),
        tasks=kw.get("tasks", []),
        agents=kw.get("agents", []),
        project_token_usage=kw.get("project_token_usage", {}),
        project_active_agent_counts=kw.get("project_active_agent_counts", {}),
        tasks_completed_in_window={},
        project_available_workspaces=kw.get("project_available_workspaces", {}),
        workspace_locks=kw.get("workspace_locks", {}),
        global_budget=kw.get("global_budget"),
        global_tokens_used=kw.get("global_tokens_used", 0),
        provider_cooldowns=kw.get("provider_cooldowns", {}),
        project_constraints=kw.get("project_constraints", {}),
        now=kw.get("now", time.time()),
        affinity_wait_seconds=kw.get("affinity_wait_seconds", 60),
    )


# ── Golden per reason code ───────────────────────────────────────────────


class TestExplainCommand:
    async def test_unrouted_ready_task_reports_awaiting_intelligence_route(
        self, handler, db
    ):
        await mktask(db, "unrouted", status=TaskStatus.READY)

        res = await handler._cmd_explain_task({"task_id": "unrouted"})

        assert "awaiting_intelligence_route" in res["reason_codes"]
        assert res["assignment_route"] is None

    async def test_precise_route_failure_suppresses_generic_scheduler_reason(
        self, handler, db
    ):
        await mktask(db, "unavailable", status=TaskStatus.READY)
        # No ready activation: the router cannot resolve an assignment artifact.
        await db.set_playbook_activation(
            playbook_id="default-assignment-routing",
            scope="system",
            scope_identifier="",
            artifact_sha256="sha256:" + "a" * 64,
            enabled=False,
            activated_by="test",
            health="disabled",
            reasons="[]",
        )
        state = make_state(
            projects=[Project(id=PROJECT_ID, name="Explain")],
            project_available_workspaces={PROJECT_ID: 1},
        )
        state.assignment_routes = {}
        handler.orchestrator._last_scheduler_state = state
        handler.orchestrator._last_scheduler_workspace_counts = {PROJECT_ID: 1}
        handler.orchestrator._last_scheduler_idle_by_project = {PROJECT_ID: 1}

        res = await handler._cmd_explain_task({"task_id": "unavailable"})

        assert res["reason_codes"] == ["assignment_playbook_unavailable"]

    async def test_explicit_class_is_exposed_as_effective_assignment_route(
        self, handler, db
    ):
        await mktask(
            db,
            "explicit",
            status=TaskStatus.READY,
            intelligence_class="fast-low",
        )

        res = await handler._cmd_explain_task({"task_id": "explicit"})

        assert res["assignment_route"] == {
            "source": "explicit",
            "intelligence_class": "fast-low",
            "provider": None,
            "reason": None,
            "playbook_id": None,
            "playbook_version": None,
            "playbook_run_id": None,
            "freshness": "fresh",
        }
        assert "route_waiting_for_compatible_agent" in res["reason_codes"]

    async def test_blocked_dependency_reason(self, handler, db):
        await mktask(db, "dep")
        await mktask(db, "t")
        await db.add_dependency("t", "dep")
        res = await handler._cmd_explain_task({"task_id": "t"})
        assert res["success"] is True
        codes = [r["code"] for r in res["reasons"]]
        assert "blocked_dependency" in codes
        d = next(r for r in res["reasons"] if r["code"] == "blocked_dependency")
        assert d["ref"] == "dep"

    async def test_blocked_gate_reason(self, handler, db):
        await mktask(db, "t")
        gid, _ = await db.create_gate(PROJECT_ID, "human", "review", waiter_task_ids=["t"])
        res = await handler._cmd_explain_task({"task_id": "t"})
        assert any(r["code"] == "blocked_gate" and r["ref"] == gid for r in res["reasons"])

    async def test_hold_label_reason(self, handler, db):
        await mktask(db, "t", status=TaskStatus.READY)
        await db.add_task_label("t", "hold:alice")
        res = await handler._cmd_explain_task({"task_id": "t"})
        assert any(r["code"] == "held" and r["ref"] == "hold:alice" for r in res["reasons"])

    async def test_cross_project_dep_names_the_other_project(self, handler, db):
        other = "proj-other"
        await db.create_project(Project(id=other, name="Other"))
        await mktask(db, "far-dep", project_id=other)
        await mktask(db, "t")
        await db.add_dependency("t", "far-dep")
        res = await handler._cmd_explain_task({"task_id": "t"})
        d = next(r for r in res["reasons"] if r["code"] == "blocked_dependency")
        assert "proj-other" in d["detail"]

    async def test_unknown_task(self, handler):
        res = await handler._cmd_explain_task({"task_id": "no-such"})
        assert res["success"] is False

    async def test_missing_task_id(self, handler):
        res = await handler._cmd_explain_task({})
        assert res["success"] is False


# ── Capacity reasons (build_capacity_reasons golden set) ─────────────────


class TestExplainAfterASessionExitedWithoutClose:
    """"Why isn't X running" must answer for a worker that vanished.

    An exit-without-close used to land the task in BLOCKED with no logged
    transition and nothing in ``explain`` beyond the graph reasons — which
    were empty, because no dependency or gate was involved.  The task is now
    PAUSED on a backoff, and both halves of the story are named.
    """

    async def test_needs_attention_and_the_backoff_are_both_named(self, handler, db):
        await mktask(db, "exited", status=TaskStatus.PAUSED,
                     resume_after=time.time() + 120)
        await db.set_task_meta("exited", "needs_attention", "session_exited_open")

        res = await handler._cmd_explain_task({"task_id": "exited"})

        assert "needs_attention" in res["reason_codes"]
        assert "paused_backoff" in res["reason_codes"]
        details = {r["code"]: r["detail"] for r in res["reasons"]}
        assert details["needs_attention"] == "session_exited_open"
        assert "resumes automatically" in details["paused_backoff"]

    async def test_a_manual_pause_is_distinguished_from_a_backoff(self, handler, db):
        await mktask(db, "held", status=TaskStatus.PAUSED)

        res = await handler._cmd_explain_task({"task_id": "held"})

        assert "paused_manually" in res["reason_codes"]
        assert "paused_backoff" not in res["reason_codes"]


class TestBuildCapacityReasons:
    def test_no_idle_agent(self):
        proj = Project(id=PROJECT_ID, name="p")
        task = Task(id="t", project_id=PROJECT_ID, title="t", description="")
        state = make_state(projects=[proj], project_available_workspaces={PROJECT_ID: 1})
        codes = [r["code"] for r in build_capacity_reasons(task, state, {PROJECT_ID: 1}, {})]
        assert "no_idle_agent" in codes

    def test_workspace_locked(self):
        proj = Project(id=PROJECT_ID, name="p")
        task = Task(id="t", project_id=PROJECT_ID, title="t", description="")
        state = make_state(projects=[proj])
        codes = [r["code"] for r in build_capacity_reasons(task, state, {}, {PROJECT_ID: 2})]
        assert "workspace_locked" in codes

    def test_budget_exhausted(self):
        proj = Project(id=PROJECT_ID, name="p")
        task = Task(id="t", project_id=PROJECT_ID, title="t", description="")
        state = make_state(projects=[proj], global_budget=100, global_tokens_used=200)
        codes = [
            r["code"] for r in build_capacity_reasons(task, state, {PROJECT_ID: 1}, {PROJECT_ID: 1})
        ]
        assert "budget_exhausted" in codes

    def test_rate_limited(self):
        from src.models import Agent

        proj = Project(id=PROJECT_ID, name="p")
        task = Task(id="t", project_id=PROJECT_ID, title="t", description="")
        agent = Agent(
            id="a1",
            name="a1",
            profile_id="claude",
            state=AgentState.IDLE,
        )
        agent.project_id = PROJECT_ID  # type: ignore[attr-defined]
        now = time.time()
        state = make_state(
            projects=[proj],
            agents=[agent],
            provider_cooldowns={"claude": now + 100},
            now=now,
        )
        reasons = build_capacity_reasons(task, state, {PROJECT_ID: 1}, {PROJECT_ID: 1})
        codes = [r["code"] for r in reasons]
        assert "rate_limited" in codes

    def test_capacity_reasons_are_stably_ordered_and_rate_limit_is_deduplicated(self):
        project = Project(id=PROJECT_ID, name="p")
        task = Task(id="t", project_id=PROJECT_ID, title="t", description="")
        agents = [
            Agent(id=f"a{n}", name=f"a{n}", profile_id="claude", state=AgentState.IDLE)
            for n in range(2)
        ]
        now = 100.0
        state = make_state(
            projects=[project],
            agents=agents,
            global_budget=10,
            global_tokens_used=10,
            provider_cooldowns={"claude": now + 30},
            now=now,
        )
        reasons = build_capacity_reasons(task, state, {}, {PROJECT_ID: 0})
        assert [reason["code"] for reason in reasons] == [
            "workspace_locked",
            "no_idle_agent",
            "budget_exhausted",
            "rate_limited",
        ]
        assert reasons[-1]["ref"] == "claude"


# ── _describe_task_blocker uses reasons[0]["detail"] ─────────────────────


class TestDescribeTaskBlocker:
    async def test_reuses_capacity_reasons(self, handler):
        """The wrapper returns ``reasons[0]['detail']`` on match, and the
        capacity-ordering "ready but not picked" fallback otherwise."""
        proj = Project(id=PROJECT_ID, name="p")
        task = Task(id="t", project_id=PROJECT_ID, title="t", description="")
        state = make_state(projects=[proj], project_available_workspaces={PROJECT_ID: 0})
        s = handler.orchestrator._describe_task_blocker(task, state, {}, {PROJECT_ID: 0})
        assert "workspace" in s

    async def test_fallback(self, handler):
        proj = Project(id=PROJECT_ID, name="p")
        task = Task(id="t", project_id=PROJECT_ID, title="t", description="")
        state = make_state(projects=[proj], project_available_workspaces={PROJECT_ID: 1})
        s = handler.orchestrator._describe_task_blocker(
            task, state, {PROJECT_ID: 1}, {PROJECT_ID: 1}
        )
        assert s == "ready but not picked this tick (capacity/priority ordering)"


# ── _cmd_project_ready ───────────────────────────────────────────────────


class TestProjectReady:
    async def test_hold_labeled_tasks_excluded(self, handler, db):
        await mktask(db, "a", status=TaskStatus.READY)
        await mktask(db, "b", status=TaskStatus.READY)
        await db.add_task_label("a", "hold:me")
        res = await handler._cmd_project_ready({"project_id": PROJECT_ID})
        assert res["success"] is True
        ids = [r["task_id"] for r in res["ready"]]
        assert "b" in ids
        assert "a" not in ids
        # 'a' should show up in withheld with a `held` reason.
        held_reasons = [w for w in res["withheld"] if w["task_id"] == "a"]
        assert held_reasons
        assert any(r["code"] == "held" for r in held_reasons[0]["reasons"])

    async def test_frontier_includes_ready_unblocked(self, handler, db):
        await mktask(db, "r1", status=TaskStatus.READY)
        await mktask(db, "r2", status=TaskStatus.READY)
        res = await handler._cmd_project_ready({"project_id": PROJECT_ID})
        assert {t["task_id"] for t in res["ready"]} == {"r1", "r2"}

    async def test_missing_project_id(self, handler):
        res = await handler._cmd_project_ready({})
        assert res["success"] is False

    # -- profile_id filter (spec §14) ------------------------------------

    async def test_profile_id_filters_to_that_profile(self, handler, db):
        from src.models import AgentProfile

        await db.create_profile(AgentProfile(id="coder", name="Coder"))
        await db.create_profile(AgentProfile(id="reviewer", name="Reviewer"))
        await mktask(db, "c1", status=TaskStatus.READY, profile_id="coder")
        await mktask(db, "r1", status=TaskStatus.READY, profile_id="reviewer")

        res = await handler._cmd_project_ready({"project_id": PROJECT_ID, "profile_id": "coder"})
        assert [t["task_id"] for t in res["ready"]] == ["c1"]

    async def test_default_profile_also_gets_unassigned_tasks(self, handler, db):
        """Same widening as select_ready_for_profile: NULL profile counts."""
        from src.models import AgentProfile

        await db.create_profile(AgentProfile(id="coder", name="Coder"))
        await db.update_project(PROJECT_ID, default_profile_id="coder")
        await mktask(db, "c1", status=TaskStatus.READY, profile_id="coder")
        await mktask(db, "u1", status=TaskStatus.READY)  # profile_id is NULL

        res = await handler._cmd_project_ready({"project_id": PROJECT_ID, "profile_id": "coder"})
        assert {t["task_id"] for t in res["ready"]} == {"c1", "u1"}

    async def test_non_default_profile_does_not_get_unassigned_tasks(self, handler, db):
        from src.models import AgentProfile

        await db.create_profile(AgentProfile(id="coder", name="Coder"))
        await db.create_profile(AgentProfile(id="reviewer", name="Reviewer"))
        await db.update_project(PROJECT_ID, default_profile_id="coder")
        await mktask(db, "u1", status=TaskStatus.READY)

        res = await handler._cmd_project_ready({"project_id": PROJECT_ID, "profile_id": "reviewer"})
        assert res["ready"] == []

    # -- brief projection (spec §14) --------------------------------------

    async def test_brief_projection_shape(self, handler, db):
        from src.models import AgentProfile

        await db.create_profile(AgentProfile(id="coder", name="Coder"))
        await mktask(db, "b1", status=TaskStatus.READY, profile_id="coder", priority=42)

        res = await handler._cmd_project_ready({"project_id": PROJECT_ID, "brief": True})
        assert len(res["ready"]) == 1
        row = res["ready"][0]
        assert set(row) == {"id", "title", "status", "priority", "is_blocked", "profile_id"}
        assert row["id"] == "b1"
        assert row["status"] == "READY"
        assert row["priority"] == 42
        assert row["is_blocked"] is False
        assert row["profile_id"] == "coder"

    async def test_default_projection_unchanged(self, handler, db):
        await mktask(db, "d1", status=TaskStatus.READY)
        res = await handler._cmd_project_ready({"project_id": PROJECT_ID})
        assert set(res["ready"][0]) == {"task_id", "title", "priority"}

    async def test_brief_and_profile_id_compose(self, handler, db):
        from src.models import AgentProfile

        await db.create_profile(AgentProfile(id="coder", name="Coder"))
        await db.create_profile(AgentProfile(id="reviewer", name="Reviewer"))
        await mktask(db, "c1", status=TaskStatus.READY, profile_id="coder")
        await mktask(db, "r1", status=TaskStatus.READY, profile_id="reviewer")

        res = await handler._cmd_project_ready(
            {"project_id": PROJECT_ID, "profile_id": "reviewer", "brief": True}
        )
        assert [t["id"] for t in res["ready"]] == ["r1"]


# ── Explain works between ticks via cached SchedulerState ────────────────


class TestBetweenTicks:
    async def test_uses_cached_scheduler_state(self, handler, db):
        await mktask(db, "t", status=TaskStatus.READY)
        # No tick has run — cached snapshot is None; explain should still
        # return graph reasons (there are none) without erroring.
        res = await handler._cmd_explain_task({"task_id": "t"})
        assert res["success"] is True

        # Now install a synthetic cached state; explain must include capacity.
        proj = Project(id=PROJECT_ID, name="p")
        task_row = await db.get_task("t")
        state = make_state(projects=[proj], project_available_workspaces={PROJECT_ID: 0})
        handler.orchestrator._last_scheduler_state = state
        handler.orchestrator._last_scheduler_workspace_counts = {PROJECT_ID: 0}
        handler.orchestrator._last_scheduler_idle_by_project = {}
        res2 = await handler._cmd_explain_task({"task_id": task_row.id})
        codes = [r["code"] for r in res2["reasons"]]
        assert "workspace_locked" in codes


class TestExplainAfterATerminalClose:
    """A hard failure has no graph reason once the container is released.

    Without naming the terminal mark, "why isn't X running" answered with an
    empty graph for a task the cascade deliberately refuses to recover.
    """

    async def test_terminal_close_is_named(self, handler, db):
        await mktask(db, "hard", status=TaskStatus.IN_PROGRESS)
        await db.transition_task("hard", TaskStatus.BLOCKED, context="session_close_hard_failure")

        res = await handler._cmd_explain_task({"task_id": "hard"})

        assert "blocked_terminal" in res["reason_codes"]
        details = {r["code"]: r["detail"] for r in res["reasons"]}
        assert details["blocked_terminal"].startswith("session_close_hard_failure")
        assert "restart or reopen" in details["blocked_terminal"]
