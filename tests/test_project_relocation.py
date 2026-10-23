"""Real Git and disposable PostgreSQL storage-cutover regressions."""
from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.build_cache import resolved_cache_settings
from src.config import AppConfig, BuildCacheConfig, WorkspacesConfig
from src.database import Database
from src.git.manager import GitManager
from src.integration.development import DevelopmentPrimitives
from src.integration.repository_git import RepositoryGit
from src.models import (
    Project,
    ProjectStatus,
    RepoConfig,
    RepoSourceType,
    Task,
    TaskStatus,
    Workspace,
)
from src.projects.relocation import ProjectRelocation, RelocationRefused, tree_manifest
from src.projects.storage_binding import bound_path, read_binding, repository_path
from tests.db_fixtures import lease_dsn


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()


def commit(path, name="source.txt", content="source"):
    (path / name).write_text(content)
    git(path, "add", name)
    git(path, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.com",
        "commit", "-qm", "fixture")
    return git(path, "rev-parse", "HEAD")


@pytest.fixture
async def relocation(tmp_path):
    db = Database(lease_dsn("relocation"))
    await db.initialize()
    source = tmp_path / "mnt" / "d" / "matter-engine-cpp"
    source.mkdir(parents=True)
    git(source, "init", "-q")
    commit(source)
    (source / ".gitignore").write_text("ignored/\n")
    git(source, "add", ".gitignore")
    git(source, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.com",
        "commit", "-qm", "ignore build assets")
    git(source, "update-ref", "refs/aq/provenance/source", "HEAD")
    (source / "ignored").mkdir()
    (source / "ignored" / "asset.bin").write_bytes(b"ignored asset\x00")
    # Full copies include LFS content, unlike a bundle.
    (source / ".git" / "lfs" / "objects").mkdir(parents=True)
    (source / ".git" / "lfs" / "objects" / "fixture").write_bytes(b"lfs content")
    cache = tmp_path / "old-cache"
    cache.mkdir()
    (cache / "cached-object").write_bytes(b"compiler cache")
    config = AppConfig(data_dir=str(tmp_path / "data"), workspaces=WorkspacesConfig(
        projects={"matter-engine-cpp": BuildCacheConfig(dir=str(cache))}))
    await db.create_project(Project(id="matter-engine-cpp", name="Fixture only",
                                    status=ProjectStatus.PAUSED))
    await db.create_repo(RepoConfig(id="repo", project_id="matter-engine-cpp",
                                    source_type=RepoSourceType.LINK, source_path=str(source),
                                    checkout_base_path=str(source), url="https://example.com/fixture.git"))
    await db.create_workspace(Workspace(id="base", project_id="matter-engine-cpp",
                                        source_type=RepoSourceType.LINK, workspace_path=str(source)))
    policy = {"artifact": "windows-fixture", "required": ["linux", "windows-msvc-package"]}
    green = {"linux", "windows-msvc-package"}

    async def read_policy(project):
        return json.loads(json.dumps(policy))

    async def check(plan, paths):
        if not set(plan["policy"]["required"]) <= green:
            raise RelocationRefused("missing currently required CI")

    service = ProjectRelocation(db, config, GitManager(), policy_reader=read_policy,
                                checks_reader=check, quiescent=AsyncMock(return_value=True))
    yield SimpleNamespace(db=db, config=config, source=source, cache=cache, service=service,
                          root=tmp_path / "native", policy=policy, green=green,
                          owner={"services": [], "fixture_only": True}, pid="matter-engine-cpp")
    await db.close()


async def preview(f, root=None):
    return await f.service.preview(f.pid, str(root or f.root), owner_inventory=f.owner)


async def apply(f, plan, root=None):
    return await f.service.apply(f.pid, str(root or f.root),
                                 expected_digest=plan["digest"], owner_inventory=f.owner)


