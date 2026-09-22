"""Delete refuse/cascade and subtree-atomic archive — spec §7."""

from __future__ import annotations

import time

import pytest

from src.database import Database
from src.database.queries.hierarchy_queries import HierarchyError
from src.models import Project, Task, TaskCompletion, TaskStatus
from tests.db_fixtures import lease_dsn

PROJECT_ID = "proj"


@pytest.fixture
async def db(tmp_path):
    database = Database(lease_dsn("test.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT_ID, name="p"))
    yield database
    await database.close()


async def mktask(db, tid, status=TaskStatus.DEFINED, **kw):
    await db.create_task(
        Task(id=tid, project_id=PROJECT_ID, title=tid, description=tid, status=status, **kw)
    )


async def tree(db, statuses=("IN_PROGRESS", "READY", "READY")):
    """Build parent+2 children, all IN_PROGRESS/READY so ``add_dependency``
    (parent-child) never trips ``container_closed``, then stamp the intended
    terminal statuses in place with a raw UPDATE (controller ruling #1)."""
    await mktask(db, "p", status=TaskStatus.IN_PROGRESS)
    await mktask(db, "c1", status=TaskStatus.READY)
    await mktask(db, "c2", status=TaskStatus.READY)
    await db.add_dependency("c1", "p", "parent-child")
    await db.add_dependency("c2", "p", "parent-child")
    await _set_statuses(db, {"p": statuses[0], "c1": statuses[1], "c2": statuses[2]})


async def _set_statuses(db, id_to_status: dict) -> None:
    from sqlalchemy import update

    from src.database.tables import tasks

    async with db._engine.begin() as conn:
        for tid, status in id_to_status.items():
            await conn.execute(update(tasks).where(tasks.c.id == tid).values(status=status))


class TestDelete:
    async def test_refuses_container_with_children(self, db):
        await tree(db)
        with pytest.raises(HierarchyError) as exc:
            await db.delete_task("p")
        assert exc.value.code == "has_children"
        assert await db.get_task("p") is not None

    async def test_cascade_deletes_subtree(self, db):
        await tree(db)
        await db.save_task_completion(
            TaskCompletion(id="close-1", task_id="c1", outcome="pass", completed_at=1.0)
        )
        await db.delete_task("p", cascade=True)
        assert await db.get_task("p") is None
        assert await db.get_task("c1") is None
        assert await db.get_task("c2") is None
        assert await db.get_task_completion("c1") is None

    async def test_deleting_last_child_settles_container(self, db):
        await tree(db, statuses=("IN_PROGRESS", "COMPLETED", "READY"))
        await db.delete_task("c2")
        assert (await db.get_task("p")).status == TaskStatus.COMPLETED


class TestArchive:
    async def test_refuses_open_descendants(self, db):
        await tree(db, statuses=("COMPLETED", "COMPLETED", "READY"))
        with pytest.raises(HierarchyError) as exc:
            await db.archive_task("p")
        assert exc.value.code == "open_descendants"

    async def test_archives_subtree_together(self, db):
        await tree(db, statuses=("COMPLETED", "COMPLETED", "FAILED"))
        assert await db.archive_task("p") is True
        for tid in ("p", "c1", "c2"):
            assert await db.get_task(tid) is None
            assert (await db.get_archived_task(tid)) is not None
        assert (await db.get_archived_task("c1"))["parent_task_id"] == "p"

    async def test_completion_story_survives_archive(self, db):
        await mktask(db, "done", status=TaskStatus.COMPLETED)
        await db.save_task_completion(
            TaskCompletion(
                id="close-1",
                task_id="done",
                outcome="pass",
                summary="Shipped with tests.",
                completed_at=1234.5,
            )
        )

        assert await db.archive_task("done") is True
        archived_completion = await db.get_task_completion("done")
        assert archived_completion is not None
        assert archived_completion.summary == "Shipped with tests."

    async def test_sweep_selects_only_terminal_subtree_roots(self, db):
        await tree(db, statuses=("COMPLETED", "COMPLETED", "READY"))
        await mktask(db, "lone", status=TaskStatus.COMPLETED)
        # Make every row old enough.
        async with db._engine.begin() as conn:
            from sqlalchemy import update

            from src.database.tables import tasks

            await conn.execute(update(tasks).values(updated_at=time.time() - 10_000))
        archived = await db.archive_old_terminal_tasks(["COMPLETED"], older_than_seconds=1)
        assert archived == ["lone"]
        assert await db.get_task("p") is not None
        assert await db.get_task("c1") is not None

    async def test_sweep_skips_root_with_open_grandchild(self, db):
        """The sweep's EXISTS check only sees direct children — an open
        grandchild is caught by ``archive_task``'s own subtree check, which
        raises; the sweep must catch that, skip the root without raising
        out, and keep archiving other roots (controller ruling on task 7
        review)."""
        await mktask(db, "root", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "child", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "grandchild", status=TaskStatus.READY)
        await db.add_dependency("child", "root", "parent-child")
        await db.add_dependency("grandchild", "child", "parent-child")
        await _set_statuses(db, {"root": "COMPLETED", "child": "COMPLETED"})
        await mktask(db, "lone", status=TaskStatus.COMPLETED)

        async with db._engine.begin() as conn:
            from sqlalchemy import update

            from src.database.tables import tasks

            await conn.execute(update(tasks).values(updated_at=time.time() - 10_000))

        archived = await db.archive_old_terminal_tasks(["COMPLETED"], older_than_seconds=1)
        assert archived == ["lone"]
        assert await db.get_task("root") is not None
        assert await db.get_task("child") is not None
        assert await db.get_task("grandchild") is not None
        assert await db.get_archived_task("lone") is not None


class TestArchiveCompletedBulk:
    async def test_skips_root_with_open_grandchild_and_keeps_going(self, db):
        """``archive_completed_tasks`` gets the same guard as the age sweep:
        one refused subtree must not abort the bulk archive."""
        await mktask(db, "root", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "child", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "grandchild", status=TaskStatus.READY)
        await db.add_dependency("child", "root", "parent-child")
        await db.add_dependency("grandchild", "child", "parent-child")
        await _set_statuses(db, {"root": "COMPLETED", "child": "COMPLETED"})
        await mktask(db, "lone", status=TaskStatus.COMPLETED)

        archived = await db.archive_completed_tasks()

        assert "lone" in archived
        assert "root" not in archived and "child" not in archived
        assert await db.get_task("root") is not None
        assert await db.get_task("grandchild") is not None
        assert await db.get_archived_task("lone") is not None


class TestTaskReferenceDispositions:
    """Every foreign key onto ``tasks`` has a declared disposition.

    ``_delete_one`` removes the ``tasks`` row, so a table it does not know
    about surfaces as a raw ``IntegrityError`` — which is how the hourly
    auto-archive sweep came to fail 225 times in a row on
    ``integration_parent_episodes``.  These tests walk the live metadata so a
    *new* table with a foreign key onto ``tasks`` fails here rather than
    silently breaking the sweep again.
    """

    @staticmethod
    def _task_foreign_keys() -> dict[tuple[str, str], tuple[str | None, bool]]:
        from src.database.tables import metadata

        found: dict[tuple[str, str], tuple[str | None, bool]] = {}
        for table in metadata.sorted_tables:
            for fk in table.foreign_key_constraints:
                for element in fk.elements:
                    if element.column.table.name != "tasks":
                        continue
                    column = table.c[element.parent.name]
                    found[(table.name, column.name)] = (fk.ondelete, column.nullable)
        return found

    def test_every_foreign_key_onto_tasks_is_classified(self):
        from src.database.queries.task_references import TASK_REFERENCE_DISPOSITIONS

        found = set(self._task_foreign_keys())
        declared = set(TASK_REFERENCE_DISPOSITIONS)
        assert found - declared == set(), (
            "new foreign key(s) onto tasks with no declared disposition — either teach "
            "_delete_one to clean them up or classify them in "
            "src/database/queries/task_references.py"
        )
        history = {key for key, value in TASK_REFERENCE_DISPOSITIONS.items() if value == "history"}
        assert declared - found == history, "only durable history may lack a task foreign key"

    def test_dispositions_agree_with_the_constraints_they_describe(self):
        from src.database.queries.task_references import TASK_REFERENCE_DISPOSITIONS

        for key, (ondelete, nullable) in self._task_foreign_keys().items():
            disposition = TASK_REFERENCE_DISPOSITIONS[key]
            if disposition == "db_cascade":
                assert ondelete == "CASCADE", f"{key} is not ON DELETE CASCADE"
            if disposition in ("nulled", "released"):
                assert nullable, f"{key} cannot be nulled — the column is NOT NULL"

    def test_a_blocking_key_is_either_cleaned_up_in_delete_one_or_refused(self):
        """A disposition that claims a cleanup nobody performs fails here.

        ``deleted`` / ``nulled`` / ``released`` are claims *about
        ``_delete_one``*: they say that method removes or clears the
        reference before the ``tasks`` row goes.  Read its source and check
        the claim, so declaring a new table ``deleted`` and forgetting the
        statement is caught here rather than by the hourly sweep dying.
        """
        import inspect

        from src.database.queries.task_queries import TaskQueryMixin
        from src.database.queries.task_references import TASK_REFERENCE_DISPOSITIONS

        # Reference cleared through a helper rather than an inline DELETE.
        indirect = {("task_layouts", "task_id"): "delete_layout_rows_for_tasks"}
        source = inspect.getsource(TaskQueryMixin._delete_one)

        for key, (ondelete, _nullable) in self._task_foreign_keys().items():
            if ondelete not in (None, "RESTRICT", "NO ACTION"):
                continue  # the database itself disposes of it
            disposition = TASK_REFERENCE_DISPOSITIONS[key]
            assert disposition != "db_cascade", f"{key} blocks the DELETE; it does not cascade"
            if disposition in ("history", "subtree"):
                continue
            needle = indirect.get(key, key[0])
            assert needle in source, (
                f"{key} is declared {disposition!r}, but _delete_one never mentions "
                f"{needle!r} — either clean it up there or classify it as history"
            )

    def test_history_dispositions_have_no_foreign_key(self):
        from src.database.queries.task_references import TASK_REFERENCE_DISPOSITIONS

        found = set(self._task_foreign_keys())
        history = {key for key, value in TASK_REFERENCE_DISPOSITIONS.items() if value == "history"}
        assert history
        assert history.isdisjoint(found)

    def test_history_dispositions_are_the_ones_the_guard_checks(self):
        from src.database.queries.task_references import (
            INTEGRATION_TASK_REFERENCES,
            TASK_REFERENCE_DISPOSITIONS,
        )

        refused = {k for k, v in TASK_REFERENCE_DISPOSITIONS.items() if v == "history"}
        checked = {(ref.table, ref.column) for ref in INTEGRATION_TASK_REFERENCES}
        assert refused == checked

    @pytest.mark.parametrize(
        "table",
        [
            "integration_parent_episodes",
            "integration_parent_verifications",
            "integration_repair_operations",
            "integration_candidate_resolutions",
        ],
    )
    async def test_archive_retains_and_delete_refuses_each_history_table(self, db, table):
        """Archive preserves every history id; hard delete never orphans one."""
        await _seed_integration_reference(db, table, "t-ref")

        with pytest.raises(HierarchyError) as exc:
            await db.delete_task("t-ref", branch_policy="keep")
        assert exc.value.code == "integration_history_retained"
        assert table in str(exc.value)
        assert await db.archive_task("t-ref") is True
        assert await db.get_task("t-ref") is None
        assert await db.get_archived_task("t-ref") is not None


class TestDeleteIntegrationReferenceScope:
    """Which tasks the delete's history refusal reads: the subtree.

    The refusal is read over the task and every descendant before anything is
    written, so a held descendant pins its whole subtree while a held task
    elsewhere in the project pins nothing.
    """

    async def _held_child(self, db) -> None:
        """``p`` (IN_PROGRESS) with one child ``c`` a parent episode names."""
        await _seed_integration_reference(db, "integration_parent_episodes", "c")
        await mktask(db, "p", status=TaskStatus.IN_PROGRESS)
        await db.add_dependency("c", "p", "parent-child")

    async def test_cascade_refuses_a_held_descendant_and_keeps_the_subtree(self, db):
        """A cascade deletes the subtree as one unit, so a held child pins it all."""
        await self._held_child(db)

        with pytest.raises(HierarchyError) as exc:
            await db.delete_task("p", cascade=True, branch_policy="keep")
        assert exc.value.code == "integration_history_retained"
        assert "integration_parent_episodes(c)" in str(exc.value)

        assert await db.get_task("p") is not None
        assert await db.get_task("c") is not None

    async def test_a_held_descendant_is_reported_before_has_children(self, db):
        """Without ``--cascade`` the answer is still the hold, not ``has_children``.

        The hold is read over the whole subtree up front, so retrying with
        ``--cascade`` would only meet the same row — naming it is the refusal
        that tells the operator the subtree cannot go at all.
        """
        await self._held_child(db)

        with pytest.raises(HierarchyError) as exc:
            await db.delete_task("p", branch_policy="keep")
        assert exc.value.code == "integration_history_retained"
        assert await db.get_task("p") is not None

    async def test_a_hold_on_an_unrelated_task_does_not_refuse(self, db):
        """The hold is scoped to the subtree, not to the table being non-empty."""
        await _seed_integration_reference(db, "integration_parent_episodes", "held")
        await mktask(db, "free", status=TaskStatus.COMPLETED)

        await db.delete_task("free", branch_policy="keep")

        assert await db.get_task("free") is None
        assert await db.get_task("held") is not None


async def _seed_integration_reference(db, table: str, task_id: str) -> None:
    """Create *task_id* plus one row in *table* that names it.

    ``integration_parent_verifications`` is keyed to its episode by
    ``(parent_task_id, episode_id)``, so it can only ever name a task that
    already has an episode — its case seeds both.
    """
    from sqlalchemy import insert

    from src.database.tables import (
        integration_parent_episodes,
        integration_parent_verifications,
        integration_repair_operations,
    )
    from src.models import RepoConfig, RepoSourceType

    await db.create_repo(
        RepoConfig(id="repo", project_id=PROJECT_ID, source_type=RepoSourceType.LINK)
    )
    await mktask(db, task_id, status=TaskStatus.COMPLETED)
    # The verifier case must not also carry an episode of its own, or the
    # test could not tell which table produced the refusal.
    episode_owner = (
        "t-owner"
        if table in ("integration_repair_operations", "integration_candidate_resolutions")
        else task_id
    )
    if episode_owner != task_id:
        await mktask(db, episode_owner, status=TaskStatus.COMPLETED)
    async with db._engine.begin() as conn:
        await conn.execute(
            insert(integration_parent_episodes).values(
                id="ep",
                parent_task_id=episode_owner,
                repository_id="repo",
                generation=0,
                pre_collection_checkpoint_sha="a" * 40,
                created_at=1.0,
            )
        )
        if table == "integration_parent_episodes":
            return
        await conn.execute(
            insert(integration_repair_operations).values(
                id="op",
                target_kind="parent",
                parent_task_id=episode_owner,
                episode_id="ep",
                active_stage=0,
                state="completed",
                policy_snapshot={},
                artifact_snapshot={},
                required_check_version="v1",
                verifier_task_id=(
                    task_id if table == "integration_repair_operations" else None
                ),
                created_at=1.0,
                updated_at=1.0,
            )
        )
        if table == "integration_parent_verifications":
            await conn.execute(
                insert(integration_parent_verifications).values(
                    id="ver",
                    operation_id="op",
                    parent_task_id=task_id,
                    episode_id="ep",
                    generation=0,
                    head_sha="b" * 40,
                    required_check_version="v1",
                    created_at=1.0,
                )
            )
        if table == "integration_candidate_resolutions":
            await _seed_candidate_resolution(conn, task_id)


async def _seed_candidate_resolution(conn, repair_task_id: str) -> None:
    """One conflict-repair resolution row naming *repair_task_id*.

    It sits at the bottom of the candidate-construction chain, so the batch,
    its reviewed member, the revision, the member result, the repair stage,
    and the session and workspace the repair ran in all have to exist first.
    """
    from sqlalchemy import insert

    from src.database.tables import (
        integration_batch_members,
        integration_batches,
        integration_candidate_member_results,
        integration_candidate_resolutions,
        integration_candidate_revisions,
        integration_repair_stages,
        integration_review_evidence,
        sessions,
        workspaces,
    )

    sha = lambda ch: ch * 40  # noqa: E731 — a 40-char sha is all these columns want
    await conn.execute(
        insert(integration_batches).values(
            id="batch",
            project_id=PROJECT_ID,
            repository_id="repo",
            request_id="request",
            trigger="manual",
            source_manifest_digest="sha256:" + "4" * 64,
            base_sha=sha("a"),
            lifecycle="sealing",
            current_revision=0,
            integration_branch="refs/heads/aq/integration/proj/1",
            policy_snapshot={},
            artifact_snapshot={},
            cleanup_state="pending",
            created_at=1.0,
            updated_at=1.0,
        )
    )
    await conn.execute(
        insert(integration_review_evidence).values(
            id="review",
            source_task_id="t-owner",
            repository_id="repo",
            source_base=sha("a"),
            reviewed_head_sha=sha("b"),
            reviewed_tree_sha=sha("c"),
            reviewer_task_id="reviewer",
            reviewer_session_attempt_id=None,
            review_kind="leaf",
            generation=1,
            verdict="approved",
            evidence={"decision": "approved"},
            created_at=1.0,
        )
    )
    await conn.execute(
        insert(integration_batch_members).values(
            batch_id="batch",
            ordinal=0,
            task_id="t-owner",
            repository_id="repo",
            source_base_sha=sha("a"),
            reviewed_head_sha=sha("b"),
            reviewed_tree_sha=sha("c"),
            review_evidence_id="review",
            review_evidence={"id": "review"},
        )
    )
    await conn.execute(
        insert(integration_candidate_revisions).values(
            batch_id="batch",
            revision=0,
            construction_base_sha=sha("a"),
            next_member_ordinal=1,
            state="constructing",
            created_at=1.0,
            updated_at=1.0,
        )
    )
    await conn.execute(
        insert(integration_candidate_member_results).values(
            batch_id="batch",
            revision=0,
            member_ordinal=0,
            input_head_sha=sha("b"),
            input_tree_sha=sha("c"),
            result="conflict",
            created_at=1.0,
            updated_at=1.0,
        )
    )
    await conn.execute(
        insert(integration_repair_stages).values(
            operation_id="op",
            ordinal=0,
            policy={},
            repair_task_id=repair_task_id,
            writer_kind="repair_delegate",
            starting_sha=sha("b"),
            attempts=0,
            state="active",
        )
    )
    await conn.execute(
        insert(sessions).values(
            id="sess",
            profile_id="standard-high-claude",
            harness="claude",
            provider="anthropic",
            name="n-repair",
            lifecycle="task",
            state="running",
            desired_state="running",
            claims=0,
            work_dir="/tmp/repair",
            epoch="0",
            instance_token="token",
            started_at=1.0,
            restarts=0,
            hooks_provisioned=False,
        )
    )
    await conn.execute(
        insert(workspaces).values(
            id="ws",
            project_id=PROJECT_ID,
            workspace_path="/tmp/ws",
            source_type="link",
            enabled=True,
            created_at=1.0,
        )
    )
    await conn.execute(
        insert(integration_candidate_resolutions).values(
            id="resolution",
            batch_id="batch",
            revision=0,
            member_ordinal=0,
            operation_id="op",
            operation_episode_id="ep",
            stage_ordinal=0,
            stage_deadline_at=1000.0,
            project_id=PROJECT_ID,
            repair_task_id=repair_task_id,
            repair_session_id="sess",
            repair_session_instance_token="token",
            repair_workspace_id="ws",
            repair_workspace_path="/tmp/ws",
            repository_id="repo",
            branch="refs/heads/aq/repair",
            target_branch="refs/heads/aq/integration/proj/1",
            target_kind="qualified",
            fence_owner_id="owner",
            fence_token=1,
            partial_head_sha=sha("d"),
            source_base_sha=sha("a"),
            source_head_sha=sha("b"),
            resolved_head_sha=sha("e"),
            resolved_tree_sha=sha("f"),
            repair_commit_shas=[sha("e")],
            state="reserved",
            created_at=1.0,
            updated_at=1.0,
        )
    )


class TestProjectDeleteIntegrationHistory:
    """Project deletion is also a removal path for durable history (S3)."""

    async def test_refuses_history_on_a_live_task_with_a_typed_error(self, db):
        await _seed_integration_reference(db, "integration_parent_episodes", "live-history")

        with pytest.raises(HierarchyError) as exc:
            await db.delete_project(PROJECT_ID)

        assert exc.value.code == "integration_history_retained"
        assert exc.value.context["references"] == [
            {
                "task_id": "live-history",
                "table": "integration_parent_episodes",
                "column": "parent_task_id",
            },
            {
                "repository_id": "repo",
                "table": "integration_parent_episodes",
                "column": "repository_id",
            },
        ]
        assert await db.get_project(PROJECT_ID) is not None

    async def test_refuses_history_on_an_archived_task_with_a_typed_error(self, db):
        await _seed_integration_reference(db, "integration_parent_episodes", "archived-history")
        assert await db.archive_task("archived-history") is True

        with pytest.raises(HierarchyError) as exc:
            await db.delete_project(PROJECT_ID)

        assert exc.value.code == "integration_history_retained"
        assert {
            "task_id": "archived-history",
            "table": "integration_parent_episodes",
            "column": "parent_task_id",
        } in exc.value.context["references"]
        assert await db.get_project(PROJECT_ID) is not None

    async def test_refuses_repository_history_owned_by_another_project(self, db):
        """A foreign task can still leave append-only evidence on this repo."""
        from sqlalchemy import insert

        from src.database.tables import integration_parent_episodes
        from src.models import RepoConfig, RepoSourceType

        await db.create_repo(
            RepoConfig(id="repo", project_id=PROJECT_ID, source_type=RepoSourceType.LINK)
        )
        await db.create_project(Project(id="foreign", name="foreign"))
        await db.create_task(
            Task(
                id="foreign-history",
                project_id="foreign",
                title="foreign history",
                description="foreign history",
                status=TaskStatus.COMPLETED,
            )
        )
        async with db._engine.begin() as conn:
            await conn.execute(
                insert(integration_parent_episodes).values(
                    id="foreign-episode",
                    parent_task_id="foreign-history",
                    repository_id="repo",
                    generation=0,
                    pre_collection_checkpoint_sha="a" * 40,
                    created_at=1.0,
                )
            )

        with pytest.raises(HierarchyError) as exc:
            await db.delete_project(PROJECT_ID)

        assert exc.value.code == "integration_history_retained"
        assert exc.value.context["references"] == [
            {
                "repository_id": "repo",
                "table": "integration_parent_episodes",
                "column": "repository_id",
            }
        ]
        assert await db.get_project(PROJECT_ID) is not None

    async def test_deletes_a_project_without_integration_history(self, db):
        await mktask(db, "ordinary", status=TaskStatus.COMPLETED)

        await db.delete_project(PROJECT_ID)

        assert await db.get_project(PROJECT_ID) is None
