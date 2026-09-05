"""Project-onboarding commands mixin (design §5, §7).

Wires the seven onboarding command names into ``CommandHandler`` with the
contract's request validation and scope policy.  Local link/init mutations
and durable status reads delegate to ``ProjectOnboardingService``; GitHub
discovery delegates to the daemon-host ``gh`` client, and root browsing uses
the configured root capability boundary.

Scope (§7).  Filesystem authorisation happens on the daemon under the same
privileged local / global-admin policy that gates project management: a
loopback CLI caller (``kind == "local"``) or the global supervisor
(``elevated`` with no project pin) may run these; a per-project supervisor
or a task-scoped worker may not.  ``src/api/scope.py`` refuses the HTTP
surface before dispatch; the check here is the in-handler half so the MCP
surface and direct callers get the same answer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.api.scope import PROJECT_ONBOARDING_SCOPE_ERROR
from src.commands.contracts.project_onboarding import (
    BROWSE_PROJECT_ROOT,
    GET_GITHUB_AUTH_STATUS,
    GET_PROJECT_ONBOARDING,
    LIST_GITHUB_OWNERS,
    LIST_PROJECT_ROOTS,
    ONBOARD_PROJECT,
    SEARCH_GITHUB_REPOSITORIES,
    BrowseEntry,
    BrowseProjectRootResult,
    ListProjectRootsResult,
    ProjectOnboardingError,
    ProjectOnboardingErrorCode,
    ProjectRootInfo,
    parse_request,
)
from src.config import resolve_project_root
from src.projects.github import GhClient, GitHubError, scrub_secrets
from src.projects.paths import ProjectPathError, list_directory

def _display_root_path(path: str) -> str:
    """Render a configured root for an operator without making it an input.

    Root paths have already been normalised while loading configuration.  A
    home-relative display is shorter and avoids exposing the daemon's exact
    home prefix unnecessarily; paths outside the home directory stay absolute.
    """
    root_path = Path(path).expanduser()
    try:
        relative = root_path.relative_to(Path.home())
    except ValueError:
        return str(root_path)
    return "~" if not relative.parts else f"~/{relative.as_posix()}"


def _browse_path_error(error: ProjectPathError) -> ProjectOnboardingError:
    """Translate the path module's four stable browse failures verbatim."""
    return ProjectOnboardingError(ProjectOnboardingErrorCode(error.code.value), error.message)


