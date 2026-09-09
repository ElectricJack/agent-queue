from types import SimpleNamespace
import pytest
from sqlalchemy import insert, update
from src.database.tables import integration_branch_owners, task_session_attempts, tasks
from src.integration.ownership import BranchKey, BranchOwnership
from src.models import TaskStatus, Workspace, RepoSourceType
from tests.test_integration_review_evidence import review_case, _git  # noqa: F401

@pytest.mark.parametrize("safe", [True, False, "missing_attempt"])
@pytest.mark.parametrize("pool", [True, False])
@pytest.mark.parametrize("deleted_ref", [True, False])
@pytest.mark.parametrize("reopened", [True, False, "paused_repair"])
@pytest.mark.parametrize("slot_state", ["free", "reused", "locked_branch"])
async def test_stopped_released_workspace_owner_is_reconciled(
    review_case, monkeypatch, safe, pool, deleted_ref, reopened, slot_state  # noqa: F811
):
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock
    from src.integration.completion_recovery import reconcile_closed_integration_owners

    case = review_case
    db = case["db"]
    if reopened:
        await db.update_task(
            "leaf", status=TaskStatus.PAUSED if reopened == "paused_repair" else TaskStatus.READY
        )
    if slot_state == "reused":
        _git(["switch", "main"], case["work"])
    if deleted_ref:
        _git(["switch", "main"], case["work"])
        _git(["branch", "-D", "aq/leaf"], case["work"])
    await db.update_project("p", hierarchical_integration_mode="train")
    await db.update_session("session", task_id="leaf", state="stopped", desired_state="stopped")
    if pool:
        await db.update_session("session", task_id=None, lifecycle="pool")
    async with db.immediate() as conn:
        await conn.execute(
            update(task_session_attempts)
            .where(task_session_attempts.c.session_id == "session")
            .values(task_id="leaf", ended_at=None if safe == "missing_attempt" else 3.0)
        )
    await db.create_workspace(
        Workspace(
            id="ws",
            project_id="p",
            workspace_path=str(case["work"]),
            source_type=RepoSourceType.CLONE,
        )
    )
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_branch_owners).values(
                id="owner",
                repository_id="repo",
                ref="refs/heads/aq/leaf" if reopened == "paused_repair" else "aq/leaf",
                owner_id="leaf",
                owner_role="repair" if reopened == "paused_repair" else "worker",
                fence_token=1,
                handoff_state="attached",
                session_id="session",
                workspace_id="ws",
                created_at=1.0,
                updated_at=1.0,
            )
        )

    @asynccontextmanager
    async def mutex(_path):
        yield

    provider = SimpleNamespace(confirm_stopped=AsyncMock(return_value=safe))
    orch = SimpleNamespace(
        db=db,
        git=case["promotion"].git,
        config=None,
        _git_mutex=mutex,
        session_providers=SimpleNamespace(create=lambda *_: provider),
    )
    if slot_state != "free":
        await db.acquire_workspace("p", "agent", task_id="review", preferred_workspace_id="ws")
    result = await reconcile_closed_integration_owners(orch, "p")
    owner = await BranchOwnership(db).get_owner(
        BranchKey(
            repository_id="repo",
            branch="refs/heads/aq/leaf" if reopened == "paused_repair" else "aq/leaf",
        )
    )
    released = (
        bool(safe)
        and not (safe == "missing_attempt" and (pool or reopened))
        and (slot_state != "locked_branch" or deleted_ref)
    )
    assert owner["handoff_state"] == ("released" if released else "attached")
    assert result == (["leaf"] if released else [])
    assert _git(["rev-parse", "--abbrev-ref", "HEAD"], case["work"]) == (
        "main" if deleted_ref or slot_state == "reused" else "HEAD" if released else "aq/leaf"
    )
    if slot_state != "free":
        ws = await db.get_workspace("ws")
        assert ws.locked_by_task_id == "review"
        assert ws.locked_by_agent_id == "agent"



@pytest.mark.parametrize("moved_head", [False, True])
async def test_flush_recovers_only_exact_completed_root_pr(review_case, monkeypatch, moved_head):  # noqa: F811
    from unittest.mock import AsyncMock
    from src.integration.completion_recovery import recover_completed_pr_links

    case = review_case
    db = case["db"]
    await db.update_project("p", hierarchical_integration_mode="train")
    async with db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "leaf").values(parent_task_id=None))
    pr_url = "https://github.com/acme/widgets/pull/123"
    git = case["promotion"].git
    monkeypatch.setattr(git, "afind_open_pr", AsyncMock(return_value=pr_url))
    monkeypatch.setattr(
        git,
        "aget_pr_identity",
        AsyncMock(
            return_value=SimpleNamespace(
                base_ref="main",
                head_ref="aq/leaf",
                head_oid=case["base"] if moved_head else case["head"],
            )
        ),
    )
    result = await recover_completed_pr_links(db, case["promotion"], "p")
    assert (await db.get_task("leaf")).pr_url == (None if moved_head else pr_url)
    assert result == ([] if moved_head else ["leaf"])
    assert await recover_completed_pr_links(db, case["promotion"], "p") == []
