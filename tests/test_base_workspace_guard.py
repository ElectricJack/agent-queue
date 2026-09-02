"""The base checkout is never handed to a session.

Three layers, one rule (see :mod:`src.orchestrator.base_workspace`):

1. identification — a base is a non-slot row that at least one slot names
   as its base, which is a fact about the rows and not about config;
2. the launch guard — a ``work_dir`` that resolves to a base is refused
   unless the profile sets ``allow_base_checkout: true``;
3. the doctor check — ``workspaces.base_sessions`` reports anything already
   running in one, including launches the guard never saw.
"""

from __future__ import annotations

import os
import time

import pytest

from src.database import Database
from src.doctor.models import Severity
from src.doctor.workspace_checks import run_check
from src.models import (
    AgentProfile,
    Project,
    RepoSourceType,
    SessionRecord,
    Workspace,
)
from src.orchestrator.base_workspace import (
    base_checkout_refusal,
    base_workspaces,
    list_base_workspaces,
)


def _now() -> float:
    return time.time()


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "base.db"))
    await database.initialize()
    await database.create_project(Project(id="p1", name="p1"))
    yield database
    await database.close()


async def _base(db, path: str, ws_id: str = "ws-base") -> Workspace:
    ws = Workspace(
        id=ws_id,
        project_id="p1",
        workspace_path=path,
        source_type=RepoSourceType.LINK,
        kind_id="project-repo",
    )
    await db.create_workspace(ws)
    return ws


async def _slot(db, path: str, index: int, base_id: str = "ws-base") -> Workspace:
    ws = Workspace(
        id=f"ws-slot-{index}",
        project_id="p1",
        workspace_path=path,
        source_type=RepoSourceType.WORKTREE,
        kind_id="project-repo",
        slot_index=index,
        base_workspace_id=base_id,
    )
    await db.create_workspace(ws)
    return ws


class TestBaseIdentification:
    async def test_a_row_with_slots_is_a_base(self, db, tmp_path):
        await _base(db, str(tmp_path / "checkout"))
        await _slot(db, str(tmp_path / "checkout" / ".aq" / "worktrees" / "slot-0"), 0)
        assert [ws.id for ws in await list_base_workspaces(db)] == ["ws-base"]

    async def test_a_lone_clone_is_not_a_base(self, db, tmp_path):
        """With ``worktrees.enabled: false`` no slots exist, so no row is a
        base and the guard refuses nothing."""
        await _base(db, str(tmp_path / "checkout"))
        assert await list_base_workspaces(db) == []

    async def test_a_slot_is_never_a_base(self, db, tmp_path):
        await _base(db, str(tmp_path / "checkout"))
        slot = await _slot(db, str(tmp_path / "checkout" / "slot-0"), 0)
        assert slot.id not in {ws.id for ws in await list_base_workspaces(db)}

    def test_base_workspaces_is_pure(self, tmp_path):
        base = Workspace(
            id="b", project_id="p1", workspace_path="/repo",
            source_type=RepoSourceType.LINK, kind_id="project-repo",
        )
        slot = Workspace(
            id="s", project_id="p1", workspace_path="/repo/slot-0",
            source_type=RepoSourceType.WORKTREE, kind_id="project-repo",
            slot_index=0, base_workspace_id="b",
        )
        assert [ws.id for ws in base_workspaces([base, slot])] == ["b"]