class ProjectOnboardingCommandsMixin:
    """Registers the seven onboarding commands (§5.1–§5.3)."""

    def _project_onboarding_scope_error(self) -> str | None:
        scope = self._current_scope
        if not scope or scope.get("kind") == "local":
            return None
        if scope.get("elevated") and scope.get("project_id") is None:
            return None
        return PROJECT_ONBOARDING_SCOPE_ERROR

    async def _run_onboarding_command(self, command: str, args: dict[str, Any]) -> dict:
        """Scope gate → contract validation → body, with one error shape."""
        scope_error = self._project_onboarding_scope_error()
        if scope_error is not None:
            return {"success": False, "error": scope_error, "error_code": "out_of_scope"}
        try:
            request = parse_request(command, args)
            body = getattr(self, f"_execute_{command}")
            result = await body(request)
        except ProjectOnboardingError as exc:
            return exc.to_dict()
        if isinstance(result, dict):
            return result
        return {"success": True, **result.model_dump(mode="json")}

    # -- §5.1 root discovery and browsing ---------------------------------

    async def _cmd_list_project_roots(self, args: dict) -> dict:
        """List the configured project roots with readable/writable flags."""
        return await self._run_onboarding_command(LIST_PROJECT_ROOTS, args)

    async def _cmd_browse_project_root(self, args: dict) -> dict:
        """List the child directories of a root-relative path."""
        return await self._run_onboarding_command(BROWSE_PROJECT_ROOT, args)

    # -- §5.2 GitHub discovery --------------------------------------------

    async def _cmd_get_github_auth_status(self, args: dict) -> dict:
        """Report whether the daemon host's gh CLI is installed and authenticated."""
        return await self._run_onboarding_command(GET_GITHUB_AUTH_STATUS, args)

    async def _cmd_list_github_owners(self, args: dict) -> dict:
        """List the GitHub owners the authenticated user can create repositories under."""
        return await self._run_onboarding_command(LIST_GITHUB_OWNERS, args)

    async def _cmd_search_github_repositories(self, args: dict) -> dict:
        """Search GitHub repositories visible to the daemon host's gh session."""
        return await self._run_onboarding_command(SEARCH_GITHUB_REPOSITORIES, args)

    # -- §5.3 onboarding --------------------------------------------------

    async def _cmd_onboard_project(self, args: dict) -> dict:
        """Link, initialise or clone a repository beneath a configured root and register it."""
        return await self._run_onboarding_command(ONBOARD_PROJECT, args)

    async def _cmd_get_project_onboarding(self, args: dict) -> dict:
        """Read the durable status, phase, result or error of an onboarding request."""
        return await self._run_onboarding_command(GET_PROJECT_ONBOARDING, args)

    # -- bodies -----------------------------------------------------------
    #
    # Each takes the validated request model from
    # ``src.commands.contracts.project_onboarding`` and returns the matching
    # result model (or a ``dict`` already in command-result shape).
    async def _execute_list_project_roots(self, request) -> ListProjectRootsResult:
        """Return the currently configured roots and live filesystem capabilities."""
        return ListProjectRootsResult(
            roots=[
                ProjectRootInfo(
                    id=root.id,
                    label=root.label,
                    path=_display_root_path(root.path),
                    readable=root.readable,
                    writable=root.writable,
                )
                for root in self.config.project_roots
            ]
        )

    async def _execute_browse_project_root(self, request) -> BrowseProjectRootResult:
        """Safely list one configured root-relative directory (§5.1)."""
        root = resolve_project_root(self.config, request.root_id)
        # A removed root and an unavailable root deliberately share the same
        # failure: callers must not treat a former root id as a filesystem
        # capability or learn whether its path still exists.
        if root is None or not root.readable:
            raise ProjectOnboardingError(
                ProjectOnboardingErrorCode.ROOT_UNAVAILABLE, "project root is unavailable"
            )
        try:
            listing = list_directory(Path(root.path), request.relative_path)
        except ProjectPathError as error:
            raise _browse_path_error(error) from None
        return BrowseProjectRootResult(
            root_id=root.id,
            relative_path=listing.relative,
            entries=[
                BrowseEntry(
                    name=entry.name,
                    relative_path=entry.relative_path,
                    is_directory=entry.is_dir,
                    is_git_repository=entry.is_git_repo,
                    selectable=entry.selectable,
                )
                for entry in listing.entries
            ],
            truncated=listing.truncated,
        )

    def _github_client(self) -> GhClient:
        """Build the daemon-host ``gh`` client.

        This small seam keeps the command tests on the same fake executable as
        the client tests, without putting a test-only option on the command
        surface.
        """
        return GhClient()

    @staticmethod
    def _github_error_result(error: GitHubError) -> dict[str, Any]:
        """Return one safe command error for a failed GitHub discovery call."""
        # ``GitHubError`` scrubs at construction, but preserve that invariant
        # at this boundary too: command results are an external surface.
        message = scrub_secrets(error.message)
        if error.code.value in {
            ProjectOnboardingErrorCode.GITHUB_CLI_MISSING.value,
            ProjectOnboardingErrorCode.GITHUB_AUTH_REQUIRED.value,
        }:
            return ProjectOnboardingError(error.code.value, message).to_dict()
        return {"success": False, "error": message, "error_code": error.code.value}

    async def _execute_get_github_auth_status(self, request) -> Any:
        try:
            status = await self._github_client().auth_status()
        except GitHubError as error:
            return self._github_error_result(error)

        if not status.installed:
            return ProjectOnboardingError(
                ProjectOnboardingErrorCode.GITHUB_CLI_MISSING,
                "the GitHub CLI (gh) is not installed on the daemon host; "
                "install it and run 'gh auth login'",
            ).to_dict()
        if not status.authenticated:
            return ProjectOnboardingError(
                ProjectOnboardingErrorCode.GITHUB_AUTH_REQUIRED,
                "the daemon host's GitHub CLI is not logged in; run 'gh auth login' there",
            ).to_dict()
        return {
            "success": True,
            "installed": True,
            "authenticated": True,
            "host": status.hostname,
            "login": status.login,
        }

    async def _execute_list_github_owners(self, request) -> Any:
        try:
            owners = await self._github_client().list_owners()
        except GitHubError as error:
            return self._github_error_result(error)
        return {
            "success": True,
            "owners": [
                {"login": owner.login, "kind": owner.kind}
                for owner in owners
            ]
        }

    async def _execute_search_github_repositories(self, request) -> Any:
        try:
            page = await self._github_client().search_repositories(
                request.query, cursor=request.cursor, limit=request.limit
            )
        except GitHubError as error:
            return self._github_error_result(error)
        return {
            "success": True,
            "repositories": [
                {
                    "owner": repository.owner,
                    "name": repository.name,
                    "full_name": repository.full_name,
                    "visibility": repository.visibility,
                    "clone_url_https": repository.clone_https,
                    "clone_url_ssh": repository.clone_ssh,
                    "default_branch": repository.default_branch,
                }
                for repository in page.repositories
            ],
            "next_cursor": page.next_cursor,
        }

    async def _execute_onboard_project(self, request) -> Any:
        from src.projects.onboarding import ProjectOnboardingService

        service = ProjectOnboardingService(self.db, self.config, self.orchestrator.git)
        return await service.onboard_project(request)

    async def _execute_get_project_onboarding(self, request) -> Any:
        from src.projects.onboarding import ProjectOnboardingService

        service = ProjectOnboardingService(self.db, self.config, self.orchestrator.git)
        return await service.get_project_onboarding(request.request_id)
