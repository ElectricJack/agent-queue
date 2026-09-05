"""Project-onboarding commands mixin (design §5, §7).

Wires the seven onboarding command names into ``CommandHandler`` with the
contract's request validation and scope policy.  Local link/init mutations
and durable status reads delegate to ``ProjectOnboardingService``; GitHub and
discovery bodies remain structured stubs for their owning packages.

Scope (§7).  Filesystem authorisation happens on the daemon under the same
privileged local / global-admin policy that gates project management: a
loopback CLI caller (``kind == "local"``) or the global supervisor
(``elevated`` with no project pin) may run these; a per-project supervisor
or a task-scoped worker may not.  ``src/api/scope.py`` refuses the HTTP
surface before dispatch; the check here is the in-handler half so the MCP
surface and direct callers get the same answer.
"""

from __future__ import annotations

import logging
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
    ProjectOnboardingError,
    ProjectOnboardingErrorCode,
    parse_request,
)

logger = logging.getLogger(__name__)


def _not_implemented(command: str) -> ProjectOnboardingError:
    return ProjectOnboardingError(
        ProjectOnboardingErrorCode.NOT_IMPLEMENTED,
        f"{command} is not implemented yet: the onboarding contract has landed, "
        "the service has not",
    )


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
    # result model (or a ``dict`` already in command-result shape).  Later
    # packages replace the remaining discovery stubs without changing the
    # ``_cmd_*`` wrappers above.

    async def _execute_list_project_roots(self, request) -> Any:
        raise _not_implemented(LIST_PROJECT_ROOTS)

    async def _execute_browse_project_root(self, request) -> Any:
        raise _not_implemented(BROWSE_PROJECT_ROOT)

    async def _execute_get_github_auth_status(self, request) -> Any:
        raise _not_implemented(GET_GITHUB_AUTH_STATUS)

    async def _execute_list_github_owners(self, request) -> Any:
        raise _not_implemented(LIST_GITHUB_OWNERS)

    async def _execute_search_github_repositories(self, request) -> Any:
        raise _not_implemented(SEARCH_GITHUB_REPOSITORIES)

    async def _execute_onboard_project(self, request) -> Any:
        from src.projects.onboarding import ProjectOnboardingService

        service = ProjectOnboardingService(self.db, self.config, self.orchestrator.git)
        return await service.onboard_project(request)

    async def _execute_get_project_onboarding(self, request) -> Any:
        from src.projects.onboarding import ProjectOnboardingService

        service = ProjectOnboardingService(self.db, self.config, self.orchestrator.git)
        return await service.get_project_onboarding(request.request_id)
