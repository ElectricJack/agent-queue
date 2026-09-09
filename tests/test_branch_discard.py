"""The drain that removes branches an operator asked to discard.

Deleting a task may also delete the branch it put on the remote, but only on
an explicit choice; ``guard_integration_mutation`` records that choice on the
retired origin row and
:class:`~src.integration.branch_discard.BranchDiscardService` is what carries
it out.  See
``docs/superpowers/specs/2026-09-08-task-deletion-with-materialized-branches-design.md``.
"""

from __future__ import annotations

import time
import uuid

import pytest
from sqlalchemy import insert, select, update

from src.database import Database
from src.database.tables import integration_branch_owners, repos, task_branch_origins
from src.git.github_app import GitHubRepositoryBinding
from src.git.manager import GitError
from src.integration.branch_discard import MAX_ATTEMPTS, BranchDiscardService
from src.models import Project, RepoConfig, RepoSourceType
from tests.db_fixtures import lease_dsn

BASE = "a" * 40
MOVED = "b" * 40
BINDING = GitHubRepositoryBinding(1234, "owner/repository")


class _Client:
    """Stands in for the installation-bound GitHub client."""

    def __init__(self, head: str | None = BASE, *, moves_to: str | None = None):
        self.repository = BINDING
        self._heads = [head] if moves_to is None else [head, moves_to]
        self.reads = 0

    async def exact_head_ref(self, branch: str) -> str | None:
        head = self._heads[min(self.reads, len(self._heads) - 1)]
        self.reads += 1
        return head

    async def installation_token(self) -> str:
        return "token"


class _Git:
    """Records deletes; optionally fails the way a real transport does."""

    def __init__(self, *, error: Exception | None = None):
        self.error = error
        self.deleted: list[tuple[str, str]] = []

    async def adelete_ref_with_app_auth(
        self, checkout, *, repository, token, branch, expected_old_oid
    ):
        if self.error is not None:
            raise self.error
        self.deleted.append((branch, expected_old_oid))
        return expected_old_oid


@pytest.fixture
async def db(tmp_path):
    database = Database(lease_dsn("branch-discard.db"))
    await database.initialize()
    await database.create_project(Project(id="p", name="discard"))
    await database.create_repo(
        RepoConfig(
            id="repo",
            project_id="p",
            source_type=RepoSourceType.LINK,
            source_path=str(tmp_path),
            default_branch="main",
        )
    )
    yield database
    await database.close()


async def _pending_origin(db, *, task_id: str = "gone", attempts: int = 0) -> str:
    """A retired, materialized origin marked for discard — what a delete leaves."""
    origin_id = str(uuid.uuid4())
    async with db.immediate() as conn:
        await conn.execute(
            insert(task_branch_origins).values(
                id=origin_id,
                task_id=task_id,
                repository_id="repo",
                parent_task_id=None,
                parent_ref="main",
                base_sha=BASE,
                creation_generation=0,
                reserved=True,
                materialized=True,
                materialized_at=1.0,
                created_at=1.0,
                retired_at=2.0,
                discard_state="pending",
                discard_requested_at=2.0,
                discard_attempts=attempts,
                discard_next_attempt_at=0.0,
            )
        )
    return origin_id


async def _row(db, origin_id: str) -> dict:
    async with db._engine.connect() as conn:
        return dict(
            (
                await conn.execute(
                    select(task_branch_origins).where(task_branch_origins.c.id == origin_id)
                )
            ).mappings().one()
        )


def _service(db, tmp_path, *, git=None, client=None) -> BranchDiscardService:
    return BranchDiscardService(
        db,
        data_dir=tmp_path,
        git_manager=git if git is not None else _Git(),
        app_client_factory=lambda binding: client if client is not None else _Client(),
        repository_binding_resolver=lambda repository: BINDING,
        clock=lambda: 1000.0,
    )


async def test_a_pending_discard_deletes_the_ref_under_the_observed_head(db, tmp_path):
    origin_id = await _pending_origin(db)
    git = _Git()
    service = _service(db, tmp_path, git=git)

    [result] = await service.drain_due()

    assert result.outcome == "complete"
    assert result.branch == "aq/gone"
    # The delete is fenced on what was actually observed, never on a guess.
    assert git.deleted == [("aq/gone", BASE)]
    assert (await _row(db, origin_id))["discard_state"] == "complete"


async def test_an_already_absent_ref_is_complete_not_an_error(db, tmp_path):
    origin_id = await _pending_origin(db)
    git = _Git()
    service = _service(db, tmp_path, git=git, client=_Client(head=None))

    [result] = await service.drain_due()

    assert result.outcome == "complete"
    assert git.deleted == []
    assert (await _row(db, origin_id))["discard_state"] == "complete"


