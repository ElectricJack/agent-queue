"""Project-onboarding command contract (design §5, §8).

The typed request and response shapes for the seven onboarding commands,
the stable error codes, and the parsers every surface funnels through.
This module holds **no behaviour**: ``ProjectOnboardingCommandsMixin``
(``src/commands/project_onboarding_commands.py``) parses with it and the
later service packages return the result types declared here.

These models deliberately mirror the Playbook V2 contract models
(:class:`~src.commands.contracts.models.CommandArgs` /
:class:`~src.commands.contracts.models.CommandValue`: ``extra="forbid"``,
frozen) but are **not** registered in :data:`~src.commands.contracts.CONTRACTS`
— that registry is the fingerprinted pipeline-command surface, and the
onboarding commands are an operator surface gated by the global-admin scope
policy (``src/api/scope.py``), not by capability contracts.

Wire shape.  ``onboard_project`` is one flat JSON object: the common fields
of §5.3 plus the mode-specific fields, discriminated by ``source_mode``.
That is what the generated API/TS clients see, and it is what the wizard
posts.  Validation is strict: unknown fields fail, and so does any field that
belongs to a different mode (a ``link`` request carrying ``create_readme``,
for instance).
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Any, Final, Literal

from pydantic import (
    Field,
    TypeAdapter,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from src.commands.contracts.models import CommandArgs, CommandValue

# ---------------------------------------------------------------------------
# Command names
# ---------------------------------------------------------------------------

LIST_PROJECT_ROOTS: Final = "list_project_roots"
BROWSE_PROJECT_ROOT: Final = "browse_project_root"
GET_GITHUB_AUTH_STATUS: Final = "get_github_auth_status"
LIST_GITHUB_OWNERS: Final = "list_github_owners"
SEARCH_GITHUB_REPOSITORIES: Final = "search_github_repositories"
ONBOARD_PROJECT: Final = "onboard_project"
GET_PROJECT_ONBOARDING: Final = "get_project_onboarding"

#: The seven onboarding commands (§5.1–§5.3).
ONBOARDING_COMMANDS: Final[frozenset[str]] = frozenset(
    {
        LIST_PROJECT_ROOTS,
        BROWSE_PROJECT_ROOT,
        GET_GITHUB_AUTH_STATUS,
        LIST_GITHUB_OWNERS,
        SEARCH_GITHUB_REPOSITORIES,
        ONBOARD_PROJECT,
        GET_PROJECT_ONBOARDING,
    }
)

# ---------------------------------------------------------------------------
# Errors (§8, plus the browsing codes of §5.1 and the request-level codes)
# ---------------------------------------------------------------------------


class ProjectOnboardingErrorCode(StrEnum):
    """Stable, machine-readable error codes.

    The wizard keys its recovery UX on these (§8), so a code, once shipped,
    is never renamed.  Add codes; do not repurpose them.
    """

    # request-level
    INVALID_REQUEST = "invalid_request"
    REQUEST_CONFLICT = "request_conflict"  # same request_id, different inputs
    NOT_IMPLEMENTED = "not_implemented"  # deferred command
    # browsing (§5.1)
    NOT_FOUND = "not_found"
    NOT_DIRECTORY = "not_directory"
    ROOT_ESCAPE = "root_escape"
    ROOT_UNAVAILABLE = "root_unavailable"
    # onboarding (§8)
    PROJECT_ID_CONFLICT = "project_id_conflict"
    DESTINATION_CONFLICT = "destination_conflict"
    DESTINATION_LOCKED = "destination_locked"
    INVALID_GIT_REPOSITORY = "invalid_git_repository"
    GITHUB_CLI_MISSING = "github_cli_missing"
    GITHUB_AUTH_REQUIRED = "github_auth_required"
    GITHUB_REPOSITORY_INACCESSIBLE = "github_repository_inaccessible"
    GITHUB_REPOSITORY_CONFLICT = "github_repository_conflict"
    CLONE_FAILED = "clone_failed"
    INIT_FAILED = "init_failed"
    COMMIT_FAILED = "commit_failed"
    PUSH_FAILED = "push_failed"
    REGISTRATION_FAILED = "registration_failed"


class ProjectOnboardingError(Exception):
    """A structured onboarding failure.

    ``to_dict()`` is the command-result shape every surface returns:
    ``{"success": False, "error": <message>, "error_code": <code>, ...}``.
    ``phase`` names the saga step that failed (§8 shows it with the retry
    action); ``details`` carries safe, non-secret facts such as a GitHub
    repository that survived a later failure (§6.3); ``field_errors`` attaches
    validation failures to their fields (``{"field", "message"}`` each).
    """

    def __init__(
        self,
        code: ProjectOnboardingErrorCode | str,
        message: str,
        *,
        phase: str | None = None,
        details: dict[str, Any] | None = None,
        field_errors: list[dict[str, str]] | None = None,
    ) -> None:
        # A string that is not a stable code is a programming error, not a
        # new code: ``StrEnum(value)`` raises ``ValueError`` for it.
        self.code: str = ProjectOnboardingErrorCode(code).value
        self.message = message
        self.phase = phase
        self.details = dict(details) if details else {}
        self.field_errors = [dict(e) for e in field_errors] if field_errors else []
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "success": False,
            "error": self.message,
            "error_code": self.code,
        }
        if self.phase is not None:
            payload["phase"] = self.phase
        if self.details:
            payload["details"] = self.details
        if self.field_errors:
            payload["field_errors"] = self.field_errors
        return payload

    @classmethod
    def from_validation_error(
        cls, command: str, exc: ValidationError, *, discriminator: str | None = None
    ) -> ProjectOnboardingError:
        """Fold a pydantic error into ``invalid_request`` with per-field entries.

        For a discriminated union pydantic prefixes each member's error
        location with the tag (``("init", "github_owner")``) and reports a
        bad or missing tag at the root; both are folded back onto the plain
        field names the wizard knows (§8 attaches errors to fields).
        """
        field_errors: list[dict[str, str]] = []
        for err in exc.errors():
            loc = list(err["loc"])
            if discriminator is not None:
                if err["type"] in ("union_tag_invalid", "union_tag_not_found"):
                    loc = [discriminator]
                elif loc and loc[0] in _SOURCE_MODES:
                    loc = loc[1:]
            field_errors.append(
                {
                    "field": ".".join(str(part) for part in loc) or "<request>",
                    "message": err["msg"],
                }
            )
        fields = ", ".join(sorted({e["field"] for e in field_errors}))
        return cls(
            ProjectOnboardingErrorCode.INVALID_REQUEST,
            f"{command}: invalid request ({fields})",
            field_errors=field_errors,
        )


# ---------------------------------------------------------------------------
# Field rules shared by several requests
# ---------------------------------------------------------------------------

#: URL-safe project id (§3.4): lower-case alphanumerics separated by single
#: ``-``/``_``/``.`` runs are the shape ``GitManager.slugify`` produces.
PROJECT_ID_PATTERN: Final = r"^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$"

#: Root ids are the same shape (§3.2: stable and URL-safe).
ROOT_ID_PATTERN: Final = r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$"

MAX_RELATIVE_PATH_LENGTH: Final = 1024
MAX_SEARCH_QUERY_LENGTH: Final = 200
MAX_SEARCH_LIMIT: Final = 50
DEFAULT_SEARCH_LIMIT: Final = 20

SourceMode = Literal["link", "init", "github_clone"]
_SOURCE_MODES: Final[frozenset[str]] = frozenset({"link", "init", "github_clone"})
GithubVisibility = Literal["private", "public"]


def validate_relative_path(value: str, *, allow_empty: bool) -> str:
    """Syntactic containment (§3.3).

    Rejects absolute paths, ``..`` components, NUL bytes, and empty/blank
    values (unless *allow_empty*, for "the root itself" when browsing).  The
    filesystem-aware half — symlink resolution, real containment under the
    root, ``.aq/worktrees`` aliasing — runs in the service, which repeats
    validation against the resolved root at browse, preflight and mutation
    time.  A path that fails here never reaches it.
    """
    if "\x00" in value:
        raise ValueError("must not contain NUL bytes")
    if len(value) > MAX_RELATIVE_PATH_LENGTH:
        raise ValueError(f"must be at most {MAX_RELATIVE_PATH_LENGTH} characters")
    stripped = value.strip()
    if stripped != value:
        raise ValueError("must not have leading or trailing whitespace")
    if value == "":
        if allow_empty:
            return value
        raise ValueError("must not be empty")
    if value.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", value):
        raise ValueError("must be relative to the selected root, not absolute")
    parts = re.split(r"[\\/]+", value)
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("must be a plain descendant path (no '.', '..' or empty components)")
    return value


# ---------------------------------------------------------------------------
# §5.1 Root discovery and browsing
# ---------------------------------------------------------------------------


class ListProjectRootsArgs(CommandArgs):
    """``list_project_roots`` takes no arguments."""


class ProjectRootInfo(CommandValue):
    id: str
    label: str
    #: Display path only (home-relative where possible).  The dashboard never
    #: sends it back; it addresses roots by ``id`` (§3.3).
    path: str
    readable: bool
    writable: bool


class ListProjectRootsResult(CommandValue):
    roots: list[ProjectRootInfo]


class BrowseProjectRootArgs(CommandArgs):
    root_id: str = Field(pattern=ROOT_ID_PATTERN, max_length=128)
    #: Root-relative directory; ``""`` is the root itself.
    relative_path: str = ""

    @field_validator("relative_path")
    @classmethod
    def _relative_path(cls, value: str) -> str:
        return validate_relative_path(value, allow_empty=True)


class BrowseEntry(CommandValue):
    name: str
    relative_path: str
    is_directory: bool
    is_git_repository: bool
    #: Whether the entry may be chosen as a ``link`` source (§3.3: only a
    #: valid Git worktree root).
    selectable: bool


class BrowseProjectRootResult(CommandValue):
    root_id: str
    #: Normalised root-relative directory that was listed.
    relative_path: str
    entries: list[BrowseEntry]
    #: True when the bounded result count cut the listing short (§5.1).
    truncated: bool = False


# ---------------------------------------------------------------------------
# §5.2 GitHub discovery
# ---------------------------------------------------------------------------


class GetGithubAuthStatusArgs(CommandArgs):
    """``get_github_auth_status`` takes no arguments."""


class GithubAuthStatus(CommandValue):
    installed: bool
    authenticated: bool
    #: GitHub host (``github.com`` unless an enterprise host is configured).
    host: str | None = None
    #: The authenticated login — an identity, never a credential.
    login: str | None = None
    cli_version: str | None = None
    #: Setup-oriented guidance when not installed/authenticated (§5.2).
    message: str | None = None


class ListGithubOwnersArgs(CommandArgs):
    """``list_github_owners`` takes no arguments."""


class GithubOwner(CommandValue):
    login: str
    kind: Literal["user", "organization"]
    name: str | None = None


class ListGithubOwnersResult(CommandValue):
    owners: list[GithubOwner]


class SearchGithubRepositoriesArgs(CommandArgs):
    query: str = Field(min_length=1, max_length=MAX_SEARCH_QUERY_LENGTH)
    limit: int = Field(default=DEFAULT_SEARCH_LIMIT, ge=1, le=MAX_SEARCH_LIMIT)
    #: Opaque page cursor from a previous result.
    cursor: str | None = Field(default=None, max_length=512)


class GithubRepositoryRef(CommandValue):
    """The canonical owner/name identity a discovery result hands back."""

    owner: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)


class GithubRepository(CommandValue):
    owner: str
    name: str
    full_name: str
    visibility: Literal["private", "public", "internal"]
    clone_url_https: str
    clone_url_ssh: str | None = None
    default_branch: str | None = None
    description: str | None = None


class SearchGithubRepositoriesResult(CommandValue):
    repositories: list[GithubRepository]
    next_cursor: str | None = None


# ---------------------------------------------------------------------------
# §5.3 Onboarding request — common fields + discriminated union
# ---------------------------------------------------------------------------


class _OnboardProjectCommon(CommandArgs):
    """The fields every ``source_mode`` shares (§5.3)."""

    #: Idempotency key.  Durable: replaying a completed request returns its
    #: prior result; the same id with different normalised inputs is
    #: ``request_conflict``.
    request_id: str = Field(min_length=1, max_length=128)
    root_id: str = Field(pattern=ROOT_ID_PATTERN, max_length=128)
    #: Root-relative destination (``link``: the repository; ``init`` /
    #: ``github_clone``: the directory to create).
    relative_path: str
    project_name: str = Field(min_length=1, max_length=200)
    project_id: str = Field(pattern=PROJECT_ID_PATTERN, max_length=100)
    #: ``None`` means "detect" for ``link``/``github_clone`` and ``main`` for
    #: ``init`` (§3.4); the service resolves it and echoes the answer.
    default_branch: str | None = Field(default=None, min_length=1, max_length=255)

    @field_validator("relative_path")
    @classmethod
    def _relative_path(cls, value: str) -> str:
        return validate_relative_path(value, allow_empty=False)

    @field_validator("project_name")
    @classmethod
    def _project_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class LinkOnboardingRequest(_OnboardProjectCommon):
    """Link an existing local repository (§4.3): no extra fields."""

    source_mode: Literal["link"]


class InitOnboardingRequest(_OnboardProjectCommon):
    """Initialise a new repository, optionally with a GitHub remote (§4.4).

    The GitHub fields are validated per field (not in a model validator) so
    a failure is attached to the field the wizard must focus (§8).  Field
    order matters: ``create_github`` is declared first so the validators
    below can read it from ``info.data``.
    """

    source_mode: Literal["init"]
    create_readme: bool = True
    create_github: bool = False
    github_owner: str | None = Field(
        default=None, min_length=1, max_length=100, validate_default=True
    )
    #: Defaults to the destination directory name when omitted.
    github_repo: str | None = Field(default=None, min_length=1, max_length=200)
    github_visibility: GithubVisibility = "private"

    @field_validator("github_owner")
    @classmethod
    def _owner_follows_create_github(cls, value: str | None, info: ValidationInfo) -> str | None:
        create_github = info.data.get("create_github", False)
        if create_github and value is None:
            raise ValueError("required when create_github is true")
        if not create_github and value is not None:
            raise ValueError("only applies when create_github is true")
        return value

    @field_validator("github_repo")
    @classmethod
    def _repo_follows_create_github(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is not None and not info.data.get("create_github", False):
            raise ValueError("only applies when create_github is true")
        return value

    @model_validator(mode="after")
    def _visibility_follows_create_github(self) -> InitOnboardingRequest:
        if "github_visibility" in self.model_fields_set and not self.create_github:
            raise ValueError("github_visibility only applies when create_github is true")
        return self


class GithubCloneOnboardingRequest(_OnboardProjectCommon):
    """Clone a GitHub repository (§4.5): exactly one source.

    ``github_repository`` is declared first so the ``github_url`` validator
    can see it in ``info.data``; ``validate_default=True`` makes the
    "neither" case fail on ``github_url`` too.
    """

    source_mode: Literal["github_clone"]
    #: Selected through discovery (``search_github_repositories``).
    github_repository: GithubRepositoryRef | None = None
    #: Pasted HTTPS/SSH URL or accepted shorthand; normalised by the service.
    github_url: str | None = Field(
        default=None, min_length=1, max_length=512, validate_default=True
    )

    @field_validator("github_url")
    @classmethod
    def _exactly_one_source(cls, value: str | None, info: ValidationInfo) -> str | None:
        # Absent from ``info.data`` when github_repository itself failed —
        # that failure is already reported on its own field.
        if "github_repository" not in info.data:
            return value
        if (info.data["github_repository"] is None) == (value is None):
            raise ValueError("exactly one of github_repository or github_url is required")
        return value


OnboardProjectRequest = Annotated[
    LinkOnboardingRequest | InitOnboardingRequest | GithubCloneOnboardingRequest,
    Field(discriminator="source_mode"),
]
_ONBOARD_PROJECT_ADAPTER: TypeAdapter[Any] = TypeAdapter(OnboardProjectRequest)

#: Values ``OnboardProjectResult.actions`` may carry.  The dashboard renders
#: these on the success page and the review page lists them ahead of time
#: (§4.2, §8); a service that performs a new kind of action adds it here.
OnboardingAction = Literal[
    "repository_linked",
    "repository_initialized",
    "readme_committed",
    "github_repository_created",
    "remote_configured",
    "branch_pushed",
    "repository_cloned",
    "project_created",
    "workspace_registered",
    "vault_initialized",
]


class OnboardProjectResult(CommandValue):
    """The success payload of §5.3."""

    request_id: str
    project_id: str
    workspace_id: str
    source_type: Literal["link", "init", "clone"]
    root_id: str
    relative_path: str
    canonical_path: str
    default_branch: str
    remote_url: str | None = None
    actions: list[OnboardingAction]


# ---------------------------------------------------------------------------
# §5.3 get_project_onboarding
# ---------------------------------------------------------------------------

OnboardingStatus = Literal["pending", "running", "completed", "failed"]
OnboardingPhase = Literal[
    "validate", "preflight", "prepare", "github", "publish", "register", "done"
]


class GetProjectOnboardingArgs(CommandArgs):
    request_id: str = Field(min_length=1, max_length=128)


class OnboardingErrorInfo(CommandValue):
    """The scrubbed error stored on a failed request (never a secret)."""

    error_code: str
    error: str
    phase: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    field_errors: list[dict[str, str]] = Field(default_factory=list)


class GetProjectOnboardingResult(CommandValue):
    request_id: str
    status: OnboardingStatus
    phase: OnboardingPhase | None = None
    result: OnboardProjectResult | None = None
    error: OnboardingErrorInfo | None = None
    created_at: str | None = None
    updated_at: str | None = None


# ---------------------------------------------------------------------------
# Command → model tables and parsers
# ---------------------------------------------------------------------------

REQUEST_MODELS: Final[dict[str, type[CommandArgs]]] = {
    LIST_PROJECT_ROOTS: ListProjectRootsArgs,
    BROWSE_PROJECT_ROOT: BrowseProjectRootArgs,
    GET_GITHUB_AUTH_STATUS: GetGithubAuthStatusArgs,
    LIST_GITHUB_OWNERS: ListGithubOwnersArgs,
    SEARCH_GITHUB_REPOSITORIES: SearchGithubRepositoriesArgs,
    # ``onboard_project`` is a union; ``parse_request`` special-cases it.
    ONBOARD_PROJECT: _OnboardProjectCommon,
    GET_PROJECT_ONBOARDING: GetProjectOnboardingArgs,
}

RESULT_MODELS: Final[dict[str, type[CommandValue]]] = {
    LIST_PROJECT_ROOTS: ListProjectRootsResult,
    BROWSE_PROJECT_ROOT: BrowseProjectRootResult,
    GET_GITHUB_AUTH_STATUS: GithubAuthStatus,
    LIST_GITHUB_OWNERS: ListGithubOwnersResult,
    SEARCH_GITHUB_REPOSITORIES: SearchGithubRepositoriesResult,
    ONBOARD_PROJECT: OnboardProjectResult,
    GET_PROJECT_ONBOARDING: GetProjectOnboardingResult,
}


def parse_onboard_project_request(
    payload: dict[str, Any],
) -> LinkOnboardingRequest | InitOnboardingRequest | GithubCloneOnboardingRequest:
    """Validate an ``onboard_project`` payload into its mode-specific request.

    Raises :class:`ProjectOnboardingError` (``invalid_request``) with one
    ``field_errors`` entry per failing field.
    """
    try:
        return _ONBOARD_PROJECT_ADAPTER.validate_python(payload)
    except ValidationError as exc:
        raise ProjectOnboardingError.from_validation_error(
            ONBOARD_PROJECT, exc, discriminator="source_mode"
        ) from None


def parse_request(command: str, payload: dict[str, Any]) -> CommandArgs:
    """Validate *payload* against the request model of *command*.

    Raises ``KeyError`` for a name outside :data:`ONBOARDING_COMMANDS` and
    :class:`ProjectOnboardingError` (``invalid_request``) for a bad payload.
    """
    if command == ONBOARD_PROJECT:
        return parse_onboard_project_request(payload)
    model = REQUEST_MODELS[command]
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise ProjectOnboardingError.from_validation_error(command, exc) from None


__all__ = [
    "BROWSE_PROJECT_ROOT",
    "DEFAULT_SEARCH_LIMIT",
    "GET_GITHUB_AUTH_STATUS",
    "GET_PROJECT_ONBOARDING",
    "LIST_GITHUB_OWNERS",
    "LIST_PROJECT_ROOTS",
    "MAX_SEARCH_LIMIT",
    "MAX_SEARCH_QUERY_LENGTH",
    "ONBOARDING_COMMANDS",
    "ONBOARD_PROJECT",
    "PROJECT_ID_PATTERN",
    "REQUEST_MODELS",
    "RESULT_MODELS",
    "ROOT_ID_PATTERN",
    "SEARCH_GITHUB_REPOSITORIES",
    "BrowseEntry",
    "BrowseProjectRootArgs",
    "BrowseProjectRootResult",
    "GetGithubAuthStatusArgs",
    "GetProjectOnboardingArgs",
    "GetProjectOnboardingResult",
    "GithubAuthStatus",
    "GithubCloneOnboardingRequest",
    "GithubOwner",
    "GithubRepository",
    "GithubRepositoryRef",
    "GithubVisibility",
    "InitOnboardingRequest",
    "LinkOnboardingRequest",
    "ListGithubOwnersArgs",
    "ListGithubOwnersResult",
    "ListProjectRootsArgs",
    "ListProjectRootsResult",
    "OnboardProjectRequest",
    "OnboardProjectResult",
    "OnboardingAction",
    "OnboardingErrorInfo",
    "OnboardingPhase",
    "OnboardingStatus",
    "ProjectOnboardingError",
    "ProjectOnboardingErrorCode",
    "ProjectRootInfo",
    "SearchGithubRepositoriesArgs",
    "SearchGithubRepositoriesResult",
    "SourceMode",
    "parse_onboard_project_request",
    "parse_request",
    "validate_relative_path",
]