class TestLaunchRefusal:
    async def test_base_work_dir_is_refused(self, db, tmp_path):
        base_path = str(tmp_path / "checkout")
        await _base(db, base_path)
        await _slot(db, str(tmp_path / "checkout" / "slot-0"), 0)
        profile = AgentProfile(id="reviewer", name="reviewer", read_only=True)

        refusal = await base_checkout_refusal(db, base_path, profile, project_id="p1")
        assert refusal is not None
        assert base_path in refusal
        assert "allow_base_checkout" in refusal

    async def test_slot_work_dir_is_allowed(self, db, tmp_path):
        slot_path = str(tmp_path / "checkout" / "slot-0")
        await _base(db, str(tmp_path / "checkout"))
        await _slot(db, slot_path, 0)
        profile = AgentProfile(id="worker", name="worker")

        assert await base_checkout_refusal(db, slot_path, profile, project_id="p1") is None

    async def test_symlinked_path_to_the_base_is_still_refused(self, db, tmp_path):
        real = tmp_path / "checkout"
        real.mkdir()
        link = tmp_path / "link-to-checkout"
        os.symlink(real, link)
        await _base(db, str(real))
        await _slot(db, str(real / "slot-0"), 0)
        profile = AgentProfile(id="worker", name="worker")

        assert await base_checkout_refusal(db, str(link), profile, project_id="p1") is not None

    async def test_opt_in_profile_may_run_in_the_base(self, db, tmp_path):
        base_path = str(tmp_path / "checkout")
        await _base(db, base_path)
        await _slot(db, str(tmp_path / "checkout" / "slot-0"), 0)
        profile = AgentProfile(id="maintenance", name="maintenance", allow_base_checkout=True)

        assert await base_checkout_refusal(db, base_path, profile, project_id="p1") is None

    async def test_empty_work_dir_is_not_this_guard_s_problem(self, db):
        profile = AgentProfile(id="worker", name="worker")
        assert await base_checkout_refusal(db, "", profile, project_id="p1") is None


class TestDoctorCheck:
    async def _session(self, db, work_dir: str, sid: str = "s1", state: str = "running"):
        await db.create_session(
            SessionRecord(
                id=sid,
                project_id="p1",
                profile_id="reviewer",
                harness="claude",
                provider="tmux",
                name=f"n-{sid}",
                lifecycle="task",
                work_dir=work_dir,
                epoch=1,
                instance_token="tok" + sid,
                started_at=_now(),
                state=state,
            )
        )

    async def test_flags_a_session_running_in_the_base(self, db, tmp_path):
        base_path = str(tmp_path / "checkout")
        await _base(db, base_path)
        await _slot(db, str(tmp_path / "checkout" / "slot-0"), 0)
        await self._session(db, base_path)

        result = await run_check(db, "workspaces.base_sessions")
        assert result.severity is Severity.ERROR
        assert result.data["count"] == 1
        assert result.data["sessions"][0]["workspace_id"] == "ws-base"

    async def test_ok_when_every_session_is_in_a_slot(self, db, tmp_path):
        slot_path = str(tmp_path / "checkout" / "slot-0")
        await _base(db, str(tmp_path / "checkout"))
        await _slot(db, slot_path, 0)
        await self._session(db, slot_path)

        result = await run_check(db, "workspaces.base_sessions")
        assert result.severity is Severity.OK

    async def test_ignores_dead_sessions(self, db, tmp_path):
        base_path = str(tmp_path / "checkout")
        await _base(db, base_path)
        await _slot(db, str(tmp_path / "checkout" / "slot-0"), 0)
        await self._session(db, base_path, state="stopped")

        result = await run_check(db, "workspaces.base_sessions")
        assert result.severity is Severity.OK

    async def test_registered_in_the_default_registry(self):
        from src.doctor import default_registry

        assert "workspaces.base_sessions" in {c.id for c in default_registry().checks()}


