"""SessionQueryMixin — CRUD, ranked name resolution, atomic restart bump.

See docs/specs/implementation/session-runtime.md §2.3.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from src.database import Database
from src.database.queries.session_queries import InvalidSessionTransition
from src.models import Project, SessionRecord


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    await database.create_project(Project(id="p1", name="P1"))
    yield database
    await database.close()


def _session(**overrides) -> SessionRecord:
    base = dict(
        id="sess1",
        project_id="p1",
        profile_id="claude-opus",
        harness="claude",
        provider="fake",
        name="s-task1",
        lifecycle="task",
        work_dir="/tmp/wd",
        epoch="epoch1",
        instance_token="tok1",
        started_at=time.time(),
        task_id=None,
        state="running",
    )
    base.update(overrides)
    return SessionRecord(**base)


class TestCrud:
    async def test_create_then_get_round_trips_every_column(self, db):
        row = _session(
            session_key="resume-abc",
            last_activity=123.5,
            restarts=2,
            quarantined_at=99.0,
            sleep_reason="rate_limit",
        )
        await db.create_session(row)
        got = await db.get_session("sess1")
        assert got == row

    async def test_get_missing_returns_none(self, db):
        assert await db.get_session("nope") is None

    async def test_update_session_changes_only_named_columns(self, db):
        await db.create_session(_session())
        assert await db.update_session("sess1", state="stopped") == 1
        got = await db.get_session("sess1")
        assert got.state == "stopped"
        assert got.instance_token == "tok1"

    async def test_update_with_no_fields_is_a_no_op(self, db):
        await db.create_session(_session())
        assert await db.update_session("sess1") == 0

    async def test_touch_activity(self, db):
        await db.create_session(_session())
        await db.touch_session_activity("sess1", 4242.0)
        assert (await db.get_session("sess1")).last_activity == 4242.0

    async def test_delete_session(self, db):
        await db.create_session(_session())
        await db.delete_session("sess1")
        assert await db.get_session("sess1") is None


class TestNameResolution:
    """``sessions.name`` is non-unique by design — see the mixin docstring."""

    async def test_live_row_wins_over_stopped_history(self, db):
        await db.create_session(
            _session(id="old", state="stopped", started_at=100.0, instance_token="t-old")
        )
        await db.create_session(
            _session(id="new", state="running", started_at=200.0, instance_token="t-new")
        )
        got = await db.get_session_by_name("s-task1")
        assert got.id == "new"

    async def test_newest_wins_among_equally_ranked_rows(self, db):
        await db.create_session(_session(id="a", state="stopped", started_at=100.0))
        await db.create_session(_session(id="b", state="stopped", started_at=300.0))
        assert (await db.get_session_by_name("s-task1")).id == "b"

    async def test_quarantined_outranks_stopped(self, db):
        await db.create_session(_session(id="a", state="stopped", started_at=500.0))
        await db.create_session(_session(id="b", state="quarantined", started_at=100.0))
        assert (await db.get_session_by_name("s-task1")).id == "b"

    async def test_get_session_for_task_uses_the_same_ranking(self, db):
        from src.models import Task

        await db.create_task(Task(id="t1", project_id="p1", title="T", description="d"))
        await db.create_session(
            _session(id="old", task_id="t1", state="stopped", started_at=100.0)
        )
        await db.create_session(
            _session(id="new", task_id="t1", state="running", started_at=200.0)
        )
        assert (await db.get_session_for_task("t1")).id == "new"

    async def test_restarting_the_same_name_is_allowed(self, db):
        """The stall ladder relaunches under the same name — that must work."""
        await db.create_session(_session(id="gen1", state="stopped"))
        await db.create_session(_session(id="gen2", state="running"))
        rows = await db.list_sessions(name="s-task1")
        assert len(rows) == 2


class TestListing:
    async def test_filters_compose(self, db):
        await db.create_session(_session(id="a", state="running", lifecycle="task"))
        await db.create_session(
            _session(id="b", state="sleeping", lifecycle="named", name="n-supervisor")
        )
        assert len(await db.list_sessions()) == 2
        assert len(await db.list_sessions(state="running")) == 1
        assert len(await db.list_sessions(lifecycle="named")) == 1
        assert len(await db.list_sessions(project_id="p1")) == 2
        assert len(await db.list_sessions(project_id="other")) == 0

    async def test_live_only_excludes_sleeping_and_stopped(self, db):
        for i, state in enumerate(
            ("starting", "running", "draining", "stopped", "sleeping", "quarantined")
        ):
            await db.create_session(_session(id=f"s{i}", state=state, name=f"s-{i}"))
        live = await db.list_sessions(live_only=True)
        assert {r.state for r in live} == {"starting", "running", "draining"}

    async def test_ordered_newest_first(self, db):
        await db.create_session(_session(id="a", started_at=100.0, name="s-a"))
        await db.create_session(_session(id="b", started_at=300.0, name="s-b"))
        assert [r.id for r in await db.list_sessions()] == ["b", "a"]


class TestStateMachine:
    """Illegal transitions raise instead of quietly corrupting the row.

    The design spec always described these edges; nothing enforced them.
    """

    async def _row(self, db, state="running", sid="sess1"):
        await db.create_session(_session(id=sid, state=state))
        return sid

    async def test_running_to_stopped_is_legal(self, db):
        sid = await self._row(db)
        await db.update_session(sid, state="stopped")
        assert (await db.get_session(sid)).state == "stopped"

    async def test_nothing_revives_a_stopped_row(self, db):
        """A restart makes a *new* row; reviving this one would put two live
        rows under one name and break create_session's invariant."""
        sid = await self._row(db, state="stopped")
        with pytest.raises(InvalidSessionTransition) as exc:
            await db.update_session(sid, state="running")
        assert exc.value.current == "stopped" and exc.value.requested == "running"
        assert (await db.get_session(sid)).state == "stopped"

    async def test_quarantine_is_terminal(self, db):
        """"Nothing auto-releases a quarantine" is now a constraint, not prose."""
        sid = await self._row(db, state="quarantined")
        for target in ("running", "sleeping", "draining", "stopped"):
            with pytest.raises(InvalidSessionTransition):
                await db.update_session(sid, state=target)

    async def test_draining_cannot_go_back_to_running(self, db):
        sid = await self._row(db, state="draining")
        with pytest.raises(InvalidSessionTransition):
            await db.update_session(sid, state="running")

    async def test_rewriting_the_same_state_is_a_no_op_not_a_violation(self, db):
        """Callers converge; converging twice is normal."""
        sid = await self._row(db, state="stopped")
        assert await db.update_session(sid, state="stopped") == 1

    async def test_other_fields_still_write_on_a_terminal_row(self, db):
        """Forensics keep accruing after the row stops moving."""
        sid = await self._row(db, state="quarantined")
        await db.update_session(sid, sleep_reason="rate_limit", restarts=4)
        row = await db.get_session(sid)
        assert row.sleep_reason == "rate_limit" and row.restarts == 4

    async def test_a_missing_row_is_not_a_transition_error(self, db):
        """That is the UPDATE's problem — it affects zero rows."""
        assert await db.update_session("nope", state="running") == 0

    async def test_intent_is_unguarded_but_validated(self, db):
        """Every intent is expressible from every state — that is the point."""
        sid = await self._row(db, state="stopped")
        await db.update_session(sid, desired_state="running")
        assert (await db.get_session(sid)).desired_state == "running"
        with pytest.raises(ValueError):
            await db.update_session(sid, desired_state="banana")


class TestRestartBump:
    async def test_bump_returns_new_value(self, db):
        await db.create_session(_session())
        assert await db.bump_session_restarts("sess1") == 1
        assert await db.bump_session_restarts("sess1") == 2
        assert (await db.get_session("sess1")).restarts == 2

    async def test_concurrent_bumps_do_not_lose_increments(self, db):
        """A read-modify-write would let two ticks both land on the same
        value and silently never reach the quarantine threshold."""
        await db.create_session(_session())
        await asyncio.gather(*(db.bump_session_restarts("sess1") for _ in range(5)))
        assert (await db.get_session("sess1")).restarts == 5

    async def test_bump_on_missing_row_returns_zero(self, db):
        assert await db.bump_session_restarts("nope") == 0
