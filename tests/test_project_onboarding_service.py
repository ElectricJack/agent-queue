"""Project onboarding saga tests (design §3.1, §6, §7)."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
import subprocess

import pytest

from src.commands.contracts.project_onboarding import (
    ProjectOnboardingError,
    parse_onboard_project_request,
)
from src.commands.handler import CommandHandler
from src.config import AppConfig, ProjectRoot
from src.database import Database
from src.git.manager import GitManager
from src.models import Project, RepoSourceType, Workspace
from src.orchestrator import Orchestrator
from src.projects.onboarding import ProjectOnboardingService


def _git(path: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=check,
        capture_output=True,
        text=True,
    )


def _make_repo(path: Path, *, remote: str | None = None) -> Path:
    path.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "--initial-branch", "main", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    (path / "tracked.txt").write_text("untouched\n", encoding="utf-8")
    _git(path, "add", "tracked.txt")
    _git(
        path,
        "-c",
        "user.name=Onboarding Test",
        "-c",
        "user.email=onboarding@example.invalid",
        "commit",
        "-m",
        "seed",
    )
    if remote:
        _git(path, "remote", "add", "origin", remote)
    return path


def _tree_digest(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(path.rglob("*"), key=lambda p: p.as_posix()):
        digest.update(item.relative_to(path).as_posix().encode())
        if item.is_file():
            digest.update(item.read_bytes())
    return digest.hexdigest()


def _request(**overrides):
    values = {
        "request_id": "request-1",
        "source_mode": "link",
        "root_id": "dev",
        "relative_path": "repo",
        "project_name": "Example Project",
        "project_id": "example-project",
        "default_branch": None,
    }
    values.update(overrides)
    return parse_onboard_project_request(values)


@pytest.fixture
async def onboarding(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    data_dir = tmp_path / "data"
    config = AppConfig(
        project_roots=[ProjectRoot(id="dev", label="Development", path=str(root))],
        data_dir=str(data_dir),
        database_path=str(tmp_path / "onboarding.db"),
    )
    database = Database(config.database_path)
    await database.initialize()
    service = ProjectOnboardingService(database, config, GitManager())
    yield service, database, config, root, data_dir
    await database.close()


@pytest.mark.parametrize(
    ("remote", "expected_remote"),
    [
        (None, None),
        ("https://example.invalid/acme/repo.git", "https://example.invalid/acme/repo.git"),
        ("https://token:secret@example.invalid/acme/repo.git", "https://example.invalid/acme/repo.git"),
    ],
)
async def test_link_registers_repository_without_modifying_it(
    onboarding, remote, expected_remote
):
    service, database, _, root, data_dir = onboarding
    repo = _make_repo(root / "repo", remote=remote)
    before = _tree_digest(repo)

    result = await service.onboard_project(_request())

    assert result.project_id == "example-project"
    assert result.source_type == "link"
    assert result.canonical_path == str(repo.resolve())
    assert result.default_branch == "main"
    assert result.remote_url == expected_remote
    assert result.actions == [
        "repository_linked",
        "project_created",
        "workspace_registered",
        "vault_initialized",
    ]
    assert _tree_digest(repo) == before
    project = await database.get_project("example-project")
    workspaces = await database.list_workspaces("example-project")
    assert project is not None
    assert project.repo_url == (expected_remote or "")
    assert project.repo_default_branch == "main"
    assert len(workspaces) == 1
    assert workspaces[0].id == result.workspace_id
    assert workspaces[0].kind_id == "project-repo"
    assert workspaces[0].source_type is RepoSourceType.LINK
    assert workspaces[0].enabled is True
    assert (data_dir / "tasks" / "example-project").is_dir()
    assert (data_dir / "vault" / "projects" / "example-project" / "memory").is_dir()


@pytest.mark.parametrize("create_readme", [True, False])
async def test_init_publishes_main_repository_with_optional_readme(onboarding, create_readme):
    service, database, _, root, _ = onboarding
    request = _request(
        request_id=f"init-{create_readme}",
        source_mode="init",
        relative_path="new-repo",
        create_readme=create_readme,
        create_github=False,
    )

    result = await service.onboard_project(request)

    destination = root / "new-repo"
    assert result.source_type == "init"
    assert result.default_branch == "main"
    assert _git(destination, "symbolic-ref", "HEAD").stdout.strip() == "refs/heads/main"
    if create_readme:
        assert (destination / "README.md").read_text(encoding="utf-8") == (
            "# Example Project\n"
        )
        assert _git(destination, "log", "-1", "--format=%s").stdout.strip() == (
            "Initial commit"
        )
        assert "readme_committed" in result.actions
    else:
        assert not (destination / "README.md").exists()
        assert _git(destination, "rev-parse", "HEAD", check=False).returncode != 0
        assert "readme_committed" not in result.actions
    workspaces = await database.list_workspaces("example-project")
    assert len(workspaces) == 1
    assert workspaces[0].source_type is RepoSourceType.INIT


async def test_preflight_rejects_project_destination_workspace_and_root_conflicts(onboarding):
    service, database, _, root, tmp_data = onboarding
    repo = _make_repo(root / "repo")

    await database.create_project(Project(id="example-project", name="Existing"))
    with pytest.raises(ProjectOnboardingError, match="already exists") as project_error:
        await service.onboard_project(_request())
    assert project_error.value.code == "project_id_conflict"

    existing = root / "already-there"
    existing.mkdir()
    (existing / "keep.txt").write_text("do not touch\n", encoding="utf-8")
    existing_before = _tree_digest(existing)
    with pytest.raises(ProjectOnboardingError) as destination_error:
        await service.onboard_project(
            _request(
                request_id="destination",
                source_mode="init",
                relative_path="already-there",
                project_id="destination-project",
                create_readme=False,
                create_github=False,
            )
        )
    assert destination_error.value.code == "destination_conflict"
    assert _tree_digest(existing) == existing_before

    await database.create_project(Project(id="other", name="Other"))
    await database.create_workspace(
        Workspace(
            id="other-primary",
            project_id="other",
            workspace_path=str(repo.resolve()),
            source_type=RepoSourceType.LINK,
            kind_id="project-repo",
        )
    )
    with pytest.raises(ProjectOnboardingError) as workspace_error:
        await service.onboard_project(
            _request(request_id="workspace", project_id="workspace-project")
        )
    assert workspace_error.value.code == "destination_conflict"
    assert "other" in workspace_error.value.message

    outside = tmp_data.parent / "outside"
    _make_repo(outside)
    (root / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ProjectOnboardingError) as escape_error:
        await service.onboard_project(
            _request(
                request_id="escape",
                relative_path="escape",
                project_id="escape-project",
            )
        )
    assert escape_error.value.code == "root_escape"


async def test_terminal_replay_and_conflicting_fingerprint(onboarding):
    service, database, _, root, _ = onboarding
    _make_repo(root / "repo")
    request = _request()

    first = await service.onboard_project(request)
    replay = await ProjectOnboardingService(database, service.config, GitManager()).onboard_project(
        request
    )

    assert replay == first
    assert len(await database.list_projects()) == 1
    assert len(await database.list_workspaces()) == 1
    with pytest.raises(ProjectOnboardingError) as conflict:
        await service.onboard_project(
            _request(project_name="Different Name", project_id="different-name")
        )
    assert conflict.value.code == "request_conflict"


class _PausingGitManager(GitManager):
    def __init__(self) -> None:
        super().__init__()
        self.init_started = asyncio.Event()
        self.continue_init = asyncio.Event()

    async def _arun(self, args, cwd=None, timeout=None):
        if args and args[0] == "init":
            self.init_started.set()
            await self.continue_init.wait()
        return await super()._arun(args, cwd=cwd, timeout=timeout)


async def test_concurrent_requests_for_same_destination_return_destination_locked(onboarding):
    _, database, config, root, _ = onboarding
    git = _PausingGitManager()
    first_service = ProjectOnboardingService(database, config, git)
    second_service = ProjectOnboardingService(database, config, GitManager())
    first_request = _request(
        request_id="concurrent-1",
        source_mode="init",
        relative_path="contended",
        project_id="concurrent-one",
        create_readme=False,
        create_github=False,
    )
    first = asyncio.create_task(first_service.onboard_project(first_request))
    await git.init_started.wait()

    with pytest.raises(ProjectOnboardingError) as locked:
        await second_service.onboard_project(
            _request(
                request_id="concurrent-2",
                source_mode="init",
                relative_path="contended",
                project_id="concurrent-two",
                create_readme=False,
                create_github=False,
            )
        )
    assert locked.value.code == "destination_locked"
    git.continue_init.set()
    assert (await first).project_id == "concurrent-one"
    assert (root / "contended").is_dir()


async def test_concurrent_replay_reports_phase_without_failing_active_request(onboarding):
    _, database, config, root, _ = onboarding
    git = _PausingGitManager()
    service = ProjectOnboardingService(database, config, git)
    request = _request(
        request_id="active-replay",
        source_mode="init",
        relative_path="active-replay",
        project_id="active-replay",
        create_readme=False,
        create_github=False,
    )
    active = asyncio.create_task(service.onboard_project(request))
    await git.init_started.wait()

    replay = await ProjectOnboardingService(database, config, GitManager()).onboard_project(
        request
    )

    assert replay.status == "running"
    assert replay.phase == "prepare"
    git.continue_init.set()
    completed = await active
    assert completed.project_id == "active-replay"
    record = await database.get_onboarding_request("active-replay")
    assert record is not None and record["status"] == "succeeded"
    assert (root / "active-replay").is_dir()


class _SimulatedCrash(BaseException):
    pass


async def test_reconstructed_service_resumes_request_owned_staging(onboarding, monkeypatch):
    service, database, config, root, _ = onboarding
    request = _request(
        request_id="recover-me",
        source_mode="init",
        relative_path="recovered",
        project_id="recovered",
        create_readme=False,
        create_github=False,
    )

    async def crash_before_publish(*_args, **_kwargs):
        raise _SimulatedCrash

    monkeypatch.setattr(service, "_publish_staging", crash_before_publish)
    with pytest.raises(_SimulatedCrash):
        await service.onboard_project(request)
    record = await database.get_onboarding_request("recover-me")
    assert record is not None and record["status"] == "pending"
    staging_paths = [
        Path(resource["path"])
        for resource in record["created_resources"]
        if resource["kind"] == "staging_directory"
    ]
    assert len(staging_paths) == 1 and staging_paths[0].is_dir()

    recovered = await ProjectOnboardingService(database, config, GitManager()).onboard_project(
        request
    )

    assert recovered.project_id == "recovered"
    assert (root / "recovered" / ".git").exists()
    assert not staging_paths[0].exists()


@pytest.mark.parametrize("source_mode", ["link", "init"])
async def test_registration_failure_rolls_back_only_request_owned_resources(
    onboarding, monkeypatch, source_mode
):
    service, database, _, root, _ = onboarding
    repo = _make_repo(root / "repo")
    before = _tree_digest(repo)

    async def fail_registration(*_args, **_kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(database, "register_onboarded_project", fail_registration)
    if source_mode == "link":
        request = _request(request_id="rollback-link")
        destination = repo
    else:
        request = _request(
            request_id="rollback-init",
            source_mode="init",
            relative_path="rollback-init",
            project_id="rollback-init",
            create_readme=True,
            create_github=False,
        )
        destination = root / "rollback-init"

    with pytest.raises(ProjectOnboardingError) as failure:
        await service.onboard_project(request)

    assert failure.value.code == "registration_failed"
    assert await database.get_project(request.project_id) is None
    assert await database.list_workspaces(request.project_id) == []
    if source_mode == "link":
        assert destination.is_dir()
        assert _tree_digest(repo) == before
    else:
        assert not destination.exists()
        assert not list(root.glob(".*aq-onboard*"))


async def test_status_maps_durable_terminal_record(onboarding):
    service, _, _, root, _ = onboarding
    _make_repo(root / "repo")
    result = await service.onboard_project(_request())

    status = await service.get_project_onboarding("request-1")

    assert status.request_id == "request-1"
    assert status.status == "completed"
    assert status.phase == "done"
    assert status.result == result


async def test_command_handler_delegates_onboard_and_status_to_service(onboarding):
    _, database, config, root, _ = onboarding
    _make_repo(root / "repo")
    orchestrator = Orchestrator(config)
    orchestrator.db = database
    handler = CommandHandler(orchestrator, config)
    payload = _request().model_dump(mode="json")

    result = await handler.execute("onboard_project", payload)
    status = await handler.execute(
        "get_project_onboarding", {"request_id": payload["request_id"]}
    )

    assert result["success"] is True
    assert result["project_id"] == "example-project"
    assert status["success"] is True
    assert status["status"] == "completed"
    assert status["result"]["workspace_id"] == result["workspace_id"]


@pytest.mark.parametrize("mode", ["github_clone", "init_github"])
async def test_github_modes_are_not_implemented_without_mutation(onboarding, mode):
    service, database, _, root, _ = onboarding
    if mode == "github_clone":
        request = _request(
            source_mode="github_clone",
            github_url="https://github.com/acme/repo",
        )
    else:
        request = _request(
            source_mode="init",
            create_readme=True,
            create_github=True,
            github_owner="acme",
        )

    with pytest.raises(ProjectOnboardingError) as failure:
        await service.onboard_project(request)

    assert failure.value.code == "not_implemented"
    assert await database.list_projects() == []
    assert await database.get_onboarding_request(request.request_id) is None
    assert list(root.iterdir()) == []
