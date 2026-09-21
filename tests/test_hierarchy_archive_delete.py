"""Delete refuse/cascade and subtree-atomic archive — spec §7."""

from __future__ import annotations

import time

import pytest

from src.database import Database
from src.database.queries.hierarchy_queries import HierarchyError
from src.database.tables import (
    integration_parent_episodes,
    integration_repair_operations,
    repos,
)
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


# ---------------------------------------------------------------------------
# Delete: integration tables that hold a hard FK onto tasks.id
# ---------------------------------------------------------------------------


async def _seed_repo(db, rid: str = "r-1") -> None:
    async with db._engine.begin() as conn:
        await conn.execute(
            repos.insert().values(
                id=rid,
                project_id=PROJECT_ID,
                url="git@example.com:acme/app.git",
                default_branch="main",
                checkout_base_path="/tmp/checkouts",
                source_type="clone",
                source_path="",
            )
        )


async def _seed_parent_episode(
    db, *, task_id: str, episode_id: str = "ep-1", repo_id: str = "r-1"
) -> None:
    """Record a parent-collection episode against *task_id* (RESTRICT FK)."""
    async with db._engine.begin() as conn:
        await conn.execute(
            integration_parent_episodes.insert().values(
                id=episode_id,
                parent_task_id=task_id,
                repository_id=repo_id,
                generation=0,
                pre_collection_checkpoint_sha="a" * 40,
                created_at=time.time(),
            )
        )


async def _seed_batch_repair_operation(
    db, *, verifier_task_id: str, operation_id: str = "op-1", state: str = "completed"
) -> None:
    """Record a settled batch repair whose verifier was *verifier_task_id*."""
    now = time.time()
    async with db._engine.begin() as conn:
        await conn.execute(
            integration_repair_operations.insert().values(
                id=operation_id,
                target_kind="batch",
                batch_id="b-1",
                parent_task_id=None,
                episode_id="ep-batch",
                active_stage=0,
                state=state,
                policy_snapshot={},
                artifact_snapshot={},
                required_check_version="v1",
                verifier_task_id=verifier_task_id,
                created_at=now,
                updated_at=now,
            )
        )


class TestDeleteIntegrationReferences:
    """Integration history pins the tasks it names, on the delete path too.

    Four integration tables carry a ``RESTRICT``/``NO ACTION`` foreign key onto
    ``tasks.id`` that ``_delete_one`` never clears, so the ``DELETE FROM tasks``
    that ends the delete cannot succeed while one of their rows still names the
    task.  The refusal itself is correct and deliberate — the control plane's
    identity may not dangle — but it used to arrive as a raw ``IntegrityError``
    from the last statement of the transaction, and ``_cmd_delete_task`` renders
    only :class:`HierarchyError`; an expected refusal reached the operator as an
    unhandled traceback.  ``archive_task`` already reports these four as
    ``integration_owned``, and the delete path now says the same thing.
    """

    async def test_refuses_a_task_a_parent_episode_records(self, db):
        await _seed_repo(db)
        await mktask(db, "held", status=TaskStatus.COMPLETED)
        await _seed_parent_episode(db, task_id="held", episode_id="ep-1")

        with pytest.raises(HierarchyError) as exc:
            await db.delete_task("held")
        assert exc.value.code == "integration_owned"
        assert "ep-1" in exc.value.detail
        assert "held" in exc.value.detail

        assert await db.get_task("held") is not None

    async def test_refuses_a_task_a_repair_operation_verified(self, db):
        await mktask(db, "held", status=TaskStatus.COMPLETED)
        # ``completed`` so no active-repair guard can be the one refusing.
        await _seed_batch_repair_operation(db, verifier_task_id="held", state="completed")

        with pytest.raises(HierarchyError) as exc:
            await db.delete_task("held")
        assert exc.value.code == "integration_owned"
        assert "op-1" in exc.value.detail

        assert await db.get_task("held") is not None

    async def test_cascade_refuses_a_held_descendant_and_keeps_the_subtree(self, db):
        """A cascade deletes the subtree as one unit, so a held child pins it all."""
        await _seed_repo(db)
        await mktask(db, "p", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "c", status=TaskStatus.READY)
        await db.add_dependency("c", "p", "parent-child")
        await _seed_parent_episode(db, task_id="c", episode_id="ep-1")

        with pytest.raises(HierarchyError) as exc:
            await db.delete_task("p", cascade=True)
        assert exc.value.code == "integration_owned"
        assert "c" in exc.value.detail

        assert await db.get_task("p") is not None
        assert await db.get_task("c") is not None

    async def test_has_children_is_still_reported_before_the_hold(self, db):
        """Ordering is deliberate: the hold is read over what is about to go.

        Without ``--cascade`` the child is not being deleted at all, so naming
        the row that holds *it* would point at something this delete never
        touches.  ``has_children`` is both the true refusal and the actionable
        one; the hold surfaces on the retry that actually asks for the subtree.
        """
        await _seed_repo(db)
        await mktask(db, "p", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "c", status=TaskStatus.READY)
        await db.add_dependency("c", "p", "parent-child")
        await _seed_parent_episode(db, task_id="c", episode_id="ep-1")

        with pytest.raises(HierarchyError) as exc:
            await db.delete_task("p")
        assert exc.value.code == "has_children"

    async def test_a_hold_on_an_unrelated_task_does_not_refuse(self, db):
        """The hold is scoped to the subtree, not to the table being non-empty."""
        await _seed_repo(db)
        await mktask(db, "held", status=TaskStatus.COMPLETED)
        await mktask(db, "free", status=TaskStatus.COMPLETED)
        await _seed_parent_episode(db, task_id="held", episode_id="ep-1")

        await db.delete_task("free")

        assert await db.get_task("free") is None
        assert await db.get_task("held") is not None
