"""Response models for the project-onboarding commands (design §5).

The wire-level twins of ``src.commands.contracts.project_onboarding``'s
result models.  They are re-declared here rather than re-exported because
the API layer's models are plain (non-frozen, ``extra`` permitted) so the
generated Python/TypeScript clients keep tolerating additive fields, while
the contract models are strict.  Field names and types match one-to-one;
``tests/test_project_onboarding_contract.py`` pins the success payload.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


class ProjectRootInfo(BaseModel):
    id: str
    label: str
    path: str
    readable: bool = True
    writable: bool = True


class ListProjectRootsResponse(BaseModel):
    success: bool = True
    roots: list[ProjectRootInfo] = []


class BrowseEntry(BaseModel):
    name: str
    relative_path: str
    is_directory: bool = True
    is_git_repository: bool = False
    selectable: bool = False


class BrowseProjectRootResponse(BaseModel):
    success: bool = True
    root_id: str
    relative_path: str = ""
    entries: list[BrowseEntry] = []
    truncated: bool = False


class GithubAuthStatusResponse(BaseModel):
    success: bool = True
    installed: bool = False
    authenticated: bool = False
    host: str | None = None
    login: str | None = None
    cli_version: str | None = None
    message: str | None = None


class GithubOwner(BaseModel):
    login: str
    kind: Literal["user", "organization"] = "user"
    name: str | None = None


class ListGithubOwnersResponse(BaseModel):
    success: bool = True
    owners: list[GithubOwner] = []


class GithubRepository(BaseModel):
    owner: str
    name: str
    full_name: str
    visibility: Literal["private", "public", "internal"] = "private"
    clone_url_https: str
    clone_url_ssh: str | None = None
    default_branch: str | None = None
    description: str | None = None


class SearchGithubRepositoriesResponse(BaseModel):
    success: bool = True
    repositories: list[GithubRepository] = []
    next_cursor: str | None = None


class OnboardProjectResponse(BaseModel):
    """The success payload of §5.3."""

    success: bool = True
    request_id: str
    project_id: str
    workspace_id: str
    source_type: Literal["link", "init", "clone"]
    root_id: str
    relative_path: str
    canonical_path: str
    default_branch: str
    remote_url: str | None = None
    actions: list[str] = []


class OnboardingErrorInfo(BaseModel):
    error_code: str
    error: str
    phase: str | None = None
    details: dict[str, Any] = {}
    field_errors: list[dict[str, str]] = []


class GetProjectOnboardingResponse(BaseModel):
    success: bool = True
    request_id: str
    status: Literal["pending", "running", "completed", "failed"]
    phase: str | None = None
    result: OnboardProjectResponse | None = None
    error: OnboardingErrorInfo | None = None
    created_at: str | None = None
    updated_at: str | None = None


RESPONSE_MODELS: dict[str, type[BaseModel]] = {
    "list_project_roots": ListProjectRootsResponse,
    "browse_project_root": BrowseProjectRootResponse,
    "get_github_auth_status": GithubAuthStatusResponse,
    "list_github_owners": ListGithubOwnersResponse,
    "search_github_repositories": SearchGithubRepositoriesResponse,
    "onboard_project": OnboardProjectResponse,
    "get_project_onboarding": GetProjectOnboardingResponse,
}
