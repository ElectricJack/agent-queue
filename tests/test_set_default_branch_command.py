"""``set_default_branch`` publishes a new default branch under the exact-OID push contract.

docs/specs/git.md: every daemon push resolves its source once and pushes an
object-ID refspec. Creating the new default branch on origin is one of those
pushes, so it goes through ``apush_validated_ref`` (source already on origin)
or ``apush_validated_delivery`` (source is the workspace HEAD, which nothing
on origin has vetted) — never a raw ``git push -u origin <name>``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.commands.handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.git.manager import GitError
from src.models import Project, RepoSourceType, Workspace
from src.orchestrator import Orchestrator

WS = "/tmp/p1"


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "sdb.db"))
    await d.initialize()
    await d.create_project(Project(id="p1", name="P1", repo_default_branch="main"))
    await d.create_project(Project(id="p2", name="P2", repo_default_branch="main"))
    await d.create_workspace(
        Workspace(id="w1", project_id="p1", workspace_path=WS, source_type=RepoSourceType.CLONE)
    )
    yield d
    await d.close()


@pytest.fixture
def config(tmp_path):
    return AppConfig(
        discord=DiscordConfig(bot_token="t", guild_id="1"),
        workspace_dir=str(tmp_path / "w"),
        database_path=str(tmp_path / "sdb.db"),
        data_dir=str(tmp_path / "d"),
    )


def _git_with_remote_refs(*present: str) -> MagicMock:
    """A GitManager double whose ``rev-parse --verify`` knows only *present* refs."""
    git = MagicMock()
    git.runs: list[list[str]] = []

    async def arun(args, cwd=None, **_kw):
        git.runs.append(list(args))
        if args[:2] == ["rev-parse", "--verify"]:
            if args[2] in present:
                return "a" * 40
            raise GitError(f"fatal: Needed a single revision: {args[2]}")
        assert args[0] != "push", "raw branch-name push is outside the exact-OID contract"
        return ""

    git._arun = arun
    git.apush_validated_ref = AsyncMock(return_value="a" * 40)
    git.apush_validated_delivery = AsyncMock(return_value="b" * 40)
    return git


@pytest.fixture
async def handler(db, config):
    o = Orchestrator(config)
    o.db = db
    o.git = _git_with_remote_refs()
    return CommandHandler(o, config)


@pytest.mark.asyncio
async def test_existing_remote_branch_is_adopted_without_any_push(handler, db):
    handler.orchestrator.git = _git_with_remote_refs("refs/remotes/origin/develop")

    result = await handler.execute("set_default_branch", {"project_id": "p1", "branch": "develop"})

    assert result.get("error") is None
    assert result["status"] == "updated"
    assert result["default_branch"] == "develop"
    assert result["previous_branch"] == "main"
    assert "branch_created" not in result
    git = handler.orchestrator.git
    git.apush_validated_ref.assert_not_awaited()
    git.apush_validated_delivery.assert_not_awaited()
    assert (await db.get_project("p1")).repo_default_branch == "develop"


@pytest.mark.asyncio
async def test_new_branch_from_old_default_is_an_exact_oid_push_of_the_remote_ref(handler):
    handler.orchestrator.git = _git_with_remote_refs("refs/remotes/origin/main^{commit}")

    result = await handler.execute("set_default_branch", {"project_id": "p1", "branch": "develop"})

    assert result.get("error") is None
    assert result["status"] == "updated"
    assert result["branch_created"] is True
    git = handler.orchestrator.git
    # The content is already on origin, so the exact-OID push of the
    # remote-tracking ref is the whole delivery: no reserved-path gate needed.
    git.apush_validated_ref.assert_awaited_once_with(WS, "refs/remotes/origin/main", "develop")
    git.apush_validated_delivery.assert_not_awaited()
    assert not any(run[0] == "branch" for run in git.runs), "no mutable local branch is created"
    assert git.runs[0] == ["fetch", "origin"]


@pytest.mark.asyncio
async def test_new_branch_from_workspace_head_is_a_gated_root_delivery(handler):
    # Neither the new branch nor the recorded old default exists on origin.
    result = await handler.execute("set_default_branch", {"project_id": "p1", "branch": "develop"})

    assert result.get("error") is None
    assert result["status"] == "updated"
    assert result["branch_created"] is True
    git = handler.orchestrator.git
    git.apush_validated_ref.assert_not_awaited()
    # HEAD has never been vetted by origin: resolve it once, gate the whole
    # tree for reserved paths, and push that same OID.
    git.apush_validated_delivery.assert_awaited_once_with(WS, None, "HEAD", "develop")


@pytest.mark.asyncio
async def test_reserved_paths_in_head_refuse_the_switch_and_keep_the_old_default(handler, db):
    git = handler.orchestrator.git
    git.apush_validated_delivery = AsyncMock(
        side_effect=GitError("reserved delivery paths: .aq/claim.json")
    )

    result = await handler.execute("set_default_branch", {"project_id": "p1", "branch": "develop"})

    assert "status" not in result
    assert "reserved delivery paths: .aq/claim.json" in result["error"]
    assert (await db.get_project("p1")).repo_default_branch == "main"


@pytest.mark.asyncio
async def test_project_without_a_workspace_just_records_the_branch(handler, db):
    result = await handler.execute("set_default_branch", {"project_id": "p2", "branch": "develop"})

    assert result.get("error") is None
    assert result["status"] == "updated"
    assert "branch_created" not in result
    assert handler.orchestrator.git.runs == []
    assert (await db.get_project("p2")).repo_default_branch == "develop"