class TestProfileOptIn:
    """``allow_base_checkout`` has to survive markdown → dict → row → object."""

    def test_parser_accepts_the_key(self):
        from src.profiles.parser import parse_profile, parsed_profile_to_agent_profile

        parsed = parse_profile(
            "---\nid: maintenance\nname: maintenance\n---\n\n"
            "## Config\n\n```json\n{\n"
            '  "harness": "claude",\n'
            '  "allow_base_checkout": true\n'
            "}\n```\n\n## Role\n\nHousekeeping.\n"
        )
        assert parsed.errors == []
        assert parsed_profile_to_agent_profile(parsed)["allow_base_checkout"] is True

    def test_parser_rejects_a_non_boolean(self):
        from src.profiles.parser import parse_profile

        parsed = parse_profile(
            "---\nid: maintenance\nname: maintenance\n---\n\n"
            "## Config\n\n```json\n{\n"
            '  "allow_base_checkout": "yes"\n'
            "}\n```\n\n## Role\n\nHousekeeping.\n"
        )
        assert any("allow_base_checkout" in e for e in parsed.errors)

    def test_defaults_to_false(self):
        assert AgentProfile(id="p", name="p").allow_base_checkout is False

    async def test_round_trips_through_the_database(self, db):
        await db.upsert_profile(
            AgentProfile(id="maintenance", name="maintenance", allow_base_checkout=True)
        )
        assert (await db.get_profile("maintenance")).allow_base_checkout is True
        await db.upsert_profile(AgentProfile(id="plain", name="plain"))
        assert (await db.get_profile("plain")).allow_base_checkout is False


class TestLaunchPathRefuses:
    """The guard is wired into the orchestrator's task-launch path.

    Unit-testing ``base_checkout_refusal`` proves the rule; this proves the
    rule is actually consulted before a provider is started.
    """

    async def _orchestrator(self, tmp_path, db):
        from unittest.mock import MagicMock

        from src.config import AppConfig, DiscordConfig
        from src.orchestrator import Orchestrator

        config = AppConfig(
            discord=DiscordConfig(bot_token="t", guild_id="1"),
            workspace_dir=str(tmp_path / "w"),
            database_path=str(tmp_path / "base.db"),
            data_dir=str(tmp_path / "d"),
        )
        orch = Orchestrator(config)
        orch.db = db
        orch.harness_registry = MagicMock()
        orch.harness_registry.get.return_value = MagicMock(id="claude")
        orch.session_providers = MagicMock()
        return orch

    async def _assigned_task(self, db):
        from src.models import Agent, AgentState, Task, TaskStatus

        await db.upsert_profile(
            AgentProfile(id="reviewer", name="reviewer", harness="claude", read_only=True)
        )
        await db.create_agent(
            Agent(id="a1", name="a1", profile_id="reviewer", state=AgentState.BUSY)
        )
        task = Task(
            id="t1", project_id="p1", title="review", description="",
            profile_id="reviewer", created_at=_now(), updated_at=_now(),
        )
        await db.create_task(task)
        await db.update_task(
            task.id, status=TaskStatus.ASSIGNED, assigned_agent_id="a1"
        )
        return await db.get_task(task.id)

    async def test_launch_is_refused_before_the_provider_starts(self, db, tmp_path):
        from unittest.mock import MagicMock

        from src.scheduler import AssignAction

        base_path = str(tmp_path / "checkout")
        await _base(db, base_path)
        await _slot(db, str(tmp_path / "checkout" / "slot-0"), 0)
        task = await self._assigned_task(db)
        orch = await self._orchestrator(tmp_path, db)

        refusals: list[str] = []

        async def fake_fail(action, failed_task, reason, stderr_path=None):
            refusals.append(reason)

        orch._fail_session_launch = fake_fail

        # Routing is a different concern: short-circuit it so the only
        # verdict left to observe is the base-checkout one.
        async def routed(t):
            return t, None

        async def no_mismatch(*a, **kw):
            return None

        orch._effective_assignment_task = routed
        orch._check_agent_routing = no_mismatch
        provider = MagicMock()
        orch.session_providers.create.return_value = provider

        action = AssignAction(task_id=task.id, agent_id="a1", project_id="p1")
        await orch._launch_session_for_task_locked(action, task, None, base_path)

        assert refusals and "base checkout" in refusals[0]
        provider.start.assert_not_called()
        assert await db.list_sessions() == []