async def test_preview_is_read_only_and_apply_preserves_every_path_family(relocation):
    f = relocation
    before = tree_manifest(f.source)
    plan = await preview(f)
    assert not f.root.exists()
    assert (await f.db.get_repo("repo")).source_path == str(f.source)
    result = await apply(f, plan)
    binding = result["binding"]
    assert binding["generation"] == 1
    new = bound_path(binding, f.source)
    assert tree_manifest(new) == before == tree_manifest(f.source)
    assert tree_manifest(f.root / "backup" / new.name) == before
    assert (await f.db.get_repo("repo")).checkout_base_path == str(new)
    assert (await f.db.get_workspace("base")).workspace_path == str(new)
    assert git(new, "rev-parse", "refs/aq/provenance/source") == git(f.source, "rev-parse", "HEAD")
    assert (new / ".git/lfs/objects/fixture").read_bytes() == b"lfs content"
    cache = await resolved_cache_settings(f.db, f.config, f.pid)
    assert cache.dir == str(bound_path(binding, f.cache))
    retained = await RepositoryGit(f.db, data_dir=f.config.data_dir,
                                   git_manager=f.service.git)._resolve_repository("repo")
    assert retained.retained_git_dir.is_relative_to(f.root)
    default = DevelopmentPrimitives(f.db, data_dir=f.config.data_dir, git=f.service.git)._store_path(
        await f.db.get_repo("repo"))
    assert (await repository_path(f.db, "repo", default)).is_relative_to(f.root)
    for path in binding["paths"].values():
        assert "/mnt/d/" not in str(Path(path).resolve())
    assert f.config.workspaces.projects[f.pid].dir == str(f.cache)
    assert (await f.db.get_project(f.pid)).hierarchical_integration_mode == "disabled"
    assert (await apply(f, plan))["already_applied"]


async def test_missing_current_windows_check_refuses_and_linux_only_edit_does_not(relocation):
    f = relocation
    f.green.remove("windows-msvc-package")
    plan = await preview(f)
    with pytest.raises(RelocationRefused, match="required CI"):
        await apply(f, plan)
    assert await read_binding(f.db, f.pid) is None
    f.policy.update(artifact="authorized-linux-only", required=["linux"])
    with pytest.raises(RelocationRefused, match="stale preview"):
        await apply(f, plan)
    root = f.root.parent / "linux-only"
    plan = await preview(f, root)
    assert (await apply(f, plan, root))["success"]


async def test_dirty_refuses_but_retains_verified_non_git_backup(relocation):
    f = relocation
    (f.source / "source.txt").write_text("uncommitted new work")
    (f.source / "untracked").write_text("untracked new work")
    plan = await preview(f)
    with pytest.raises(RelocationRefused, match="dirty worktree; full verified backup"):
        await apply(f, plan)
    assert await read_binding(f.db, f.pid) is None
    name = Path(plan["plan"]["paths"][str(f.source)]).name
    assert tree_manifest(f.root / "backup" / name) == tree_manifest(f.source)


@pytest.mark.parametrize("phase", ["prepared", "before_commit"])
async def test_crash_never_publishes_partial_binding_and_restart_reuses_verified_copies(relocation, phase):
    f = relocation
    plan = await preview(f)

    async def crash(actual):
        if actual == phase:
            raise RuntimeError("injected crash")
    f.service.crash_hook = crash
    with pytest.raises(RuntimeError, match="injected crash"):
        await apply(f, plan)
    assert await read_binding(f.db, f.pid) is None
    assert (await f.db.get_workspace("base")).workspace_path == str(f.source)
    assert (await f.db.get_repo("repo")).source_path == str(f.source)
    f.service.crash_hook = None
    assert (await apply(f, plan))["binding"]["generation"] == 1


async def test_reverse_plan_preserves_new_commits_ignored_files_and_original_backup(relocation):
    f = relocation
    first = await apply(f, await preview(f))
    new = bound_path(first["binding"], f.source)
    head = commit(new, content="new work since relocation")
    (new / "ignored/asset.bin").write_bytes(b"new ignored build")
    rollback = f.root.parent / "rollback-fresh"
    second = await apply(f, await preview(f, rollback), rollback)
    restored = bound_path(second["binding"], f.source)
    assert git(restored, "rev-parse", "HEAD") == head
    assert (restored / "ignored/asset.bin").read_bytes() == b"new ignored build"
    assert (f.source / "source.txt").read_text() == "source"
    assert second["binding"]["generation"] == 2
    assert (await resolved_cache_settings(f.db, f.config, f.pid)).dir.startswith(str(rollback))
    with pytest.raises(RelocationRefused, match="occupied"):
        await preview(f, f.source)


@pytest.mark.parametrize("change", ["content", "config", "policy", "repo"])
async def test_stale_preview_refuses_without_destination_mutation(relocation, change):
    f = relocation
    plan = await preview(f)
    if change == "content":
        (f.source / "ignored/asset.bin").write_bytes(b"changed")
    elif change == "config":
        f.config.workspaces.projects[f.pid].kind = "sccache"
    elif change == "policy":
        f.policy["artifact"] = "new-policy"
    else:
        await f.db.update_repo("repo", default_branch="changed")
    with pytest.raises(RelocationRefused, match="stale preview"):
        await apply(f, plan)
    assert not f.root.exists()


