"""Surface-independent project onboarding saga.

The service is the single boundary that may turn an onboarding request into
Git/filesystem changes plus AQ project/workspace records.  HTTP, MCP and CLI
surfaces validate their wire contracts and delegate here.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
from typing import Any, AsyncIterator
from urllib.parse import urlsplit, urlunsplit

from src.commands.contracts.project_onboarding import (
    GetProjectOnboardingResult,
    OnboardProjectResult,
    OnboardingErrorInfo,
    ProjectOnboardingError,
    ProjectOnboardingErrorCode,
)
from src.config import AppConfig, resolve_project_root
from src.database.base import DatabaseBackend
from src.git.manager import GitError, GitManager, _validate_ref
from src.models import Project, RepoSourceType, Workspace
from src.profiles.default_selection import select_default_profile_id
from src.projects.github import (
    GhClient,
    GitHubError,
    GitHubErrorCode,
    GitHubRepo,
    parse_github_repository,
    scrub_secrets,
)
from src.projects.paths import (
    ProjectPathError,
    is_git_worktree_root,
    validate_relative_path,
)
from src.projects.storage import ensure_project_storage

_OWNER_FILE = ".aq-onboarding-owner"
_LOCKS: dict[str, asyncio.Lock] = {}


def _lock_for(key: str) -> asyncio.Lock:
    # The daemon runs one event loop.  There is no await between lookup and
    # insertion, so setdefault is sufficient and keeps locks shared across
    # reconstructed service instances.
    return _LOCKS.setdefault(key, asyncio.Lock())


@asynccontextmanager
async def _try_locks(keys: list[str]) -> AsyncIterator[None]:
    acquired: list[asyncio.Lock] = []
    try:
        for key in sorted(set(keys)):
            lock = _lock_for(key)
            if lock.locked():
                raise ProjectOnboardingError(
                    ProjectOnboardingErrorCode.DESTINATION_LOCKED,
                    "Another onboarding request is using this project id or destination",
                    phase="preflight",
                )
            await lock.acquire()
            acquired.append(lock)
        yield
    finally:
        for lock in reversed(acquired):
            lock.release()


def _fingerprint(
    request: Any,
    github_clone: tuple[GitHubRepo, str] | None = None,
) -> str:
    normalized = request.model_dump(mode="json", exclude_none=False)
    if github_clone is not None:
        identity, clone_url = github_clone
        normalized["github_repository"] = {
            "owner": identity.owner,
            "name": identity.name,
        }
        normalized["github_url"] = clone_url
    encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _timestamp(value: float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()


def _safe_remote_url(value: str | None) -> str | None:
    """Strip URL userinfo so detected credentials are never persisted."""
    if not value:
        return None
    parsed = urlsplit(value)
    if not parsed.scheme or "@" not in parsed.netloc:
        return value
    return urlunsplit(
        (parsed.scheme, parsed.netloc.rsplit("@", 1)[1], parsed.path, parsed.query, parsed.fragment)
    )


class ProjectOnboardingService:
    """Validate, prepare, publish, register and compensate onboarding requests."""

    def __init__(
        self,
        db: DatabaseBackend,
        config: AppConfig,
        git_manager: GitManager | None = None,
        *,
        gh_client: GhClient | None = None,
    ) -> None:
        self.db = db
        self.config = config
        self.git = git_manager or GitManager()
        self.gh = gh_client or GhClient()

    async def onboard_project(self, request: Any) -> OnboardProjectResult | GetProjectOnboardingResult:
        """Run or replay one project onboarding request."""
        if request.default_branch is not None:
            try:
                _validate_ref(request.default_branch, field="default branch")
            except GitError:
                raise ProjectOnboardingError(
                    ProjectOnboardingErrorCode.INVALID_REQUEST,
                    "The default branch name is not valid",
                    phase="validate",
                    field_errors=[
                        {
                            "field": "default_branch",
                            "message": "invalid Git default branch name",
                        }
                    ],
                ) from None
        github_clone: tuple[GitHubRepo, str] | None = None
        if request.source_mode == "github_clone":
            try:
                github_clone = self._github_clone_source(request)
            except GitHubError as exc:
                raise self._map_github_error(exc, phase="preflight") from exc

        fingerprint = _fingerprint(request, github_clone)
        already_exists, stored_fingerprint = await self.db.create_onboarding_request(
            request.request_id, fingerprint, phase="validate"
        )
        if stored_fingerprint != fingerprint:
            raise ProjectOnboardingError(
                ProjectOnboardingErrorCode.REQUEST_CONFLICT,
                f"Request id '{request.request_id}' was already used with different inputs",
                phase="validate",
            )

        record = await self.db.get_onboarding_request(request.request_id)
        if record is None:  # pragma: no cover - store contract is tested independently.
            raise RuntimeError("onboarding request disappeared after creation")
        if already_exists and record["status"] == "succeeded":
            return OnboardProjectResult.model_validate(record["result"])
        if already_exists and record["status"] == "failed":
            raise self._stored_error(record)

        request_lock = _lock_for(f"request:{request.request_id}")
        if already_exists and request_lock.locked():
            return await self.get_project_onboarding(request.request_id)

        registered = False
        workspace_id = f"{request.project_id}-primary"
        destination: Path | None = None
        try:
            root, destination = self._resolve_destination(request)
            keys = [
                f"request:{request.request_id}",
                f"project:{request.project_id}",
                f"destination:{os.path.normcase(str(destination))}",
            ]
            async with _try_locks(keys):
                # Another service could have completed between the first read
                # and lock acquisition.
                record = await self.db.get_onboarding_request(request.request_id)
                if record and record["status"] == "succeeded":
                    return OnboardProjectResult.model_validate(record["result"])
                if record and record["status"] == "failed":
                    raise self._stored_error(record)

                await self.db.update_onboarding_phase(request.request_id, "preflight")
                await self._preflight(request, root, destination, record or {})

                actions: list[str]
                remote_url: str | None = None
                published = destination
                prepared = destination
                staging_container: Path | None = None
                if request.source_mode == "link":
                    remote_url = _safe_remote_url(
                        await self.git.aget_remote_url(str(destination))
                    )
                    default_branch = request.default_branch or await self.git.aget_default_branch(
                        str(destination)
                    )
                    actions = ["repository_linked"]
                elif request.source_mode == "init":
                    default_branch = request.default_branch or "main"
                    if self._owner_matches(destination, request.request_id):
                        actions = self._actions_for_existing_init(destination, request.create_readme)
                    else:
                        prepared, actions = await self._prepare_init(
                            request, root, destination, default_branch, record or {}
                        )
                    if request.create_github:
                        remote_url, github_actions = await self._create_github_remote(
                            request,
                            prepared,
                            destination,
                            default_branch,
                            record or {},
                        )
                        actions.extend(github_actions)
                else:
                    if github_clone is None:  # pragma: no cover - established above.
                        raise RuntimeError("normalized GitHub clone source is missing")
                    _, clone_url = github_clone
                    if self._owner_matches(
                        destination,
                        request.request_id,
                        git_metadata=True,
                    ):
                        prepared = destination
                        actions = ["repository_cloned"]
                    else:
                        prepared, staging_container, actions = await self._prepare_clone(
                            request,
                            root,
                            destination,
                            clone_url,
                            record or {},
                        )
                    remote_url = _safe_remote_url(
                        await self.git.aget_remote_url(str(prepared))
                    ) or clone_url
                    default_branch = request.default_branch or await self.git.aget_default_branch(
                        str(prepared)
                    )

                if request.source_mode != "link" and prepared != destination:
                    await self.db.update_onboarding_phase(request.request_id, "publish")
                    published = await self._publish_staging(request, prepared, destination)
                if request.source_mode != "link":
                    if self._recorded_path(record or {}, "final_directory") is None:
                        recorded = await self.db.append_onboarding_resource(
                            request.request_id,
                            {
                                "kind": "final_directory",
                                "path": str(published),
                                "owner_marker": (
                                    "git"
                                    if request.source_mode == "github_clone"
                                    else "root"
                                ),
                            },
                        )
                        if not recorded:
                            raise ProjectOnboardingError(
                                ProjectOnboardingErrorCode.REGISTRATION_FAILED,
                                "Could not record the published project directory",
                                phase="publish",
                            )
                    if request.source_mode == "github_clone" and staging_container is None:
                        staging_container = self._recorded_path(
                            record or {}, "staging_directory"
                        )
                    if staging_container is not None:
                        self._remove_owned_staging_container(
                            staging_container, request.request_id
                        )

                await self.db.update_onboarding_phase(request.request_id, "register")
                default_profile_id = select_default_profile_id(
                    profile.id for profile in await self.db.list_profiles()
                )
                project = Project(
                    id=request.project_id,
                    name=request.project_name,
                    repo_url=remote_url or "",
                    repo_default_branch=default_branch,
                    default_profile_id=default_profile_id,
                )
                source_type = {
                    "link": RepoSourceType.LINK,
                    "init": RepoSourceType.INIT,
                    "github_clone": RepoSourceType.CLONE,
                }[request.source_mode]
                workspace = Workspace(
                    id=workspace_id,
                    project_id=request.project_id,
                    workspace_path=str(published),
                    source_type=source_type,
                    name="primary",
                    kind_id="project-repo",
                    enabled=True,
                )
                try:
                    await self.db.register_onboarded_project(project, workspace)
                except Exception as exc:
                    raise ProjectOnboardingError(
                        ProjectOnboardingErrorCode.REGISTRATION_FAILED,
                        f"Could not register project '{request.project_id}'",
                        phase="register",
                    ) from exc
                registered = True
                await self.db.append_onboarding_resource(
                    request.request_id,
                    {"kind": "project", "id": request.project_id},
                )
                await self.db.append_onboarding_resource(
                    request.request_id,
                    {"kind": "workspace", "id": workspace_id},
                )

                storage_paths = self._new_storage_paths(request.project_id)
                for path in storage_paths:
                    await self.db.append_onboarding_resource(
                        request.request_id,
                        {"kind": "project_storage", "path": str(path)},
                    )
                try:
                    ensure_project_storage(self.config.data_dir, request.project_id)
                except Exception as exc:
                    raise ProjectOnboardingError(
                        ProjectOnboardingErrorCode.REGISTRATION_FAILED,
                        f"Could not initialize vault storage for '{request.project_id}'",
                        phase="register",
                    ) from exc

                result = OnboardProjectResult(
                    request_id=request.request_id,
                    project_id=request.project_id,
                    workspace_id=workspace_id,
                    source_type=source_type.value,
                    root_id=request.root_id,
                    relative_path=request.relative_path,
                    canonical_path=str(published),
                    default_branch=default_branch,
                    remote_url=remote_url,
                    actions=[
                        *actions,
                        "project_created",
                        "workspace_registered",
                        "vault_initialized",
                    ],
                )
                await self.db.update_onboarding_phase(request.request_id, "done")
                await self.db.finish_onboarding_request(
                    request.request_id,
                    "succeeded",
                    result=result.model_dump(mode="json"),
                )
                if request.source_mode != "link":
                    self._remove_owner_marker(
                        published,
                        git_metadata=request.source_mode == "github_clone",
                    )
                return result
        except ProjectOnboardingError as exc:
            if exc.code != ProjectOnboardingErrorCode.REQUEST_CONFLICT.value:
                exc = await self._with_retained_github_recovery(request.request_id, exc)
                await self._compensate(
                    request.request_id,
                    request.project_id,
                    workspace_id,
                    registered=registered,
                    owned_destination=(
                        destination if request.source_mode != "link" else None
                    ),
                    destination_git_metadata=request.source_mode == "github_clone",
                )
                await self.db.finish_onboarding_request(
                    request.request_id,
                    "failed",
                    error=exc.to_dict(),
                )
            raise exc
        except Exception as exc:
            error = ProjectOnboardingError(
                ProjectOnboardingErrorCode.REGISTRATION_FAILED,
                "Project onboarding failed unexpectedly",
                phase="register" if registered else "prepare",
            )
            error = await self._with_retained_github_recovery(request.request_id, error)
            await self._compensate(
                request.request_id,
                request.project_id,
                workspace_id,
                registered=registered,
                owned_destination=(
                    destination
                    if destination is not None and request.source_mode != "link"
                    else None
                ),
                destination_git_metadata=request.source_mode == "github_clone",
            )
            await self.db.finish_onboarding_request(
                request.request_id,
                "failed",
                error=error.to_dict(),
            )
            raise error from exc

    async def get_project_onboarding(self, request_id: str) -> GetProjectOnboardingResult:
        """Return the public status projection of a durable request record."""
        record = await self.db.get_onboarding_request(request_id)
        if record is None:
            raise ProjectOnboardingError(
                ProjectOnboardingErrorCode.NOT_FOUND,
                f"Onboarding request '{request_id}' was not found",
            )
        stored_status = record["status"]
        status = {
            "pending": "running",
            "succeeded": "completed",
            "failed": "failed",
        }[stored_status]
        result = (
            OnboardProjectResult.model_validate(record["result"])
            if record.get("result")
            else None
        )
        error_payload = record.get("error") or None
        error = None
        if error_payload:
            error = OnboardingErrorInfo.model_validate(
                {
                    key: value
                    for key, value in error_payload.items()
                    if key in {"error_code", "error", "phase", "details", "field_errors"}
                }
            )
        return GetProjectOnboardingResult(
            request_id=request_id,
            status=status,
            phase=record.get("phase"),
            result=result,
            error=error,
            created_at=_timestamp(record.get("created_at")),
            updated_at=_timestamp(record.get("updated_at")),
        )

    def _resolve_destination(self, request: Any) -> tuple[Any, Path]:
        root = resolve_project_root(self.config, request.root_id)
        if root is None or not root.readable:
            raise ProjectOnboardingError(
                ProjectOnboardingErrorCode.ROOT_UNAVAILABLE,
                f"Project root '{request.root_id}' is unavailable",
                phase="preflight",
            )
        if request.source_mode != "link" and not root.writable:
            raise ProjectOnboardingError(
                ProjectOnboardingErrorCode.ROOT_UNAVAILABLE,
                f"Project root '{request.root_id}' is not writable",
                phase="preflight",
            )
        try:
            resolved = validate_relative_path(
                Path(root.path),
                request.relative_path,
                require_directory=request.source_mode == "link",
            )
        except ProjectPathError as exc:
            raise ProjectOnboardingError(
                exc.code.value,
                exc.message,
                phase="preflight",
            ) from exc
        return root, resolved.real_path

    async def _preflight(
        self, request: Any, root: Any, destination: Path, record: dict[str, Any]
    ) -> None:
        if await self.db.get_project(request.project_id) is not None:
            raise ProjectOnboardingError(
                ProjectOnboardingErrorCode.PROJECT_ID_CONFLICT,
                f"Project id '{request.project_id}' already exists",
                phase="preflight",
            )

        canonical = os.path.normcase(str(destination.resolve()))
        for workspace in await self.db.list_workspaces():
            registered = os.path.normcase(str(Path(workspace.workspace_path).resolve()))
            if registered == canonical:
                raise ProjectOnboardingError(
                    ProjectOnboardingErrorCode.DESTINATION_CONFLICT,
                    f"Destination is already registered to project '{workspace.project_id}'",
                    phase="preflight",
                )

        if request.source_mode == "link":
            if not is_git_worktree_root(destination):
                raise ProjectOnboardingError(
                    ProjectOnboardingErrorCode.INVALID_GIT_REPOSITORY,
                    "The selected directory is not a Git worktree root",
                    phase="preflight",
                )
            return

        if destination.exists() and not self._owner_matches(
            destination,
            request.request_id,
            git_metadata=request.source_mode == "github_clone",
        ):
            raise ProjectOnboardingError(
                ProjectOnboardingErrorCode.DESTINATION_CONFLICT,
                "The destination already exists",
                phase="preflight",
            )

    async def _prepare_init(
        self,
        request: Any,
        root: Any,
        destination: Path,
        default_branch: str,
        record: dict[str, Any],
    ) -> tuple[Path, list[str]]:
        await self.db.update_onboarding_phase(request.request_id, "prepare")
        staging = self._staging_path(destination, request.request_id)
        recorded_staging = self._recorded_path(record, "staging_directory")
        if recorded_staging is not None and recorded_staging != staging:
            raise ProjectOnboardingError(
                ProjectOnboardingErrorCode.DESTINATION_LOCKED,
                "The durable request ledger names a different staging directory",
                phase="prepare",
            )

        for candidate in destination.parent.glob(f".{destination.name}.aq-onboard-*"):
            if candidate != staging:
                raise ProjectOnboardingError(
                    ProjectOnboardingErrorCode.DESTINATION_LOCKED,
                    "Another request owns a staging directory for this destination",
                    phase="prepare",
                )

        if staging.exists():
            if not self._owner_matches(staging, request.request_id):
                raise ProjectOnboardingError(
                    ProjectOnboardingErrorCode.DESTINATION_LOCKED,
                    "The staging directory is not owned by this request",
                    phase="prepare",
                )
            if not is_git_worktree_root(staging):
                shutil.rmtree(staging)
        if not staging.exists():
            # Mutation-time validation catches a root symlink swap and a
            # destination created after the earlier preflight.
            resolved = validate_relative_path(Path(root.path), request.relative_path)
            if resolved.exists:
                raise ProjectOnboardingError(
                    ProjectOnboardingErrorCode.DESTINATION_CONFLICT,
                    "The destination appeared during onboarding",
                    phase="prepare",
                )
            staging.mkdir(mode=0o700)
            self._write_owner_marker(staging, request.request_id)
            await self.db.append_onboarding_resource(
                request.request_id,
                {"kind": "staging_directory", "path": str(staging)},
            )

        try:
            if not is_git_worktree_root(staging):
                await self.git._arun(
                    ["init", "--initial-branch", default_branch], cwd=str(staging)
                )
        except (GitError, OSError) as exc:
            raise ProjectOnboardingError(
                ProjectOnboardingErrorCode.INIT_FAILED,
                "Could not initialize the Git repository",
                phase="prepare",
                details={"subprocess_error": scrub_secrets(str(exc))},
            ) from None

        actions = ["repository_initialized"]
        if request.create_readme:
            readme = staging / "README.md"
            expected = f"# {request.project_name}\n"
            if readme.exists():
                if readme.read_text(encoding="utf-8") != expected:
                    raise ProjectOnboardingError(
                        ProjectOnboardingErrorCode.DESTINATION_LOCKED,
                        "The staged README is not owned by this request",
                        phase="prepare",
                    )
            else:
                with readme.open("x", encoding="utf-8") as handle:
                    handle.write(expected)
                await self.db.append_onboarding_resource(
                    request.request_id,
                    {"kind": "readme", "path": str(readme)},
                )
            if await self.git.arev_parse(str(staging), "HEAD") is None:
                try:
                    await self.git._arun(["add", "--", "README.md"], cwd=str(staging))
                    await self.git._arun(
                        [
                            "-c",
                            "user.name=Agent Queue",
                            "-c",
                            "user.email=agent-queue@localhost",
                            "commit",
                            "-m",
                            "Initial commit",
                        ],
                        cwd=str(staging),
                    )
                except GitError as exc:
                    raise ProjectOnboardingError(
                        ProjectOnboardingErrorCode.COMMIT_FAILED,
                        "Could not create the initial README commit",
                        phase="prepare",
                        details={"subprocess_error": scrub_secrets(str(exc))},
                    ) from None
            actions.append("readme_committed")
        return staging, actions

    async def _prepare_clone(
        self,
        request: Any,
        root: Any,
        destination: Path,
        clone_url: str,
        record: dict[str, Any],
    ) -> tuple[Path, Path, list[str]]:
        """Clone into an owned hidden container and return its checkout."""
        await self.db.update_onboarding_phase(request.request_id, "prepare")
        staging = self._staging_path(destination, request.request_id)
        recorded_staging = self._recorded_path(record, "staging_directory")
        if recorded_staging is not None and recorded_staging != staging:
            raise ProjectOnboardingError(
                ProjectOnboardingErrorCode.DESTINATION_LOCKED,
                "The durable request ledger names a different staging directory",
                phase="prepare",
            )
        for candidate in destination.parent.glob(f".{destination.name}.aq-onboard-*"):
            if candidate != staging:
                raise ProjectOnboardingError(
                    ProjectOnboardingErrorCode.DESTINATION_LOCKED,
                    "Another request owns a staging directory for this destination",
                    phase="prepare",
                )

        if staging.exists() and not self._owner_matches(staging, request.request_id):
            raise ProjectOnboardingError(
                ProjectOnboardingErrorCode.DESTINATION_LOCKED,
                "The staging directory is not owned by this request",
                phase="prepare",
            )
        if not staging.exists():
            resolved = validate_relative_path(Path(root.path), request.relative_path)
            if resolved.exists:
                raise ProjectOnboardingError(
                    ProjectOnboardingErrorCode.DESTINATION_CONFLICT,
                    "The destination appeared during onboarding",
                    phase="prepare",
                )
            staging.mkdir(mode=0o700)
            self._write_owner_marker(staging, request.request_id)
            await self.db.append_onboarding_resource(
                request.request_id,
                {"kind": "staging_directory", "path": str(staging)},
            )

        checkout = staging / "repository"
        if checkout.exists() and not await self.git.avalidate_checkout(str(checkout)):
            shutil.rmtree(checkout)
        if not checkout.exists():
            try:
                await self.git.acreate_checkout(clone_url, str(checkout))
                await self.git._arun(
                    ["remote", "set-url", "origin", clone_url],
                    cwd=str(checkout),
                )
            except (GitError, OSError) as exc:
                raise ProjectOnboardingError(
                    ProjectOnboardingErrorCode.CLONE_FAILED,
                    "Could not clone the GitHub repository",
                    phase="prepare",
                    details={"subprocess_error": scrub_secrets(str(exc))},
                ) from None
        self._write_owner_marker(checkout, request.request_id, git_metadata=True)
        return checkout, staging, ["repository_cloned"]

    async def _create_github_remote(
        self,
        request: Any,
        repository: Path,
        destination: Path,
        default_branch: str,
        record: dict[str, Any],
    ) -> tuple[str, list[str]]:
        """Create or resume an external repository, configure origin and push if possible."""
        await self.db.update_onboarding_phase(request.request_id, "github")
        repository_name = request.github_repo or destination.name
        try:
            requested_identity = parse_github_repository(
                f"{request.github_owner}/{repository_name}"
            )
        except GitHubError as exc:
            raise self._map_github_error(exc, phase="github") from exc

        retained_url = self._recorded_url(record, "github_repository")
        intent_url = self._recorded_url(record, "github_repository_intent")
        if intent_url and intent_url != requested_identity.html_url:
            raise ProjectOnboardingError(
                ProjectOnboardingErrorCode.DESTINATION_LOCKED,
                "The durable request ledger names a different GitHub repository",
                phase="github",
            )
        if retained_url:
            try:
                identity = parse_github_repository(retained_url)
            except GitHubError as exc:  # pragma: no cover - ledger writes are canonical.
                raise self._map_github_error(exc, phase="github") from exc
        else:
            if intent_url is None:
                intent_recorded = await self.db.append_onboarding_resource(
                    request.request_id,
                    {
                        "kind": "github_repository_intent",
                        "url": requested_identity.html_url,
                    },
                )
                if not intent_recorded:
                    raise ProjectOnboardingError(
                        ProjectOnboardingErrorCode.REGISTRATION_FAILED,
                        "Could not record the pending GitHub repository creation",
                        phase="github",
                    )
            try:
                identity = await self.gh.create_repository(
                    request.github_owner,
                    repository_name,
                    request.github_visibility,
                )
            except GitHubError as exc:
                error = self._map_github_error(exc, phase="github")
                if (
                    intent_url is not None
                    and error.code
                    == ProjectOnboardingErrorCode.GITHUB_REPOSITORY_CONFLICT.value
                ):
                    error = ProjectOnboardingError(
                        error.code,
                        error.message,
                        phase=error.phase,
                        details={
                            **error.details,
                            **self._retained_github_details(intent_url),
                        },
                        field_errors=error.field_errors,
                    )
                raise error from exc
            retained_url = identity.html_url
            try:
                created_recorded = await self.db.append_onboarding_resource(
                    request.request_id,
                    {"kind": "github_repository", "url": retained_url},
                )
            except Exception:
                raise ProjectOnboardingError(
                    ProjectOnboardingErrorCode.REGISTRATION_FAILED,
                    "The GitHub repository was created but could not be recorded",
                    phase="github",
                    details=self._retained_github_details(retained_url),
                ) from None
            if not created_recorded:
                raise ProjectOnboardingError(
                    ProjectOnboardingErrorCode.REGISTRATION_FAILED,
                    "The GitHub repository was created but could not be recorded",
                    phase="github",
                    details=self._retained_github_details(retained_url),
                )

        existing_origin = _safe_remote_url(await self.git.aget_remote_url(str(repository)))
        if existing_origin and existing_origin != identity.clone_https:
            raise ProjectOnboardingError(
                ProjectOnboardingErrorCode.GITHUB_REPOSITORY_CONFLICT,
                "The prepared repository already has a different origin remote",
                phase="github",
            )
        actions = ["github_repository_created"]
        if not existing_origin:
            try:
                await self.git._arun(
                    ["remote", "add", "origin", identity.clone_https],
                    cwd=str(repository),
                )
            except (GitError, OSError) as exc:
                raise ProjectOnboardingError(
                    ProjectOnboardingErrorCode.PUSH_FAILED,
                    "Could not configure the GitHub origin remote",
                    phase="github",
                    details={"subprocess_error": scrub_secrets(str(exc))},
                ) from None
        actions.append("remote_configured")

        if await self.git.arev_parse(str(repository), "HEAD") is not None:
            try:
                await self.git._arun(
                    ["push", "-u", "origin", "--", default_branch],
                    cwd=str(repository),
                )
            except (GitError, OSError) as exc:
                raise ProjectOnboardingError(
                    ProjectOnboardingErrorCode.PUSH_FAILED,
                    f"Could not push branch '{default_branch}' to GitHub",
                    phase="github",
                    details={"subprocess_error": scrub_secrets(str(exc))},
                ) from None
            actions.append("branch_pushed")
        return identity.clone_https, actions

    async def _publish_staging(self, request: Any, staging: Path, destination: Path) -> Path:
        resolved = validate_relative_path(
            Path(resolve_project_root(self.config, request.root_id).path),
            request.relative_path,
        )
        if resolved.exists:
            raise ProjectOnboardingError(
                ProjectOnboardingErrorCode.DESTINATION_CONFLICT,
                "The destination appeared before publish",
                phase="publish",
            )
        try:
            staging.rename(destination)
        except FileExistsError as exc:
            raise ProjectOnboardingError(
                ProjectOnboardingErrorCode.DESTINATION_CONFLICT,
                "The destination appeared before publish",
                phase="publish",
            ) from exc
        return destination.resolve()

    async def _compensate(
        self,
        request_id: str,
        project_id: str,
        workspace_id: str,
        *,
        registered: bool,
        owned_destination: Path | None = None,
        destination_git_metadata: bool = False,
    ) -> None:
        if registered:
            await self.db.rollback_onboarded_project(project_id, workspace_id)
        record = await self.db.get_onboarding_request(request_id)
        resources = (record or {}).get("created_resources") or []
        for resource in reversed(resources):
            if resource.get("kind") not in {
                "staging_directory",
                "final_directory",
                "project_storage",
            }:
                continue
            raw_path = resource.get("path")
            if not isinstance(raw_path, str):
                continue
            path = Path(raw_path)
            if resource.get("kind") == "project_storage":
                if path.exists():
                    shutil.rmtree(path)
            elif path.exists() and self._owner_matches(
                path,
                request_id,
                git_metadata=resource.get("owner_marker") == "git",
            ):
                shutil.rmtree(path)
        if owned_destination is not None and owned_destination.exists() and self._owner_matches(
            owned_destination,
            request_id,
            git_metadata=destination_git_metadata,
        ):
            shutil.rmtree(owned_destination)

    def _new_storage_paths(self, project_id: str) -> list[Path]:
        candidates = [
            Path(self.config.data_dir, "tasks", project_id),
            Path(self.config.data_dir, "vault", "projects", project_id),
        ]
        return [path for path in candidates if not path.exists()]

    @staticmethod
    def _staging_path(destination: Path, request_id: str) -> Path:
        safe_id = re.sub(r"[^A-Za-z0-9._-]+", "-", request_id).strip("-._") or "request"
        digest = hashlib.sha256(request_id.encode()).hexdigest()[:10]
        return destination.parent / f".{destination.name}.aq-onboard-{safe_id[:40]}-{digest}"

    @staticmethod
    def _owner_marker_path(path: Path, *, git_metadata: bool) -> Path:
        return (path / ".git" / _OWNER_FILE) if git_metadata else (path / _OWNER_FILE)

    @classmethod
    def _owner_matches(
        cls,
        path: Path,
        request_id: str,
        *,
        git_metadata: bool = False,
    ) -> bool:
        marker = cls._owner_marker_path(path, git_metadata=git_metadata)
        try:
            if not stat.S_ISREG(marker.lstat().st_mode):
                return False
            return marker.read_text(encoding="utf-8") == request_id
        except (OSError, UnicodeError):
            return False

    @classmethod
    def _write_owner_marker(
        cls,
        path: Path,
        request_id: str,
        *,
        git_metadata: bool = False,
    ) -> None:
        marker = cls._owner_marker_path(path, git_metadata=git_metadata)
        if cls._owner_matches(path, request_id, git_metadata=git_metadata):
            return
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(marker, flags, 0o600)
        except FileExistsError as exc:
            raise ProjectOnboardingError(
                ProjectOnboardingErrorCode.DESTINATION_LOCKED,
                "The onboarding ownership marker is not owned by this request",
                phase="prepare",
            ) from exc
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(request_id)

    @classmethod
    def _remove_owner_marker(
        cls,
        path: Path,
        *,
        git_metadata: bool = False,
    ) -> None:
        marker = cls._owner_marker_path(path, git_metadata=git_metadata)
        try:
            marker.unlink()
        except FileNotFoundError:
            pass

    @classmethod
    def _remove_owned_staging_container(cls, path: Path, request_id: str) -> None:
        if path.exists() and cls._owner_matches(path, request_id):
            shutil.rmtree(path)

    @staticmethod
    def _recorded_path(record: dict[str, Any], kind: str) -> Path | None:
        for resource in record.get("created_resources") or []:
            if resource.get("kind") == kind and isinstance(resource.get("path"), str):
                return Path(resource["path"])
        return None

    @staticmethod
    def _recorded_url(record: dict[str, Any], kind: str) -> str | None:
        for resource in record.get("created_resources") or []:
            if resource.get("kind") == kind and isinstance(resource.get("url"), str):
                return resource["url"]
        return None

    @staticmethod
    def _github_clone_source(request: Any) -> tuple[GitHubRepo, str]:
        if request.github_repository is not None:
            identity = parse_github_repository(
                f"{request.github_repository.owner}/{request.github_repository.name}"
            )
            return identity, identity.clone_https
        raw = request.github_url
        identity = parse_github_repository(raw)
        lowered = raw.strip().lower()
        clone_url = (
            identity.clone_ssh
            if lowered.startswith(("git@", "ssh://"))
            else identity.clone_https
        )
        return identity, clone_url

    @staticmethod
    def _map_github_error(exc: GitHubError, *, phase: str) -> ProjectOnboardingError:
        code = {
            GitHubErrorCode.CLI_MISSING: ProjectOnboardingErrorCode.GITHUB_CLI_MISSING,
            GitHubErrorCode.AUTH_REQUIRED: ProjectOnboardingErrorCode.GITHUB_AUTH_REQUIRED,
            GitHubErrorCode.REPOSITORY_INACCESSIBLE: (
                ProjectOnboardingErrorCode.GITHUB_REPOSITORY_INACCESSIBLE
            ),
            GitHubErrorCode.REPOSITORY_CONFLICT: (
                ProjectOnboardingErrorCode.GITHUB_REPOSITORY_CONFLICT
            ),
            GitHubErrorCode.CLI_FAILED: (
                ProjectOnboardingErrorCode.GITHUB_REPOSITORY_INACCESSIBLE
            ),
            GitHubErrorCode.INVALID_INPUT: (
                ProjectOnboardingErrorCode.GITHUB_REPOSITORY_INACCESSIBLE
            ),
        }[exc.code]
        details = {"subprocess_error": scrub_secrets(exc.details)} if exc.details else None
        return ProjectOnboardingError(
            code,
            scrub_secrets(exc.message),
            phase=phase,
            details=details,
        )

    async def _with_retained_github_recovery(
        self,
        request_id: str,
        error: ProjectOnboardingError,
    ) -> ProjectOnboardingError:
        record = await self.db.get_onboarding_request(request_id)
        retained_url = self._recorded_url(record or {}, "github_repository")
        if not retained_url:
            return error
        details = {
            **error.details,
            **self._retained_github_details(retained_url),
        }
        return ProjectOnboardingError(
            error.code,
            error.message,
            phase=error.phase,
            details=details,
            field_errors=error.field_errors,
        )

    @staticmethod
    def _retained_github_details(url: str) -> dict[str, str]:
        return {
            "github_repository_url": url,
            "recovery_action": (
                "The GitHub repository was retained. Reuse it or delete it manually "
                "before retrying with a new request id."
            ),
        }

    @staticmethod
    def _actions_for_existing_init(destination: Path, create_readme: bool) -> list[str]:
        actions = ["repository_initialized"]
        if create_readme and (destination / "README.md").is_file():
            actions.append("readme_committed")
        return actions

    @staticmethod
    def _stored_error(record: dict[str, Any]) -> ProjectOnboardingError:
        payload = record.get("error") or {}
        return ProjectOnboardingError(
            payload.get("error_code", ProjectOnboardingErrorCode.REGISTRATION_FAILED.value),
            payload.get("error", "Project onboarding previously failed"),
            phase=payload.get("phase"),
            details=payload.get("details"),
            field_errors=payload.get("field_errors"),
        )


__all__ = ["ProjectOnboardingService"]
