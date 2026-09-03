"""A merged PR is not automatically work on ``main``.

Pkg 4's workers stacked PRs #284/#288/#289 onto
``feature/playbook-v2-pkg4-core``.  The merge sweep merged all three, the
tasks closed COMPLETED, and nothing ever merged ``pkg4-core`` into ``main`` —
so ``main`` lacked ``src/playbooks/executors/agent_task.py`` while every
dependent task's ``pr-merged`` gate had already released it to run.

Two halves are tested here:

* ``pr_merge`` records ``pr_base`` on the task and labels a merge that did
  not target the default branch;
* the gate sweep refuses to resolve a ``pr-merged`` gate until the PR's base
  has itself reached the default branch.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.commands.handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.git.manager import GitError, PullRequestIdentity
from src.models import (
    AgentProfile,
    Project,
    RepoSourceType,
    Task,
    TaskStatus,
    Workspace,
)
from src.orchestrator import Orchestrator
from tests.pg_dsn import ensure_worker_postgres_dsn

POSTGRES_DSN = ensure_worker_postgres_dsn()

PR = "https://github.com/o/r/pull/288"
#: What ``avalidate_pr_for_merge`` resolves ``PR`` to.  ``_cmd_pr_merge``
#: pins the merge to these OIDs, so they have to be well-formed.
PR_IDENTITY = PullRequestIdentity(
    repository="o/r",
    number=288,
    base_ref="feature/pkg4-core",
    base_oid="a" * 40,
    head_ref="task/c5",
    head_oid="b" * 40,
)


@pytest.fixture(params=["sqlite", "postgres"])
async def orch(request, tmp_path):
    """SQLite always; PostgreSQL when ``POSTGRES_TEST_DSN`` is set (CI).

    Both halves write through the database — task metadata on the merge,
    gate rows on the sweep — so both dialects run.
    """
    if request.param == "postgres":
        if not POSTGRES_DSN:
            pytest.skip("POSTGRES_TEST_DSN not set")
        from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter

        db = PostgreSQLDatabaseAdapter(POSTGRES_DSN)
        await db.initialize()
        await db.reset_for_tests()
    else:
        db = Database(str(tmp_path / "base.db"))
        await db.initialize()
    cfg = AppConfig(
        discord=DiscordConfig(bot_token="t", guild_id="1"),
        workspace_dir=str(tmp_path / "w"),
        database_path=str(tmp_path / "base.db"),
        data_dir=str(tmp_path / "d"),
    )
    o = Orchestrator(cfg)
    o.db = db
    o.git = MagicMock()
    # ``pr_merge`` resolves the immutable PR identity before it merges and
    # fails closed when it cannot; a bare MagicMock is not awaitable, so the
    # merge under test would never be reached.
    o.git.avalidate_pr_for_merge = AsyncMock(return_value=PR_IDENTITY)
    o.bus = MagicMock()
    o.bus.emit = AsyncMock()
    o.command_handler = CommandHandler(o, cfg)
    await db.create_project(
        Project(id="p1", name="P1", repo_default_branch="main")
    )
    await db.upsert_profile(AgentProfile(id="worker", name="W"))
    await db.create_workspace(
        Workspace(
            id="w1",
            project_id="p1",
            workspace_path=str(tmp_path / "checkout"),
            source_type=RepoSourceType.CLONE,
        )
    )
    yield o
    await db.close()


async def _task_with_pr(db, task_id: str = "t1") -> str:
    await db.create_task(
        Task(
            id=task_id,
            project_id="p1",
            title="Pkg 4 C5",
            description="d",
            status=TaskStatus.COMPLETED,
            pr_url=PR,
        )
    )
    return task_id


# ---------------------------------------------------------------------------
# pr_merge records the base branch
# ---------------------------------------------------------------------------


async def test_merge_to_a_feature_branch_is_labelled_and_recorded(orch):
    await _task_with_pr(orch.db)
    orch.git.amerge_pr = AsyncMock(
        return_value={"success": True, "sha": "abc", "error": None}
    )
    orch.git.apr_base_ref = AsyncMock(return_value="feature/pkg4-core")

    result = await orch.command_handler.execute(
        "pr_merge", {"project_id": "p1", "pr_url": PR}
    )

    assert result["success"] is True
    assert result["base"] == "feature/pkg4-core"
    assert result["merged_to_default"] is False
    assert result["note"] == "merged to feature/pkg4-core (not main)"
    assert await orch.db.get_task_meta("t1", "pr_base") == "feature/pkg4-core"
    assert await orch.db.get_task_meta("t1", "pr_merged_to_default") is False


async def test_merge_to_the_default_branch_carries_no_warning(orch):
    await _task_with_pr(orch.db)
    orch.git.amerge_pr = AsyncMock(
        return_value={"success": True, "sha": "abc", "error": None}
    )
    orch.git.apr_base_ref = AsyncMock(return_value="main")

    result = await orch.command_handler.execute(
        "pr_merge", {"project_id": "p1", "pr_url": PR}
    )

    assert result["merged_to_default"] is True
    assert "note" not in result
    assert await orch.db.get_task_meta("t1", "pr_merged_to_default") is True


async def test_unknown_base_does_not_fail_the_merge(orch):
    """No ``gh``, no auth: the merge still succeeded and must report so."""
    await _task_with_pr(orch.db)
    orch.git.amerge_pr = AsyncMock(
        return_value={"success": True, "sha": "abc", "error": None}
    )
    orch.git.apr_base_ref = AsyncMock(return_value=None)

    result = await orch.command_handler.execute(
        "pr_merge", {"project_id": "p1", "pr_url": PR}
    )

    assert result["success"] is True
    assert "base" not in result
    assert await orch.db.get_task_meta("t1", "pr_base") is None


async def test_a_failed_merge_records_nothing(orch):
    await _task_with_pr(orch.db)
    orch.git.amerge_pr = AsyncMock(
        return_value={"success": False, "sha": None, "error": "conflicts"}
    )
    orch.git.apr_base_ref = AsyncMock(return_value="feature/pkg4-core")

    result = await orch.command_handler.execute(
        "pr_merge", {"project_id": "p1", "pr_url": PR}
    )

    assert result["success"] is False
    assert await orch.db.get_task_meta("t1", "pr_base") is None


async def test_identity_validation_failure_fails_closed_before_merging(orch):
    """A PR whose base/head pair cannot be validated is never merged."""
    await _task_with_pr(orch.db)
    orch.git.avalidate_pr_for_merge = AsyncMock(side_effect=GitError("head moved"))
    orch.git.amerge_pr = AsyncMock()
    orch.git.apr_base_ref = AsyncMock(return_value="main")

    result = await orch.command_handler.execute(
        "pr_merge", {"project_id": "p1", "pr_url": PR}
    )

    assert result["success"] is False
    assert result["error"] == "Could not validate immutable PR delivery: head moved"
    orch.git.amerge_pr.assert_not_awaited()
    assert await orch.db.get_task_meta("t1", "pr_base") is None


# ---------------------------------------------------------------------------
# the pr-merged gate does not resolve on a stacked merge
# ---------------------------------------------------------------------------


async def _open_pr_gate(db, task_id: str = "waiter") -> str:
    await db.create_task(
        Task(
            id=task_id,
            project_id="p1",
            title="Dependent",
            description="d",
            status=TaskStatus.DEFINED,
        )
    )
    gate_id, _ = await db.create_gate(
        project_id="p1",
        gate_type="pr-merged",
        title="waiting for the PR to merge",
        await_id=PR,
        waiter_task_ids=[task_id],
    )
    return gate_id


def _stub_base(orch, base: str, *, reached: bool):
    orch.git.apr_base_ref = AsyncMock(return_value=base)
    orch.git.ais_ancestor = AsyncMock(return_value=reached)
    orch.git._arun = AsyncMock(return_value="")
    orch.git.avalidate_checkout = AsyncMock(return_value=True)
    orch.git.aget_default_branch = AsyncMock(return_value="main")
    orch._poll_pr_merged = AsyncMock(return_value=True)


async def test_gate_stays_open_while_the_base_has_not_reached_main(orch):
    gate_id = await _open_pr_gate(orch.db)
    _stub_base(orch, "feature/pkg4-core", reached=False)

    await orch._sweep_resolve_pr_ci_gates()

    assert [g["id"] for g in await orch.db.list_open_gates_by_type("pr-merged")] == [
        gate_id
    ]


async def test_gate_resolves_once_the_base_reaches_main(orch):
    await _open_pr_gate(orch.db)
    _stub_base(orch, "feature/pkg4-core", reached=True)

    await orch._sweep_resolve_pr_ci_gates()

    assert await orch.db.list_open_gates_by_type("pr-merged") == []


async def test_gate_resolves_normally_for_a_pr_targeting_main(orch):
    await _open_pr_gate(orch.db)
    _stub_base(orch, "main", reached=False)  # ancestry never consulted

    await orch._sweep_resolve_pr_ci_gates()

    assert await orch.db.list_open_gates_by_type("pr-merged") == []


async def test_unknowable_base_still_resolves_the_gate(orch):
    """Offline must not wedge every dependent task shut forever."""
    await _open_pr_gate(orch.db)
    _stub_base(orch, "feature/pkg4-core", reached=False)
    orch.git.apr_base_ref = AsyncMock(return_value=None)

    await orch._sweep_resolve_pr_ci_gates()

    assert await orch.db.list_open_gates_by_type("pr-merged") == []