@pytest.mark.parametrize("change", ["claim", "process", "destination", "source"])
async def test_fresh_precommit_checks_refuse_new_writer_or_changed_copy(relocation, change):
    f = relocation
    plan = await preview(f)

    async def mutate(phase):
        if phase != "prepared":
            return
        if change == "claim":
            await f.db.create_task(Task(id="live", project_id=f.pid, title="new claim", description="fixture",
                                       status=TaskStatus.IN_PROGRESS))
        elif change == "process":
            f.service.quiescent.return_value = False
        else:
            path = (Path(plan["plan"]["paths"][str(f.source)])
                    if change == "destination" else f.source)
            (path / "ignored/asset.bin").write_bytes(b"new writer")
    f.service.crash_hook = mutate
    with pytest.raises(RelocationRefused):
        await apply(f, plan)
    assert await read_binding(f.db, f.pid) is None
    assert (await f.db.get_repo("repo")).source_path == str(f.source)


async def test_reader_snapshot_is_one_generation_during_transaction(relocation):
    f = relocation
    entered, release = asyncio.Event(), asyncio.Event()
    plan = await preview(f)

    async def pause(phase):
        if phase == "before_commit":
            entered.set()
            await release.wait()
    f.service.crash_hook = pause
    writer = asyncio.create_task(apply(f, plan))
    try:
        await asyncio.wait_for(entered.wait(), 15)
        snapshot = await f.service.snapshot(f.pid)
        assert snapshot["project"]["storage_binding"] is None
        assert snapshot["repos"][0]["source_path"] == str(f.source)
        assert snapshot["workspaces"][0]["workspace_path"] == str(f.source)
        release.set()
        await writer
        snapshot = await f.service.snapshot(f.pid)
        binding = snapshot["project"]["storage_binding"]
        assert snapshot["repos"][0]["source_path"] == str(bound_path(binding, f.source))
        assert snapshot["workspaces"][0]["workspace_path"] == str(bound_path(binding, f.source))
    finally:
        release.set()
        await writer


async def test_concurrent_plans_only_one_generation_can_commit(relocation):
    f = relocation
    second_root = f.root.parent / "other-attempt"
    first, second = await preview(f), await preview(f, second_root)
    results = await asyncio.gather(apply(f, first), apply(f, second, second_root),
                                   return_exceptions=True)
    assert sum(isinstance(r, dict) and r["success"] for r in results) == 1
    assert (await read_binding(f.db, f.pid))["generation"] == 1


@pytest.mark.parametrize("kind", ["workspace", "cache", "global-recovery"])
async def test_shared_ownership_refuses(relocation, kind):
    f = relocation
    await f.db.create_project(Project(id="other", name="Other project"))
    if kind == "workspace":
        await f.db.create_workspace(Workspace(id="other-ws", project_id="other",
            source_type=RepoSourceType.LINK, workspace_path=str(f.source)))
    elif kind == "cache":
        f.config.workspaces.projects["other"] = BuildCacheConfig(dir=str(f.cache))
    else:
        path = Path(f.config.data_dir) / "branch-backups"
        path.mkdir(parents=True)
        (path / "unknown-owner.bundle").write_bytes(b"retained")
    with pytest.raises(RelocationRefused, match="shared"):
        await preview(f)
    assert not f.root.exists()


@pytest.mark.parametrize("kind", ["claim", "linked", "symlink", "alternates"])
async def test_unmanaged_pointers_and_claim_files_are_never_rewritten(relocation, kind):
    f = relocation
    if kind == "claim":
        (f.source / ".aq").mkdir()
        (f.source / ".aq/claim.json").write_text('{"task_id":"held"}')
    elif kind == "linked":
        git(f.source, "worktree", "add", "--detach", str(f.source / "linked"), "HEAD")
    elif kind == "symlink":
        (f.source / "ignored/link").symlink_to(f.source / "source.txt")
    else:
        (f.source / ".git/objects/info/alternates").write_text(str(f.source / ".git/objects"))
    before = tree_manifest(f.source)
    plan = await preview(f)
    with pytest.raises(RelocationRefused):
        await apply(f, plan)
    assert tree_manifest(f.source) == before
    assert await read_binding(f.db, f.pid) is None


async def test_unrelated_project_paths_and_default_cache_unchanged(relocation):
    f = relocation
    await f.db.create_project(Project(id="other", name="Other"))
    f.config.workspaces.projects["other"] = BuildCacheConfig(dir=str(f.root.parent / "other-cache"))
    await apply(f, await preview(f))
    assert await read_binding(f.db, "other") is None
    assert (await resolved_cache_settings(f.db, f.config, "other")).dir.endswith("other-cache")


