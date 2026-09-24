"""Container-close semantics and the hierarchy command surface — spec §7, §14."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.commands.handler import CommandHandler
from src.config import DatabaseConfig, AppConfig, DiscordConfig
from src.database import Database
from src.models import AgentProfile, DepType, Project, SessionRecord, Task, TaskStatus
from src.orchestrator import Orchestrator
from src.tools import _ALL_TOOL_DEFINITIONS
from tests.db_fixtures import lease_dsn

PROJECT_ID = "proj"


@pytest.fixture
async def db(tmp_path):
    database = Database(lease_dsn("test.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT_ID, name="Test Project"))
    yield database
    await database.close()


@pytest.fixture
def config(tmp_path):
    return AppConfig(
        discord=DiscordConfig(bot_token="test-token", guild_id="123"),
        workspace_dir=str(tmp_path / "workspaces"),
        database=DatabaseConfig(url=lease_dsn("test.db")),
        data_dir=str(tmp_path / "data"),
    )


@pytest.fixture
async def handler(db, config):
    orchestrator = Orchestrator(config)
    orchestrator.db = db
    orchestrator.git = MagicMock()
    orchestrator.complete_session_task = AsyncMock(return_value={"status": "COMPLETED"})
    return CommandHandler(orchestrator, config)


async def mktask(db, tid, status=TaskStatus.DEFINED, **kw):
    await db.create_task(
        Task(id=tid, project_id=PROJECT_ID, title=tid, description=tid, status=status, **kw)
    )
    return tid


async def mksession(db, sid, task_id, state="running"):
    now = time.time()
    await db.create_session(
        SessionRecord(
            id=sid,
            task_id=task_id,
            project_id=PROJECT_ID,
            profile_id="worker",
            harness="claude",
            provider="fake",
            name=f"s-{sid}",
            lifecycle="task",
            state=state,
            work_dir="/tmp",
            epoch="e",
            instance_token=sid,
            started_at=now,
            last_activity=now,
        )
    )
    return sid


async def container_with_open_child(db):
    await mktask(db, "p", status=TaskStatus.IN_PROGRESS)
    await mktask(db, "c", status=TaskStatus.READY)
    await db.add_dependency("c", "p", "parent-child")


class TestCloseRefusals:
    async def test_task_close_refuses_open_children(self, handler, db):
        await container_with_open_child(db)
        res = await handler._cmd_task_close({"task_id": "p", "outcome": "pass", "summary": "x"})
        assert res["success"] is False
        assert res["code"] == "hierarchy.open_children"
        assert res["open_children"] == ["c"]

    async def test_set_task_status_refuses_open_children(self, handler, db):
        await container_with_open_child(db)
        res = await handler._cmd_set_task_status({"task_id": "p", "status": "COMPLETED"})
        assert res.get("code") == "hierarchy.open_children"

    async def test_skip_refuses_open_children(self, handler, db):
        await mktask(db, "p", status=TaskStatus.BLOCKED)
        await mktask(db, "c", status=TaskStatus.READY)
        await db.add_dependency("c", "p", "parent-child")
        res = await handler._cmd_skip_task({"task_id": "p"})
        assert "open_children" in res["error"]


class TestAbandonChildren:
    async def test_abandons_when_no_live_descendants(self, handler, db):
        await container_with_open_child(db)
        res = await handler._cmd_task_close(
            {"task_id": "p", "outcome": "pass", "summary": "x", "abandon_children": True}
        )
        assert res["success"] is True
        assert res["abandoned"] == ["c"]
        assert (await db.get_task("c")).status == TaskStatus.COMPLETED
        assert await db.get_task_meta("c", "work_outcome") == "abandoned"

    async def test_refused_while_descendant_has_live_session(self, handler, db):
        await container_with_open_child(db)
        now = time.time()
        await db.create_session(
            SessionRecord(
                id="s1",
                task_id="c",
                project_id=PROJECT_ID,
                profile_id="worker",
                harness="claude",
                provider="fake",
                name="s-c",
                lifecycle="task",
                state="running",
                work_dir="/tmp",
                epoch="e",
                instance_token="t",
                started_at=now,
                last_activity=now,
            )
        )
        res = await handler._cmd_task_close(
            {"task_id": "p", "outcome": "pass", "summary": "x", "abandon_children": True}
        )
        assert res["code"] == "hierarchy.live_descendants"
        assert res["sessions"] == [{"session_id": "s1", "task_id": "c"}]
        assert (await db.get_task("c")).status == TaskStatus.READY

    async def test_parents_own_session_does_not_block_abandon(self, handler, db):
        """The closing container's own session is not a *descendant* holder.

        Regression for smart-orbit.9: with the only child PAUSED, unassigned
        and session-less, ``--abandon-children`` still refused because the
        parent's own live session was counted by the subtree check.
        """
        await mktask(db, "p", status=TaskStatus.IN_PROGRESS)
        # PAUSED with a resume_after = rate-limit pause, not a manual one;
        # unassigned and holding no session of its own.
        await mktask(db, "c", status=TaskStatus.PAUSED, resume_after=time.time() + 3600)
        await db.add_dependency("c", "p", "parent-child")
        await mksession(db, "s-parent", "p")

        res = await handler._cmd_task_close(
            {"task_id": "p", "outcome": "pass", "summary": "x", "abandon_children": True}
        )
        assert res["success"] is True
        assert res["abandoned"] == ["c"]
        assert (await db.get_task("c")).status == TaskStatus.COMPLETED

    async def test_refusal_names_the_offending_descendants(self, handler, db):
        await container_with_open_child(db)
        await mksession(db, "s-parent", "p")
        await mksession(db, "s1", "c")

        res = await handler._cmd_task_close(
            {"task_id": "p", "outcome": "pass", "summary": "x", "abandon_children": True}
        )
        assert res["code"] == "hierarchy.live_descendants"
        assert res["live_descendants"] == ["c"]
        assert "c" in res["error"]
        # The parent's own session is never reported as a descendant holder.
        assert res["sessions"] == [{"session_id": "s1", "task_id": "c"}]

    async def test_duplicate_and_deleted_children_do_not_count(self, handler, db):
        """Two live sessions on one descendant collapse to one task id, and a
        deleted child contributes nothing to the live check."""
        await container_with_open_child(db)
        await mktask(db, "gone", status=TaskStatus.READY)
        await db.add_dependency("gone", "p", "parent-child")
        await mksession(db, "s-gone", "gone")
        await db.delete_task("gone", cascade=False)
        await mksession(db, "s1", "c")
        await mksession(db, "s2", "c")

        res = await handler._cmd_task_close(
            {"task_id": "p", "outcome": "pass", "summary": "x", "abandon_children": True}
        )
        assert res["code"] == "hierarchy.live_descendants"
        assert res["live_descendants"] == ["c"]
        assert res["sessions"] == [
            {"session_id": "s1", "task_id": "c"},
            {"session_id": "s2", "task_id": "c"},
        ]

    async def test_summary_refusal_precedes_abandon(self, handler, db):
        """A refused close (missing summary) must never abandon anything —
        the summary check runs before the container-close block (review
        finding #1)."""
        await db.create_profile(AgentProfile(id="worker", name="Worker", needs_workspace=True))
        await mktask(db, "p", status=TaskStatus.IN_PROGRESS, profile_id="worker")
        await mktask(db, "c", status=TaskStatus.READY)
        await db.add_dependency("c", "p", "parent-child")

        res = await handler._cmd_task_close(
            {"task_id": "p", "outcome": "pass", "abandon_children": True}
        )
        assert res["success"] is False
        assert "summary is required" in res["error"]
        assert (await db.get_task("c")).status == TaskStatus.READY

    async def test_abandon_settlement_flip_reaches_events(self, handler, db):
        """A sibling ``blocks``-dependent on the abandoned child unblocks in
        the same call, and the flip lands in the ``task.unblocked`` audit
        log — not dropped when produced inside ``abandon_subtree`` (review
        finding #2)."""
        await container_with_open_child(db)
        await mktask(db, "sib", status=TaskStatus.DEFINED)
        await db.add_dependency("sib", "c", DepType.BLOCKS.value)
        assert (await db.get_task("sib")).is_blocked is True

        res = await handler._cmd_task_close(
            {"task_id": "p", "outcome": "pass", "summary": "x", "abandon_children": True}
        )
        assert res["success"] is True
        assert (await db.get_task("sib")).is_blocked is False

        rows = await db.get_recent_events(event_type="task.unblocked", task_id="sib")
        assert rows, "expected a task.unblocked audit row for 'sib'"

    async def test_abandon_forces_invalid_transition(self, handler, db):
        """A timed/backoff PAUSED descendant has no ordinary path to COMPLETED;
        abandonment forces the state-machine edge, but a manual pause is
        separately protected (see the next test)."""
        await container_with_open_child(db)
        await db.transition_task(
            "c", TaskStatus.PAUSED, context="test-setup", force=True, resume_after=time.time() + 60,
        )
        assert (await db.get_task("c")).status == TaskStatus.PAUSED
        db.set_state_machine_enforcement(True)

        res = await handler._cmd_task_close(
            {"task_id": "p", "outcome": "pass", "summary": "x", "abandon_children": True}
        )
        assert res["success"] is True
        assert res["abandoned"] == ["c"]
        assert (await db.get_task("c")).status == TaskStatus.COMPLETED

    async def test_abandon_does_not_override_manual_pause(self, handler, db):
        """A hand-paused descendant still refuses the close — but as a
        structured ``hierarchy.manually_paused_descendants`` result naming
        the ids, not a ``ManualPauseActive`` escaping the transaction."""
        await container_with_open_child(db)
        await mktask(db, "sibling", status=TaskStatus.READY)
        await db.add_dependency("sibling", "p", "parent-child")
        await db.transition_task("c", TaskStatus.PAUSED, context="test-setup", force=True)
        res = await handler._cmd_task_close(
            {"task_id": "p", "outcome": "pass", "summary": "x", "abandon_children": True}
        )
        assert res["success"] is False
        assert res["code"] == "hierarchy.manually_paused_descendants"
        assert res["manually_paused_descendants"] == ["c"]
        assert "aq task resume --task-id <id>" in res["error"]
        assert (await db.get_task("c")).status == TaskStatus.PAUSED
        assert (await db.get_task("sibling")).status == TaskStatus.READY
        assert (await db.get_task("p")).status == TaskStatus.IN_PROGRESS
        assert await db.get_task_meta("p", "outcome") is None

    async def test_abandons_deepest_first_multi_level(self, handler, db):
        """container -> child -> grandchild, only the grandchild open: both
        the child (now-emptied container) and grandchild close, deepest
        first (review finding #7)."""
        await mktask(db, "p", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "c", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "g", status=TaskStatus.READY)
        await db.add_dependency("c", "p", "parent-child")
        await db.add_dependency("g", "c", "parent-child")

        res = await handler._cmd_task_close(
            {"task_id": "p", "outcome": "pass", "summary": "x", "abandon_children": True}
        )
        assert res["success"] is True
        assert set(res["abandoned"]) == {"c", "g"}
        assert res["abandoned"].index("g") < res["abandoned"].index("c")
        assert (await db.get_task("c")).status == TaskStatus.COMPLETED
        assert (await db.get_task("g")).status == TaskStatus.COMPLETED
        assert await db.get_task_meta("g", "work_outcome") == "abandoned"
        assert await db.get_task_meta("c", "work_outcome") == "abandoned"

    async def test_close_with_all_terminal_children_needs_no_flag(self, handler, db):
        """A container whose children are already terminal closes normally —
        no ``abandon_children`` needed, and nothing is reported abandoned.

        Uses a FAILED child rather than COMPLETED: a COMPLETED child would
        already have settled ``p`` via the ordinary settlement cascade
        (spec §7), leaving nothing left to close by the time this test
        calls ``_cmd_task_close`` — FAILED is terminal for the close-refusal
        check but does not participate in that cascade.
        """
        await mktask(db, "p", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "c", status=TaskStatus.FAILED)
        await db.add_dependency("c", "p", "parent-child")

        res = await handler._cmd_task_close({"task_id": "p", "outcome": "pass", "summary": "x"})
        assert res["success"] is True
        assert res["abandoned"] == []


class TestCascadeDeleteLiveDescendants:
    """Cascade delete refuses rather than pulling a live session out from
    under a grandchild (spec §7, controller ruling on task 7 review)."""

    async def _grandchild_tree(self, db):
        await mktask(db, "p", status=TaskStatus.DEFINED)
        await mktask(db, "c", status=TaskStatus.READY)
        await mktask(db, "gc", status=TaskStatus.READY)
        await db.add_dependency("c", "p", "parent-child")
        await db.add_dependency("gc", "c", "parent-child")

    async def test_refused_while_grandchild_has_live_session(self, handler, db):
        await self._grandchild_tree(db)
        now = time.time()
        await db.create_session(
            SessionRecord(
                id="s1",
                task_id="gc",
                project_id=PROJECT_ID,
                profile_id="worker",
                harness="claude",
                provider="fake",
                name="s-gc",
                lifecycle="task",
                state="running",
                work_dir="/tmp",
                epoch="e",
                instance_token="t",
                started_at=now,
                last_activity=now,
            )
        )

        res = await handler._cmd_delete_task({"task_id": "p", "cascade": True})
        assert res["success"] is False
        assert res["code"] == "hierarchy.live_descendants"
        assert res["sessions"] == [{"session_id": "s1", "task_id": "gc"}]

        # Nothing was deleted.
        assert await db.get_task("p") is not None
        assert await db.get_task("c") is not None
        assert await db.get_task("gc") is not None

    async def test_succeeds_once_session_stopped(self, handler, db):
        await self._grandchild_tree(db)
        now = time.time()
        await db.create_session(
            SessionRecord(
                id="s1",
                task_id="gc",
                project_id=PROJECT_ID,
                profile_id="worker",
                harness="claude",
                provider="fake",
                name="s-gc",
                lifecycle="task",
                state="running",
                work_dir="/tmp",
                epoch="e",
                instance_token="t",
                started_at=now,
                last_activity=now,
            )
        )
        await db.update_session("s1", state="stopped")

        res = await handler._cmd_delete_task({"task_id": "p", "cascade": True})
        assert res == {"deleted": "p", "title": "p", "discarded_branches": []}

        assert await db.get_task("p") is None
        assert await db.get_task("c") is None
        assert await db.get_task("gc") is None


class TestHierarchyCommands:
    async def test_children_flat_and_recursive(self, handler, db):
        await mktask(db, "r", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "r.1", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "r.1.1", status=TaskStatus.READY)
        await db.add_dependency("r.1", "r", "parent-child")
        await db.add_dependency("r.1.1", "r.1", "parent-child")
        flat = await handler._cmd_task_children({"task_id": "r"})
        assert [c["id"] for c in flat["children"]] == ["r.1"]
        deep = await handler._cmd_task_children({"task_id": "r", "recursive": True})
        assert [c["id"] for c in deep["children"]] == ["r.1", "r.1.1"]

    async def test_progress(self, handler, db):
        await mktask(db, "r", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "r.1", status=TaskStatus.READY)
        await db.add_dependency("r.1", "r", "parent-child")
        res = await handler._cmd_task_progress({"task_id": "r"})
        assert res["success"] is True
        assert res["total"] == 1 and res["ready"] == 1 and res["max_parallelism"] == 1

    async def test_reparent_and_root(self, handler, db):
        await mktask(db, "p1", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "p2", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "c")
        await db.add_dependency("c", "p1", "parent-child")
        res = await handler._cmd_reparent_task({"task_id": "c", "parent_id": "p2"})
        assert res["success"] is True and res["old_parent"] == "p1"
        assert (await db.get_task("c")).parent_task_id == "p2"
        res = await handler._cmd_reparent_task({"task_id": "c", "root": True})
        assert (await db.get_task("c")).parent_task_id is None

    async def test_reparent_error_codes(self, handler, db):
        await mktask(db, "a", status=TaskStatus.IN_PROGRESS)
        res = await handler._cmd_reparent_task({"task_id": "a", "parent_id": "a"})
        assert res["code"] == "hierarchy.self_parent"

    async def test_reparent_to_current_parent_is_idempotent(self, handler, db):
        await mktask(db, "p1", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "c")
        await db.add_dependency("c", "p1", "parent-child")
        res = await handler._cmd_reparent_task({"task_id": "c", "parent_id": "p1"})
        assert res["success"] is True
        assert res["old_parent"] == res["new_parent"] == "p1"
        assert "code" not in res
        assert (await db.get_task("c")).parent_task_id == "p1"

    async def test_schema_lists_hierarchy_codes(self, handler):
        res = await handler._cmd_get_schema({})
        assert "container_closed" in res["enums"]["hierarchy_error"]

    async def test_agent_scope_includes_reads(self):
        from src.api.scope import AGENT_COMMAND_SET

        assert {"task_children", "task_progress"} <= AGENT_COMMAND_SET


class TestDeleteBranchPolicy:
    """``task_delete``'s ``branches`` argument, end to end through the handler.

    The guard's own tests cover the database side; these pin the surface: the
    refusal a UI has to act on, and that a discard actually reaches the row the
    drain reads.
    """

    async def _hierarchical_task_with_a_branch(
        self, db, task_id: str, *, branch: str | None = None
    ) -> None:
        from sqlalchemy import insert, update

        from src.database.tables import projects, task_branch_origins

        branch = branch or f"aq/{task_id}"
        await mktask(db, task_id, status=TaskStatus.FAILED, branch_name=branch)
        async with db.immediate() as conn:
            await conn.execute(
                update(projects)
                .where(projects.c.id == PROJECT_ID)
                .values(hierarchical_integration_mode="train", integration_repository_id="repo")
            )
            await conn.execute(
                insert(task_branch_origins).values(
                    id=f"origin-{task_id}",
                    task_id=task_id,
                    repository_id="repo",
                    branch_name=branch,
                    parent_ref="main",
                    base_sha="a" * 40,
                    creation_generation=0,
                    reserved=True,
                    materialized=True,
                    materialized_at=1.0,
                    created_at=1.0,
                )
            )

    async def test_delete_without_a_choice_names_the_branches(self, db, handler):
        epic_branch = "aq/epic/retire-the-publisher"
        await self._hierarchical_task_with_a_branch(
            db, "has-branch", branch=epic_branch
        )

        res = await handler.execute("delete_task", {"task_id": "has-branch"})

        assert res["success"] is False
        assert res["code"] == "hierarchy.branch_discard_required"
        assert res["branches"] == [
            {"task_id": "has-branch", "branch": epic_branch, "base_sha": "a" * 40}
        ]
        assert await db.get_task("has-branch") is not None

    async def test_delete_with_keep_leaves_no_discard_work(self, db, handler):
        from sqlalchemy import select

        from src.database.tables import task_branch_origins

        await self._hierarchical_task_with_a_branch(db, "keep-branch")

        res = await handler.execute(
            "delete_task", {"task_id": "keep-branch", "branches": "keep"}
        )

        assert res["deleted"] == "keep-branch"
        assert res["discarded_branches"] == []
        async with db._engine.connect() as conn:
            state = (
                await conn.execute(
                    select(task_branch_origins.c.discard_state).where(
                        task_branch_origins.c.task_id == "keep-branch"
                    )
                )
            ).scalar_one()
        assert state is None

    async def test_delete_with_delete_queues_the_branch_and_reports_it(self, db, handler):
        from sqlalchemy import select

        from src.database.tables import task_branch_origins

        epic_branch = "aq/epic/retire-the-publisher"
        await self._hierarchical_task_with_a_branch(
            db, "drop-branch", branch=epic_branch
        )

        res = await handler.execute(
            "delete_task", {"task_id": "drop-branch", "branches": "delete"}
        )

        assert res["deleted"] == "drop-branch"
        assert res["discarded_branches"] == [
            {"task_id": "drop-branch", "branch": epic_branch, "base_sha": "a" * 40}
        ]
        assert await db.get_task("drop-branch") is None
        async with db._engine.connect() as conn:
            origin = (
                await conn.execute(
                    select(task_branch_origins).where(
                        task_branch_origins.c.task_id == "drop-branch"
                    )
                )
            ).mappings().one()
        assert origin["discard_state"] == "pending"
        assert origin["branch_name"] == epic_branch

    async def test_an_unknown_choice_is_rejected_before_anything_moves(self, db, handler):
        await self._hierarchical_task_with_a_branch(db, "bad-choice")

        res = await handler.execute(
            "delete_task", {"task_id": "bad-choice", "branches": "shred"}
        )

        assert res["success"] is False
        assert res["code"] == "invalid_branches"
        assert await db.get_task("bad-choice") is not None


class TestHierarchyRefusalsOverHTTP:
    """The refusals a dashboard has to act on must survive the typed route.

    ``src/api/codegen.py`` answers a command error with a bare
    ``{"error": ...}`` 422 unless the command is on its allowlist, which
    strips the very ``code``/``branches``/``references`` keys the delete
    dialog branches on. These tests pin the wire body, not the handler
    result — the handler-level tests above cannot see that boundary.
    """

    @pytest.fixture
    async def typed_routes(self, handler):
        """A client serving the generated ``delete_task``/``archive_task`` routes.

        ``ASGITransport``, never ``TestClient``: the latter drives the app on
        an event loop of its own, and the handler's asyncpg connections belong
        to this test's loop — every database call would fail with "another
        operation is in progress" and the route would answer a 422 carrying
        that error instead of the refusal under test.
        """
        from fastapi import FastAPI
        from httpx import ASGITransport, AsyncClient

        from src.api.codegen import _make_input_model, _make_route_handler
        from src.api.dependencies import get_command_handler

        app = FastAPI()
        app.dependency_overrides[get_command_handler] = lambda: handler
        for cmd, path in (
            ("delete_task", "/api/task/delete"),
            ("archive_task", "/api/task/archive"),
        ):
            schema = next(
                tool["input_schema"]
                for tool in _ALL_TOOL_DEFINITIONS
                if tool["name"] == cmd
            )
            route = _make_route_handler(cmd, _make_input_model(cmd, schema))
            app.add_api_route(path, route, methods=["POST"])
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as client:
            yield client

    async def _integration_owned_task(self, db, task_id: str) -> None:
        """A COMPLETED task an append-only integration audit row still names."""
        from sqlalchemy import insert

        from src.database.tables import integration_parent_episodes
        from src.models import RepoConfig, RepoSourceType

        await db.create_repo(
            RepoConfig(id="repo", project_id=PROJECT_ID, source_type=RepoSourceType.LINK)
        )
        await mktask(db, task_id, status=TaskStatus.COMPLETED)
        async with db._engine.begin() as conn:
            await conn.execute(
                insert(integration_parent_episodes).values(
                    id="ep",
                    parent_task_id=task_id,
                    repository_id="repo",
                    generation=0,
                    pre_collection_checkpoint_sha="a" * 40,
                    created_at=1.0,
                )
            )

    async def test_delete_refused_by_integration_history_keeps_its_code_on_the_wire(
        self, db, typed_routes
    ):
        await self._integration_owned_task(db, "owned")

        response = await typed_routes.post("/api/task/delete", json={"task_id": "owned"})

        assert response.status_code == 422, response.text
        body = response.json()
        assert body["code"] == "hierarchy.integration_history_retained"
        assert body["success"] is False
        assert body["error"].startswith("hierarchy.integration_history_retained:")
        assert [r["table"] for r in body["references"]] == ["integration_parent_episodes"]
        assert await db.get_task("owned") is not None

    async def test_archive_with_integration_history_succeeds_on_the_wire(
        self, db, typed_routes
    ):
        await self._integration_owned_task(db, "owned")

        response = await typed_routes.post("/api/task/archive", json={"task_id": "owned"})

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["archived"] == "owned"
        assert await db.get_task("owned") is None

    async def test_branch_discard_refusal_keeps_its_branch_list_on_the_wire(
        self, db, typed_routes
    ):
        """What ``BranchDiscardPrompt`` renders has to reach the browser."""
        await TestDeleteBranchPolicy()._hierarchical_task_with_a_branch(db, "has-branch")

        response = await typed_routes.post("/api/task/delete", json={"task_id": "has-branch"})

        assert response.status_code == 422, response.text
        body = response.json()
        assert body["code"] == "hierarchy.branch_discard_required"
        assert body["branches"] == [
            {"task_id": "has-branch", "branch": "aq/has-branch", "base_sha": "a" * 40}
        ]
        assert await db.get_task("has-branch") is not None


class TestDeleteIntegrationOwned:
    """An integration audit row refuses a cascade delete through the surface.

    The cascade path checks ``live_descendant_sessions`` and deletes inside one
    caller-owned transaction, so the refusal has to come back as the command's
    refusal dict *and* roll that transaction back — never half a tree deleted.
    """

    async def _repair_verified(self, db, task_id: str) -> None:
        """A settled batch repair whose verifier was *task_id* (RESTRICT FK)."""
        from sqlalchemy import insert

        from src.database.tables import integration_repair_operations

        now = time.time()
        async with db._engine.begin() as conn:
            await conn.execute(
                insert(integration_repair_operations).values(
                    id="op-1",
                    target_kind="batch",
                    batch_id="b-1",
                    parent_task_id=None,
                    episode_id="ep-batch",
                    active_stage=0,
                    state="completed",
                    policy_snapshot={},
                    artifact_snapshot={},
                    required_check_version="v1",
                    verifier_task_id=task_id,
                    created_at=now,
                    updated_at=now,
                )
            )

    async def test_cascade_delete_reports_the_refusal_and_keeps_the_tree(self, db, handler):
        await mktask(db, "p", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "c", status=TaskStatus.COMPLETED)
        await db.add_dependency("c", "p", "parent-child")
        await self._repair_verified(db, "c")

        res = await handler.execute("delete_task", {"task_id": "p", "cascade": True})

        assert res["success"] is False
        assert res["code"] == "hierarchy.integration_history_retained"
        assert "integration_repair_operations(c)" in res["error"]
        assert await db.get_task("p") is not None
        assert await db.get_task("c") is not None