async def test_a_ref_that_moved_during_the_delete_is_parked_as_a_conflict(db, tmp_path):
    origin_id = await _pending_origin(db)
    service = _service(
        db,
        tmp_path,
        git=_Git(error=GitError("stale")),
        client=_Client(head=BASE, moves_to=MOVED),
    )

    [result] = await service.drain_due()

    assert result.outcome == "conflict"
    row = await _row(db, origin_id)
    assert row["discard_state"] == "conflict"
    assert row["discard_last_error"] == "branch moved during discard"


async def test_a_live_branch_owner_blocks_the_discard(db, tmp_path):
    """Something re-acquired the ref; deleting it would lose unasked-for work."""
    origin_id = await _pending_origin(db)
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_branch_owners).values(
                id="owner-1",
                repository_id="repo",
                ref="aq/gone",
                owner_id="someone-else",
                owner_role="repair",
                handoff_state="reserved",
                fence_token=1,
                created_at=1.0,
                updated_at=1.0,
            )
        )
    git = _Git()

    [result] = await _service(db, tmp_path, git=git).drain_due()

    assert result.outcome == "conflict"
    assert git.deleted == []
    assert (await _row(db, origin_id))["discard_last_error"] == "branch has an active owner"


async def test_the_default_branch_is_never_discarded(db, tmp_path):
    """Unreachable today (a task branch is ``aq/<id>``), and catastrophic if
    branch naming ever made it reachable — so the check holds regardless."""
    async with db.immediate() as conn:
        await conn.execute(
            update(repos).where(repos.c.id == "repo").values(default_branch="aq/gone")
        )
    origin_id = await _pending_origin(db)
    git = _Git()

    [result] = await _service(db, tmp_path, git=git).drain_due()

    assert result.outcome == "conflict"
    assert git.deleted == []
    row = await _row(db, origin_id)
    assert row["discard_last_error"] == "default branch discard is forbidden"


async def test_transport_failure_retries_with_backoff(db, tmp_path):
    origin_id = await _pending_origin(db)
    service = _service(db, tmp_path, git=_Git(error=RuntimeError("network down")))

    [result] = await service.drain_due()

    assert result.outcome == "retryable"
    row = await _row(db, origin_id)
    assert row["discard_state"] == "pending"
    assert row["discard_attempts"] == 1
    assert row["discard_next_attempt_at"] > 1000.0
    assert "network down" in row["discard_last_error"]


async def test_a_discard_that_never_succeeds_is_parked_for_an_operator(db, tmp_path):
    origin_id = await _pending_origin(db, attempts=MAX_ATTEMPTS - 1)
    service = _service(db, tmp_path, git=_Git(error=RuntimeError("network down")))

    [result] = await service.drain_due()

    assert result.outcome == "failed"
    assert (await _row(db, origin_id))["discard_state"] == "failed"


async def test_a_backed_off_row_is_not_attempted_early(db, tmp_path):
    origin_id = await _pending_origin(db)
    async with db.immediate() as conn:
        await conn.execute(
            update(task_branch_origins)
            .where(task_branch_origins.c.id == origin_id)
            .values(discard_next_attempt_at=9_999_999.0)
        )
    git = _Git()

    assert await _service(db, tmp_path, git=git).drain_due() == []
    assert git.deleted == []


async def test_only_pending_rows_are_drained(db, tmp_path):
    """A retired origin the operator chose to keep is not the drain's business."""
    origin_id = await _pending_origin(db)
    async with db.immediate() as conn:
        await conn.execute(
            update(task_branch_origins)
            .where(task_branch_origins.c.id == origin_id)
            .values(discard_state=None, discard_requested_at=None)
        )
    git = _Git()

    assert await _service(db, tmp_path, git=git).drain_due() == []
    assert git.deleted == []


async def test_an_unavailable_transport_retries_rather_than_giving_up(db, tmp_path):
    origin_id = await _pending_origin(db)
    service = BranchDiscardService(
        db,
        data_dir=tmp_path,
        git_manager=_Git(),
        app_client_factory=None,
        repository_binding_resolver=lambda repository: BINDING,
        clock=time.time,
    )

    [result] = await service.drain_due()

    assert result.outcome == "retryable"
    row = await _row(db, origin_id)
    assert row["discard_state"] == "pending"
    assert row["discard_last_error"] == "authenticated discard transport is unavailable"