async def test_schema_column_is_jsonb_on_disposable_database(relocation):
    import importlib.util

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text

    f = relocation
    path = Path(__file__).parents[1] / "migrations/versions/a00000000118_project_storage_binding.py"
    spec = importlib.util.spec_from_file_location("relocation_revision", path)
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)

    def migrate(connection):
        revision.op = Operations(MigrationContext.configure(connection))
        # The squashed baseline already includes the column; both paths must work.
        revision.upgrade()
        revision.downgrade()
        revision.downgrade()
        revision.upgrade()
        revision.upgrade()

    async with f.db._engine.begin() as conn:
        await conn.run_sync(migrate)
        value = await conn.scalar(text("SELECT data_type FROM information_schema.columns "
                                      "WHERE table_name='projects' AND column_name='storage_binding'"))
    assert value == "jsonb"


async def test_command_uses_current_playbook_and_trusted_exact_head_evidence(relocation, monkeypatch):
    import time

    from sqlalchemy import insert

    from src.commands.project_relocation_commands import ProjectRelocationCommandsMixin
    from src.database.tables import integration_check_evidence
    from src.integration.models import RequiredCheckSet
    from src.playbooks.artifact_store import ArtifactStore

    f = relocation
    required = RequiredCheckSet(version="windows-v1", names=("linux", "windows-msvc-package"),
                                 producer_id="trusted-runner")
    definition = SimpleNamespace(scope=SimpleNamespace(project_id=f.pid),
        integration_policy=SimpleNamespace(tables={"root_batch": SimpleNamespace(required_checks=required)}))
    monkeypatch.setattr(ArtifactStore, "load", lambda *args: definition)
    monkeypatch.setattr(ProjectRelocation, "_stopped", AsyncMock())
    await f.db.update_project(f.pid, hierarchical_integration_policy={"development": {"route": {
        "artifact": {"playbook_id": "fixture", "artifact_sha256": "sha256:" + "a" * 64}}}})
    handler = ProjectRelocationCommandsMixin()
    handler.db, handler.config = f.db, f.config
    handler.orchestrator = SimpleNamespace(git=f.service.git)
    args = {"project_id": f.pid, "destination": str(f.root), "owner_inventory": f.owner}
    plan = await handler._cmd_project_relocate(args)
    assert plan["success"], plan
    sha = git(f.source, "rev-parse", "HEAD")
    async with f.db._engine.begin() as conn:
        for name, producer in (("linux", "trusted-runner"), ("windows-msvc-package", "foreign")):
            await conn.execute(insert(integration_check_evidence).values(
                id=name, repository_id="repo", sha=sha, check_name=name, producer_id=producer,
                workflow_id="native", run_id=name, attempt=1, required_check_version="windows-v1",
                checks={}, conclusion="success", classification="green", observed_at=time.time() - 3600))
    failed = await handler._cmd_project_relocate({**args, "apply": True,
                                                "expected_digest": plan["digest"]})
    assert not failed["success"]
    assert "required exact-SHA CI" in failed["error"]
    # An authorized current policy removes the obsolete Windows requirement.
    definition.integration_policy.tables["root_batch"].required_checks = RequiredCheckSet(
        version="windows-v1", names=("linux",), producer_id="trusted-runner")
    root = f.root.parent / "current-linux-policy"
    args["destination"] = str(root)
    plan = await handler._cmd_project_relocate(args)
    result = await handler._cmd_project_relocate({**args, "apply": True,
                                                "expected_digest": plan["digest"]})
    assert result["success"]


async def test_command_rejects_agent_authority(relocation):
    from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
    from src.commands.project_relocation_commands import ProjectRelocationCommandsMixin
    from src.profiles.capabilities import DENY_ALL

    with principal_context(ExecutionPrincipal(kind=PrincipalKind.SESSION, session_id="worker",
                                              policy=DENY_ALL)):
        result = await ProjectRelocationCommandsMixin()._cmd_project_relocate({})
    assert not result["success"]
    assert "local operator" in result["error"]


