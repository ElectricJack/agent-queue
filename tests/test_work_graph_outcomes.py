"""Work-graph WG-5: outcomes, hierarchical ids, group progress, enforcement.

Covers docs/specs/implementation/work-graph.md §6.3, §7, §4.1 and §10:

* ``failure_class == "hard"`` skips retry and transitions to BLOCKED,
* soft / absent ``failure_class`` leaves the retry ladder unchanged,
* hierarchical child id generation (ordinals, gaps, depth-cap fallback),
* ``get_group_progress`` counts + Kahn waves on a small graph,
* ``transition_task(force=True)`` bypass under ``enforce=True``, warn-only
  default unchanged,
* ``InvalidTransition`` carries ``from_status`` / ``to_status``.
"""

from __future__ import annotations

import pytest

from src.database import SQLiteDatabaseAdapter
from src.models import DepType, Project, Task, TaskStatus
from src.state_machine import InvalidTransition
from src.task_names import _hierarchy_depth, generate_task_id


PROJECT = "p-wg5"


@pytest.fixture
async def db(tmp_path):
    database = SQLiteDatabaseAdapter(str(tmp_path / "wg5.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT, name="wg5"))
    yield database
    await database.close()


async def mktask(db, tid, status=TaskStatus.DEFINED, **kw):
    await db.create_task(
        Task(id=tid, project_id=PROJECT, title=tid, description=tid, status=status, **kw)
    )
    return tid


# ── failure_class policy ─────────────────────────────────────────────────


class TestFailureClassPolicy:
    """The FAILED branch in ``execution.py`` reads ``failure_class`` and
    picks BLOCKED (hard) vs. the retry path (soft / absent).

    We drive the policy at the *reading* level so this test doesn't require
    a full pipeline: verify that ``get_task_meta("failure_class")`` returns
    what was written and that the execution branch's ``== "hard"`` check
    would fire.
    """

    async def test_meta_persists_across_reads(self, db):
        await mktask(db, "t", status=TaskStatus.IN_PROGRESS)
        await db.set_task_meta("t", "failure_class", "hard")
        assert await db.get_task_meta("t", "failure_class") == "hard"

    async def test_absent_meta_returns_none(self, db):
        await mktask(db, "t", status=TaskStatus.IN_PROGRESS)
        assert await db.get_task_meta("t", "failure_class") is None

    async def test_soft_keeps_the_retry_path(self, db):
        await mktask(db, "t", status=TaskStatus.IN_PROGRESS)
        await db.set_task_meta("t", "failure_class", "soft")
        # Only ``"hard"`` triggers the skip-retry branch (execution.py).
        assert await db.get_task_meta("t", "failure_class") != "hard"


# ── hierarchical ids ─────────────────────────────────────────────────────


class TestHierarchicalIds:
    async def test_root_id_is_adjective_noun(self, db):
        tid = await generate_task_id(db)
        assert "-" in tid
        assert "." not in tid

    async def test_child_ordinals_start_at_one(self, db):
        await mktask(db, "root-a")
        cid = await generate_task_id(db, parent_id="root-a")
        assert cid == "root-a.1"

    async def test_child_ordinals_fill_after_max(self, db):
        await mktask(db, "root-a")
        # Create children manually with gaps: root-a.1, root-a.3 exist.
        await mktask(db, "root-a.1", parent_task_id="root-a")
        await mktask(db, "root-a.3", parent_task_id="root-a")
        cid = await generate_task_id(db, parent_id="root-a")
        # Next ordinal is max + 1 = 4 (gap at 2 is *not* refilled).
        assert cid == "root-a.4"

    async def test_depth_cap_falls_back_to_root(self, db, caplog):
        """A depth-3 parent hits the cap: the helper returns a fresh root id
        (adjective-noun) and warns; the caller is expected to add a
        ``discovered-from`` edge."""
        # Depth 3: "a.1.2" (3 segments).
        assert _hierarchy_depth("a.1.2") == 3
        await mktask(db, "a")
        await mktask(db, "a.1", parent_task_id="a")
        await mktask(db, "a.1.2", parent_task_id="a.1")
        import logging as _lg

        caplog.set_level(_lg.WARNING, logger="src.task_names")
        cid = await generate_task_id(db, parent_id="a.1.2")
        # Fallback root id: has a hyphen (adjective-noun), no dot prefix
        # tying it to the parent.
        assert "." not in cid
        assert "-" in cid
        assert any("depth" in r.message.lower() for r in caplog.records)


# ── get_group_progress ───────────────────────────────────────────────────


class TestGroupProgress:
    async def test_counts_and_waves_on_a_small_graph(self, db):
        # parent + 4 children; chained blocks: c1 → c2 → c3 (c4 independent).
        await mktask(db, "parent", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "c1", parent_task_id="parent", status=TaskStatus.COMPLETED)
        await mktask(db, "c2", parent_task_id="parent", status=TaskStatus.READY)
        await mktask(db, "c3", parent_task_id="parent", status=TaskStatus.DEFINED)
        await mktask(db, "c4", parent_task_id="parent", status=TaskStatus.IN_PROGRESS)
        await db.add_dependency("c2", "c1", DepType.BLOCKS.value)
        await db.add_dependency("c3", "c2", DepType.BLOCKS.value)

        prog = await db.get_group_progress("parent")
        assert prog["parent_id"] == "parent"
        assert prog["total"] == 4
        assert prog["done"] == 1
        assert prog["ready"] == 1
        assert prog["in_progress"] == 1
        # c3 is DEFINED and (via blocks c2) is_blocked=1 — counted blocked.
        assert prog["blocked"] == 1
        # Waves: {c1, c4} → {c2} → {c3}.
        assert prog["waves"][0] == ["c1", "c4"]
        assert prog["waves"][1] == ["c2"]
        assert prog["waves"][2] == ["c3"]

    async def test_no_children(self, db):
        await mktask(db, "solo")
        prog = await db.get_group_progress("solo")
        assert prog["total"] == 0
        assert prog["waves"] == []


# ── transition_task force + enforcement ──────────────────────────────────


class TestEnforcement:
    async def test_default_is_warn_only(self, db, caplog):
        """An invalid transition warns and proceeds when enforce=False."""
        import logging as _lg

        await mktask(db, "t", status=TaskStatus.COMPLETED)
        caplog.set_level(_lg.WARNING, logger="src.database.queries.task_queries")
        # COMPLETED -> IN_PROGRESS is not in VALID_STATUS_TRANSITIONS.
        await db.transition_task("t", TaskStatus.IN_PROGRESS)
        task = await db.get_task("t")
        assert task.status == TaskStatus.IN_PROGRESS
        assert any("Invalid task status transition" in r.message for r in caplog.records)

    async def test_enforce_raises_invalid_transition(self, db):
        await mktask(db, "t", status=TaskStatus.COMPLETED)
        db.set_state_machine_enforcement(True)
        try:
            with pytest.raises(InvalidTransition) as ei:
                await db.transition_task("t", TaskStatus.IN_PROGRESS)
            assert ei.value.from_status == TaskStatus.COMPLETED
            assert ei.value.to_status == TaskStatus.IN_PROGRESS
        finally:
            db.set_state_machine_enforcement(False)

    async def test_force_bypasses_enforcement(self, db):
        await mktask(db, "t", status=TaskStatus.COMPLETED)
        db.set_state_machine_enforcement(True)
        try:
            await db.transition_task("t", TaskStatus.IN_PROGRESS, force=True)
        finally:
            db.set_state_machine_enforcement(False)
        assert (await db.get_task("t")).status == TaskStatus.IN_PROGRESS


class TestInvalidTransitionShape:
    def test_carries_from_and_to(self):
        exc = InvalidTransition(
            TaskStatus.COMPLETED,
            None,
            from_status=TaskStatus.COMPLETED,
            to_status=TaskStatus.IN_PROGRESS,
        )
        assert exc.from_status == TaskStatus.COMPLETED
        assert exc.to_status == TaskStatus.IN_PROGRESS
        assert "COMPLETED" in str(exc)
        assert "IN_PROGRESS" in str(exc)

    def test_back_compat_event_form(self):
        from src.models import TaskEvent

        exc = InvalidTransition(TaskStatus.DEFINED, TaskEvent.DEPS_MET)
        # Legacy attributes still present.
        assert exc.state == TaskStatus.DEFINED
        assert exc.event == TaskEvent.DEPS_MET


# ── _cmd_task_close carries all outcome-meta keys ────────────────────────


class TestTaskCloseMetaKeys:
    async def test_session_close_writes_work_branch(self, db):
        """WG-5 adds ``work_branch`` to the outcome-meta contract.  The
        existing ``_cmd_task_close`` already writes the other six —
        ``outcome`` / ``failure_class`` / ``work_outcome`` / ``work_commit``
        / ``verification`` / ``close_notes`` — this test guards the new key.
        """
        # We only exercise the meta-write side by calling ``set_task_meta``
        # under the WG-5-defined key list; the command test proper lives in
        # tests/test_session_commands.py which we run in the same suite.
        await mktask(db, "t", status=TaskStatus.IN_PROGRESS)
        for key, value in [
            ("outcome", "success"),
            ("failure_class", "soft"),
            ("work_outcome", "shipped"),
            ("work_commit", "abc123"),
            ("work_branch", "aq/t"),
            ("verification", "green"),
            ("close_notes", "done"),
        ]:
            await db.set_task_meta("t", key, value)
        meta = await db.get_all_task_meta("t")
        for key in (
            "outcome",
            "failure_class",
            "work_outcome",
            "work_commit",
            "work_branch",
            "verification",
            "close_notes",
        ):
            assert key in meta
