"""Dotted child ids from tasks.next_child_ordinal — spec §6."""

from __future__ import annotations

import asyncio
import time

import pytest
from sqlalchemy import insert

from src import task_names
from src.database import Database
from src.database.tables import (
    integration_branch_owners,
    task_branch_origins,
    task_integration_checkpoints,
)
from src.integration.models import BranchKey
from src.integration.ownership import BranchOwnership
from src.models import Project, Task
from tests.db_fixtures import lease_dsn

PROJECT_ID = "proj"


@pytest.fixture
async def db(tmp_path):
    database = Database(lease_dsn("test.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT_ID, name="p"))
    yield database
    await database.close()


async def mktask(db, tid):
    await db.create_task(Task(id=tid, project_id=PROJECT_ID, title=tid, description=tid))


def test_naming_depth():
    assert task_names.naming_depth("swift-falcon") == 1
    assert task_names.naming_depth("swift-falcon.2") == 2
    assert task_names.naming_depth("swift-falcon.2.1") == 3


class TestReserveChildOrdinal:
    async def test_ordinals_are_sequential_and_never_reused(self, db):
        await mktask(db, "p")
        async with db._engine.begin() as conn:
            first = await task_names.reserve_child_ordinal(conn, "p")
            second = await task_names.reserve_child_ordinal(conn, "p")
        assert (first, second) == (1, 2)
        # A deleted sibling's ordinal is not reused.
        async with db._engine.begin() as conn:
            third = await task_names.reserve_child_ordinal(conn, "p")
        assert third == 3

    async def test_unknown_parent_raises(self, db):
        async with db._engine.begin() as conn:
            with pytest.raises(KeyError):
                await task_names.reserve_child_ordinal(conn, "nope")


class TestChildTaskId:
    async def test_dotted_id_under_root(self, db):
        await mktask(db, "p")
        async with db._engine.begin() as conn:
            cid, capped = await task_names.child_task_id(conn, "p")
        assert (cid, capped) == ("p.1", False)

    async def test_naming_depth_cap_falls_back_to_root_id(self, db):
        await mktask(db, "a.1.1")
        async with db._engine.begin() as conn:
            cid, capped = await task_names.child_task_id(conn, "a.1.1")
        assert capped is True
        assert "." not in cid

    async def test_concurrent_reservations_are_unique(self, db):
        await mktask(db, "p")

        async def one():
            async with db._engine.begin() as conn:
                return await task_names.reserve_child_ordinal(conn, "p")

        ords = await asyncio.gather(*(one() for _ in range(10)))
        assert sorted(ords) == list(range(1, 11))


# A root id is drawn from ~900 adjective-noun names, so a deleted task's name
# comes round again.  What must not come round with it is the integration
# identity keyed by that name: checkpoints, branch origins and branch fences
# outlive a deleted task.  Observed live (sharp-willow, steady-willow): the
# task deleted on 2026-09-09 left all three; the name was minted again on
# 2026-09-24 and the new task's train-mode close verified the predecessor's
# released fence and refused, "no longer owns its delivery branch fence".

_RESIDUE_KINDS = ("checkpoint", "origin", "retired_origin", "owner")


async def _leave_integration_identity(db, tid: str, kinds) -> None:
    now = time.time()
    async with db._engine.begin() as conn:
        if "checkpoint" in kinds:
            await conn.execute(insert(task_integration_checkpoints).values(
                task_id=tid, repository_id="repo", branch=f"aq/{tid}",
                checkpoint_sha="a" * 40, branch_owner_id=tid, updated_at=now,
            ))
        if "origin" in kinds or "retired_origin" in kinds:
            await conn.execute(insert(task_branch_origins).values(
                id=f"origin-{tid}", task_id=tid, repository_id="repo",
                branch_name=f"aq/{tid}", parent_ref="main", base_sha="a" * 40,
                creation_generation=0, reserved=True, materialized=True,
                created_at=now, materialized_at=now,
                retired_at=now if "retired_origin" in kinds else None,
            ))
        if "owner" in kinds:
            await conn.execute(insert(integration_branch_owners).values(
                id=f"owner-{tid}", repository_id="repo", ref=f"aq/{tid}",
                owner_id=tid, owner_role="worker", fence_token=3,
                handoff_state="released", created_at=now, updated_at=now,
            ))


def _draw(monkeypatch, *names: str) -> None:
    """Make ``fresh_root_id`` draw *names* in order (adjective, then noun)."""
    words = iter(part for name in names for part in name.split("-", 1))
    monkeypatch.setattr(task_names.random, "choice", lambda _pool: next(words))


class TestDeletedIntegrationIdentityIsNeverReMinted:
    async def test_sharp_willow_reuse_is_not_minted_onto_the_predecessor(
        self, db, monkeypatch
    ):
        """Reproduce the incident through the real delete path."""
        await mktask(db, "sharp-willow")
        await _leave_integration_identity(db, "sharp-willow", {"checkpoint", "origin", "owner"})
        await db.delete_task("sharp-willow")
        assert await db.get_task("sharp-willow") is None
        assert await db.get_integration_checkpoint("sharp-willow") is not None

        _draw(monkeypatch, "sharp-willow", "calm-flare")
        async with db._engine.begin() as conn:
            minted = await task_names.fresh_root_id(conn)

        assert minted == "calm-flare"
        await mktask(db, minted)
        assert await db.get_integration_checkpoint(minted) is None
        assert await BranchOwnership(db).get_owner(
            BranchKey(repository_id="repo", branch=f"aq/{minted}")
        ) is None

    @pytest.mark.parametrize("kind", _RESIDUE_KINDS)
    async def test_each_identity_row_reserves_the_name(self, db, monkeypatch, kind):
        await _leave_integration_identity(db, "sharp-willow", {kind})
        _draw(monkeypatch, "sharp-willow", "calm-flare")
        async with db._engine.begin() as conn:
            assert await task_names.fresh_root_id(conn) == "calm-flare"

    async def test_a_name_without_identity_is_still_minted(self, db, monkeypatch):
        await mktask(db, "gone-task")
        await db.delete_task("gone-task")
        _draw(monkeypatch, "gone-task")
        async with db._engine.begin() as conn:
            assert await task_names.fresh_root_id(conn) == "gone-task"

    async def test_child_ordinal_skips_a_predecessors_identity(self, db):
        """A re-filed parent restarts its ordinals; ``p.1``'s identity survives."""
        await mktask(db, "p")
        await _leave_integration_identity(db, "p.1", {"checkpoint"})
        async with db._engine.begin() as conn:
            cid, capped = await task_names.child_task_id(conn, "p")
        assert (cid, capped) == ("p.2", False)