async def test_remote_heads_must_be_preserved_and_rechecked(relocation, tmp_path):
    f = relocation
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", "-q", str(remote))
    git(f.source, "remote", "add", "origin", str(remote))
    git(f.source, "push", "-q", "origin", "HEAD:refs/heads/main")
    plan = await preview(f)
    remote_heads = plan["plan"]["inventory"][str(f.source)]["git"][0]["remote_heads"]
    assert remote_heads == {"refs/heads/main": git(f.source, "rev-parse", "HEAD")}

    async def change_remote(stage):
        if stage == "prepared":
            git(remote, "update-ref", "refs/heads/new", remote_heads["refs/heads/main"])

    f.service.crash_hook = change_remote
    with pytest.raises(RelocationRefused, match="remote heads changed"):
        await apply(f, plan)
    assert await read_binding(f.db, f.pid) is None
    # A new remote commit absent from the current clone must be fetched/retained.
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", "--branch", "main", str(remote), str(other))
    commit(other, "new.txt", "remote work")
    git(other, "push", "-q", "origin", "HEAD:main")
    plan = await preview(f, tmp_path / "fresh")
    assert any("fetch and retain" in blocker for blocker in plan["blockers"])


@pytest.mark.parametrize("object_state", ["missing", "corrupt", "valid"])
async def test_lfs_pointer_requires_verified_local_object(relocation, object_state):
    import hashlib

    f = relocation
    content = b"fixture LFS asset"
    oid = hashlib.sha256(content).hexdigest()
    commit(f.source, "asset.lfs", "version https://git-lfs.github.com/spec/v1\n"
           f"oid sha256:{oid}\nsize {len(content)}\n")
    obj = f.source / ".git/lfs/objects" / oid[:2] / oid[2:4] / oid
    if object_state != "missing":
        obj.parent.mkdir(parents=True)
        obj.write_bytes(content if object_state == "valid" else b"corrupt")
    plan = await preview(f)
    if object_state == "valid":
        result = await apply(f, plan)
        assert bound_path(result["binding"], obj).read_bytes() == content
    else:
        with pytest.raises(RelocationRefused, match=f"{object_state} LFS object"):
            await apply(f, plan)
        assert not f.root.exists()


async def test_repository_origin_cannot_depend_on_abandoned_storage(relocation):
    f = relocation
    await f.db.update_repo("repo", url=str(f.source))
    with pytest.raises(RelocationRefused, match="origin still depends"):
        await preview(f)


async def test_registered_session_uses_published_cache_binding(relocation):
    from src.models import SessionRecord
    from src.sessions.fake import FakeProvider
    from src.sessions.launch import launch_session
    from src.sessions.provider import SessionSpec

    f = relocation
    result = await apply(f, await preview(f))
    provider = FakeProvider()
    record = SessionRecord(id="relocated-session", project_id=f.pid, name="relocated-session",
                           provider="fake", harness="codex", work_dir=str(f.root),
                           instance_token="fixture-token", epoch="fixture-epoch",
                           profile_id="worker", lifecycle="task", started_at=1.0)
    spec = SessionSpec(session_name=record.name, work_dir=record.work_dir, command=("codex",),
                       instance_token=record.instance_token,
                       env={"AQ_SESSION_ID": record.id, "SCCACHE_DIR": "/stale",
                            "CCACHE_DIR": str(f.cache), "KEEP": "yes"})
    await launch_session(f.db, provider, spec, record)
    env = provider.starts[0].env
    assert env["CCACHE_DIR"] == result["binding"]["cache"]["dir"]
    assert "SCCACHE_DIR" not in env
    assert env["KEEP"] == "yes"


async def test_ended_session_with_retained_preparation_refuses(relocation):
    from sqlalchemy import insert

    from src.database.tables import sessions

    f = relocation
    async with f.db._engine.begin() as conn:
        await conn.execute(insert(sessions).values(
            id="retained", project_id=f.pid, profile_id="worker", harness="codex",
            provider="fake", name="retained", lifecycle="pool", work_dir=str(f.source),
            epoch="fixture", instance_token="fixture", started_at=1.0, ended_at=2.0,
            state="stopped", desired_state="stopped", claim_phase="preparing"))
    with pytest.raises(RelocationRefused, match="active writer, preparation"):
        await preview(f)


async def test_finished_job_with_retained_pin_refuses(relocation):
    from sqlalchemy import insert

    from src.database.tables import job_workspace_pins, jobs
    from tests.test_jobs_queries import values

    f = relocation
    async with f.db._engine.begin() as conn:
        await conn.execute(insert(jobs).values(**values(
            id="retained-job", project_id=f.pid, task_id=None, owner_kind="integration",
            owner_id="fixture-batch", workspace_id="base", state="succeeded", ended_at=2.0)))
        await conn.execute(insert(job_workspace_pins).values(
            job_id="retained-job", workspace_id="base", generation=0, created_at=1.0))
    with pytest.raises(RelocationRefused, match="active writer, preparation"):
        await preview(f)
