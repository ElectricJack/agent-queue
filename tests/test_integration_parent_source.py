"""Two-child aggregate admission survives an advanced canonical PR head."""


import pytest
from sqlalchemy import select, update

from src.commands.integration_commands import IntegrationCommandsMixin
from src.database import tables as t
from src.integration.models import BranchKey, Fence
from src.integration.ownership import BranchOwnership
from src.integration.records import ParentEpisodeRecords
from src.integration.parent_source import ParentHeadObservation
from src.models import Project
from tests.test_epic_pr_review_evidence import _PollClient
from tests.test_integration_parent_completion import (
    _trusted_check,
)


@pytest.fixture
async def db(reuse_database):
    database = await reuse_database("parent-source.db")
    await database.create_project(Project(id="p", name="parent source"))
    return database


class _Commands(IntegrationCommandsMixin):
    def __init__(self, database, promotion):
        self.db, self.promotion = database, promotion
        self.completion = ParentEpisodeRecords(database)

    def _hierarchy_integration_service(self):
        return self.completion

    def _integration_repository_git(self):
        return self.promotion

    def _integration_promotion_service(self):
        raise AssertionError("train parent completion must use the retained repository Git port")

    async def _integration_delivery_authorized(self, *args):
        return True


async def _finish(case, *, hook=True, parent_id="parent"):
    database, completion = case.db, case.commands.completion
    checkpoint = await database.get_integration_checkpoint(parent_id)
    branch = checkpoint["branch"]
    async with database.immediate() as conn:
        operation = dict(
            (
                await conn.execute(
                    select(t.integration_repair_operations).where(
                        t.integration_repair_operations.c.episode_id == checkpoint["episode_id"],
                    )
                )
            )
            .mappings()
            .one()
        )
        owner = (
            (
                await conn.execute(
                    select(t.integration_branch_owners).where(
                        t.integration_branch_owners.c.ref == branch,
                    )
                )
            )
            .mappings()
            .one()
        )
        fence = await BranchOwnership(database).transfer_detached_on(
            conn,
            Fence(
                target=BranchKey(repository_id="repo", branch=branch),
                owner_id=owner["owner_id"],
                token=owner["fence_token"],
            ),
            operation["verifier_task_id"],
            "verifier",
        )
    assert (await completion.wake_verifier(parent_id, fence))["outcome"] == "woken"
    checkpoint = await database.get_integration_checkpoint(parent_id)
    generation = checkpoint["generation"]
    check_id = f"check-{generation}" if parent_id == "parent" else f"check-{parent_id}-{generation}"
    await _trusted_check(
        database,
        {"operation_id": operation["id"]},
        head_sha=checkpoint["checkpoint_sha"],
        generation=generation,
        parent_task_id=parent_id,
        id=check_id,
        run_id=f"run-{check_id}",
    )
    verified = await completion.verify_parent(
        parent_id,
        generation,
        checkpoint["checkpoint_sha"],
        [check_id],
    )
    assert verified["outcome"] == "verified"
    args = {"task_id": parent_id, "generation": generation, "head_sha": checkpoint["checkpoint_sha"]}
    if hook:
        assert (await case.commands._cmd_integration_complete_parent(args))["success"]
    else:
        assert (await completion.complete_parent(parent_id, generation, args["head_sha"]))[
            "outcome"
        ] == "completed"
    # Session close normally settles the verifier after completing the parent.
    async with database.immediate() as conn:
        await conn.execute(
            update(t.tasks)
            .where(t.tasks.c.id == operation["verifier_task_id"])
            .values(status="COMPLETED")
        )
    return verified






async def _source(case):
    async with case.db._engine.connect() as conn:
        return await case.producer._pull_request_source_on(conn, "parent")




class _ParentClient(_PollClient):
    async def pull_request(self, url):
        pull = await super().pull_request(url)
        pull["head"]["ref"] = "aq/parent"
        return pull
















async def test_parent_source_command_requires_server_principal():
    commands = _Commands(None, None)
    result = await commands.reverify_integration_parent_source(
        ParentHeadObservation("parent", {"review_kind": "parent"}, "b" * 40, 0),
    )
    assert result["success"] is False
    assert result["outcome"] == "unauthorized"
