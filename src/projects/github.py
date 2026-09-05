"""GitHub repository identity normalisation and an async ``gh`` CLI wrapper.

Project-onboarding design §4.5 (identity normalisation), §5.2 (discovery
commands) and §7 (security).  The dashboard lets an operator paste a GitHub
URL or pick a repository through the daemon host's ``gh`` session; this module
is the single place that turns those inputs into a validated identity and
talks to the ``gh`` executable.

Contract
--------
* :func:`parse_github_repository` accepts HTTPS URLs (with or without
  ``.git`` / trailing slash), SSH URLs (``git@github.com:o/r.git`` and
  ``ssh://git@github.com/o/r``) and the shorthands ``owner/repo`` and
  ``github.com/owner/repo``.  It returns a :class:`GitHubRepo` whose clone
  URLs are *derived* from the validated ``owner`` / ``name`` — never copied
  from the input — so the caller only ever hands ``git`` a URL this module
  built.  Anything on another host, with embedded credentials, or with
  invalid owner/name characters raises :class:`GitHubError` with
  ``github_repository_inaccessible``.
* :class:`GhClient` runs ``gh`` through :func:`asyncio.create_subprocess_exec`
  with argument arrays only.  User-controlled values (query, cursor, owner,
  name) travel as single arguments, never through a shell string.  A missing
  binary is ``github_cli_missing``; a logged-out session is
  ``github_auth_required``; any other non-zero exit is ``github_cli_failed``.
* :func:`scrub_secrets` removes credential-bearing URLs, GitHub token
  literals, ``Authorization`` headers, ``GH_TOKEN=`` style assignments and
  ``gh auth status`` / ``gh auth token`` output before any subprocess text is
  logged or returned.  :class:`GitHubError` applies it to its own message and
  details, so every error raised here is safe to return to the dashboard.

The module never reads a token itself, never runs ``gh auth token``, and does
not import ``src.config`` or the database.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any, Literal
from urllib.parse import urlsplit

__all__ = [
    "DEFAULT_SEARCH_LIMIT",
    "GITHUB_HOST",
    "MAX_QUERY_LENGTH",
    "MAX_SEARCH_LIMIT",
    "GhClient",
    "GitHubAuthStatus",
    "GitHubError",
    "GitHubErrorCode",
    "GitHubOwner",
    "GitHubRepo",
    "GitHubRepository",
    "RepositorySearchPage",
    "parse_github_repository",
    "scrub_secrets",
]

logger = logging.getLogger(__name__)

#: The only host this flow accepts (design §4.5: GitHub only).
GITHUB_HOST = "github.com"

#: Bounds on :meth:`GhClient.search_repositories` input (design §5.2: bounded query).
MAX_QUERY_LENGTH = 256
DEFAULT_SEARCH_LIMIT = 20
MAX_SEARCH_LIMIT = 50

Visibility = Literal["private", "public"]
_VISIBILITIES: frozenset[str] = frozenset({"private", "public"})

# GitHub login rules: alphanumerics and single hyphens, no leading/trailing
# hyphen, at most 39 characters.  Repository names: alphanumerics, ``.``,
# ``_`` and ``-``, at most 100 characters, never ``.`` / ``..``.
_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")

# ``git@github.com:owner/repo(.git)``
_SCP_SSH_RE = re.compile(r"^git@(?P<host>[^:/]+):(?P<path>[^:]*)$")
# ``owner/repo`` and ``github.com/owner/repo`` (no scheme)
_SHORTHAND_RE = re.compile(r"^(?:(?P<host>[A-Za-z0-9.-]+\.[A-Za-z]{2,})/)?(?P<path>[^/].*)$")


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------


class GitHubErrorCode(StrEnum):
    """Structured failure codes (design §8, plus two module-level extras).

    ``github_cli_failed`` is the generic non-zero exit / timeout the design
    says setup problems must *not* be reported as; ``github_invalid_input``
    covers a bounded-input violation (empty query, unknown visibility) that is
    the caller's mistake rather than a GitHub or ``gh`` state.
    """

    CLI_MISSING = "github_cli_missing"
    AUTH_REQUIRED = "github_auth_required"
    REPOSITORY_INACCESSIBLE = "github_repository_inaccessible"
    REPOSITORY_CONFLICT = "github_repository_conflict"
    CLI_FAILED = "github_cli_failed"
    INVALID_INPUT = "github_invalid_input"


class GitHubError(ValueError):
    """A GitHub identity was rejected or a ``gh`` invocation failed.

    ``message`` is a short operator-facing sentence and ``details`` the
    (scrubbed) subprocess output, when there is any.  Both pass through
    :func:`scrub_secrets` on construction, so the exception — ``str()``,
    ``args``, :meth:`to_dict` — can be logged or returned as-is.
    """

    def __init__(self, code: GitHubErrorCode, message: str, *, details: str | None = None) -> None:
        self.code = GitHubErrorCode(code)
        self.message = scrub_secrets(message)
        self.details = scrub_secrets(details) if details else None
        super().__init__(f"{self.code.value}: {self.message}")

    def to_dict(self) -> dict[str, str | None]:
        return {"code": self.code.value, "message": self.message, "details": self.details}


# --------------------------------------------------------------------------
# secret scrubbing
# --------------------------------------------------------------------------

_SCRUBBERS: tuple[tuple[re.Pattern[str], str], ...] = (
    # scheme://user:password@host  /  scheme://token@host
    (re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)[^/\s@]+@"), r"\g<scheme>***@"),
    # bare ``user:token@host`` fragments (git credential helper style)
    (re.compile(r"(?<![\w/.:@-])[\w.-]+:[^\s@/]+@(?=[\w.-]+)"), "***@"),
    # GitHub token literals in any of the documented prefixes
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"), "***"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{16,}\b"), "***"),
    # HTTP Authorization headers
    (re.compile(r"(?i)(authorization\s*:\s*)(?:token|bearer|basic)\s+\S+"), r"\1***"),
    # environment-style assignments gh honours
    (
        re.compile(r"\b(GH_TOKEN|GITHUB_TOKEN|GH_ENTERPRISE_TOKEN|GITHUB_ENTERPRISE_TOKEN)=\S+"),
        r"\1=***",
    ),
    # ``gh auth status`` masks the token itself (``Token: ****ab12``) but the
    # tail is still a hint; drop the whole value.
    (re.compile(r"(?im)^(\s*-?\s*token\s*:\s*)\S+.*$"), r"\1***"),
)


def scrub_secrets(text: str | None) -> str:
    """Return ``text`` with credential-bearing fragments replaced by ``***``.

    Removes user-info from URLs (keeping scheme and host so the operator can
    still tell *what* failed), GitHub token literals (``ghp_``, ``gho_``,
    ``ghu_``, ``ghs_``, ``ghr_``, ``github_pat_``), ``Authorization`` header
    values, ``GH_TOKEN=`` style assignments and any ``Token:`` line of the
    kind ``gh auth status`` / ``gh auth token`` print.  Non-string input
    scrubs to the empty string rather than raising: this runs inside error
    paths and must never itself fail.
    """
    if not isinstance(text, str) or not text:
        return ""
    for pattern, replacement in _SCRUBBERS:
        text = pattern.sub(replacement, text)
    return text


# --------------------------------------------------------------------------
# identity
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GitHubRepo:
    """A validated ``owner/name`` identity with clone URLs built from it."""

    owner: str
    name: str
    clone_https: str
    clone_ssh: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def html_url(self) -> str:
        return f"https://{GITHUB_HOST}/{self.full_name}"

    def to_dict(self) -> dict[str, str]:
        return {
            "owner": self.owner,
            "name": self.name,
            "full_name": self.full_name,
            "html_url": self.html_url,
            "clone_https": self.clone_https,
            "clone_ssh": self.clone_ssh,
        }


def _inaccessible(message: str) -> GitHubError:
    return GitHubError(GitHubErrorCode.REPOSITORY_INACCESSIBLE, message)


def _build_repo(owner: str, name: str) -> GitHubRepo:
    """Validate ``owner`` / ``name`` and derive the clone URLs from them."""
    name = name.removesuffix(".git")
    if not owner or not _OWNER_RE.match(owner):
        raise _inaccessible(
            "GitHub owner must be 1-39 letters, digits or single hyphens "
            "(not starting or ending with a hyphen)"
        )
    if not name or name in {".", ".."} or not _NAME_RE.match(name):
        raise _inaccessible("GitHub repository name must be 1-100 letters, digits, '.', '_' or '-'")
    return GitHubRepo(
        owner=owner,
        name=name,
        clone_https=f"https://{GITHUB_HOST}/{owner}/{name}.git",
        clone_ssh=f"git@{GITHUB_HOST}:{owner}/{name}.git",
    )


def _split_path(path: str) -> tuple[str, str]:
    """``owner/name`` from a URL path; exactly two non-empty components."""
    parts = path.strip("/").split("/")
    if len(parts) != 2 or not all(parts):
        raise _inaccessible(
            "expected a GitHub repository as https://github.com/<owner>/<repo>, "
            "git@github.com:<owner>/<repo>.git or <owner>/<repo>"
        )
    return parts[0], parts[1]


def _is_github_host(host: str | None) -> bool:
    return (host or "").lower() in {GITHUB_HOST, "www." + GITHUB_HOST}


def parse_github_repository(text: str) -> GitHubRepo:
    """Normalise any accepted GitHub repository reference to a :class:`GitHubRepo`.

    Accepted forms (case-insensitive scheme and host; owner/name case kept):

    * ``https://github.com/owner/repo`` with optional ``.git`` and/or ``/``
    * ``git@github.com:owner/repo`` with optional ``.git``
    * ``ssh://git@github.com[:22]/owner/repo`` with optional ``.git``
    * ``owner/repo`` and ``github.com/owner/repo``

    Everything else — another host, ``http://``, a query string or fragment,
    extra path segments, a non-``git`` SSH user, a non-default port,
    embedded credentials, invalid characters, control characters — raises
    :class:`GitHubError` (``github_repository_inaccessible``).  The message
    never echoes the input, so a pasted credential cannot leak through it.
    """
    if not isinstance(text, str):
        raise _inaccessible("GitHub repository reference must be a string")
    text = text.strip()
    if not text:
        raise _inaccessible("GitHub repository reference is empty")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
        raise _inaccessible("GitHub repository reference contains control characters")

    if "://" in text:
        parts = urlsplit(text)
        scheme = parts.scheme.lower()
        if scheme not in {"https", "ssh"}:
            raise _inaccessible("GitHub repository URL must use https:// or ssh://")
        # The only user-info ever accepted is the ``git`` login of an SSH URL.
        git_over_ssh = scheme == "ssh" and parts.username == "git" and parts.password is None
        if "@" in parts.netloc and not git_over_ssh:
            raise _inaccessible(
                "GitHub repository URL must not embed credentials; "
                "the daemon host's gh session is used instead"
            )
        if not _is_github_host(parts.hostname):
            raise _inaccessible("only repositories on github.com are supported")
        if parts.query or parts.fragment:
            raise _inaccessible("GitHub repository URL must not carry a query string or fragment")
        try:
            port = parts.port
        except ValueError:
            raise _inaccessible("GitHub repository URL has an invalid port") from None
        if scheme == "ssh":
            if parts.username != "git":
                raise _inaccessible("GitHub SSH URLs must use the git@ user")
            if port not in (None, 22):
                raise _inaccessible("GitHub SSH URLs must use the default port")
        elif port not in (None, 443):
            raise _inaccessible("GitHub HTTPS URLs must use the default port")
        return _build_repo(*_split_path(parts.path))

    scp = _SCP_SSH_RE.match(text)
    if scp:
        if not _is_github_host(scp.group("host")):
            raise _inaccessible("only repositories on github.com are supported")
        return _build_repo(*_split_path(scp.group("path")))

    if "@" in text:
        raise _inaccessible(
            "GitHub repository reference must not embed credentials; "
            "the daemon host's gh session is used instead"
        )
    if text.startswith("/"):
        raise _inaccessible("expected <owner>/<repo>, not an absolute path")
    short = _SHORTHAND_RE.match(text)
    if not short:
        raise _inaccessible("expected <owner>/<repo>")
    host = short.group("host")
    if host and not _is_github_host(host):
        raise _inaccessible("only repositories on github.com are supported")
    return _build_repo(*_split_path(short.group("path")))


# --------------------------------------------------------------------------
# gh client results
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GitHubAuthStatus:
    """What ``get_github_auth_status`` reports (design §5.2) — never a token."""

    installed: bool
    authenticated: bool
    login: str | None
    hostname: str = GITHUB_HOST

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class GitHubOwner:
    """An account repositories can be created under."""

    login: str
    kind: Literal["user", "organization"]

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class GitHubRepository:
    """One search result: identity, visibility, clone URLs and default branch."""

    owner: str
    name: str
    visibility: str
    default_branch: str | None
    html_url: str
    clone_https: str
    clone_ssh: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["full_name"] = self.full_name
        return d


@dataclass(frozen=True, slots=True)
class RepositorySearchPage:
    repositories: list[GitHubRepository]
    next_cursor: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "repositories": [r.to_dict() for r in self.repositories],
            "next_cursor": self.next_cursor,
        }


@dataclass(frozen=True, slots=True)
class _Completed:
    returncode: int
    stdout: str
    stderr: str


# ``gh`` exits 4 when a command needs authentication it does not have.
_GH_EXIT_AUTH_REQUIRED = 4
_AUTH_HINTS = ("not logged in", "gh auth login", "authentication required")
_CONFLICT_HINTS = ("already exists",)

_OWNERS_QUERY = """
query {
  viewer {
    login
    organizations(first: 100) {
      nodes { login viewerCanCreateRepositories }
    }
  }
}
""".strip()

_SEARCH_QUERY = """
query($q: String!, $first: Int!, $after: String) {
  search(query: $q, type: REPOSITORY, first: $first, after: $after) {
    pageInfo { endCursor hasNextPage }
    nodes {
      ... on Repository {
        nameWithOwner
        name
        owner { login }
        visibility
        url
        defaultBranchRef { name }
      }
    }
  }
}
""".strip()


class GhClient:
    """Async wrapper around the ``gh`` executable (design §5.2, §7).

    ``executable`` is a bare name resolved on ``PATH`` at call time (so a
    freshly installed ``gh`` is picked up without a restart) or an explicit
    path.  ``env`` replaces the inherited environment; prompt-disabling
    variables are always layered on top.  ``timeout`` bounds each invocation
    in seconds; a hung ``gh`` (an interactive prompt that slipped through) is
    killed and reported as ``github_cli_failed``.
    """

    #: Applied on top of the environment for every invocation.
    NO_PROMPT_ENV: Mapping[str, str] = {
        "GH_PROMPT_DISABLED": "1",
        "GH_NO_UPDATE_NOTIFIER": "1",
        "GH_PAGER": "cat",
        "PAGER": "cat",
        "NO_COLOR": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/false",
    }

    def __init__(
        self,
        executable: str = "gh",
        *,
        hostname: str = GITHUB_HOST,
        env: Mapping[str, str] | None = None,
        timeout: float = 30.0,
        create_timeout: float = 60.0,
    ) -> None:
        self.executable = executable
        self.hostname = hostname
        self._env = dict(env) if env is not None else None
        self.timeout = timeout
        self.create_timeout = create_timeout

    # -- plumbing ---------------------------------------------------------

    def subprocess_env(self) -> dict[str, str]:
        base = dict(os.environ if self._env is None else self._env)
        base.update(self.NO_PROMPT_ENV)
        return base

    def resolve_executable(self) -> str | None:
        """Absolute path of ``gh`` or ``None`` when it is not installed."""
        env = self.subprocess_env()
        if os.sep in self.executable:
            return self.executable if os.access(self.executable, os.X_OK) else None
        return shutil.which(self.executable, path=env.get("PATH"))

    async def _run(self, args: list[str], *, timeout: float | None = None) -> _Completed:
        """Run ``gh <args>``; raise ``github_cli_missing`` when it is absent.

        Never raises for a non-zero exit — callers classify that — but the
        returned ``stderr`` / ``stdout`` are already scrubbed.
        """
        path = self.resolve_executable()
        if path is None:
            raise GitHubError(
                GitHubErrorCode.CLI_MISSING,
                "the GitHub CLI (gh) is not installed on the daemon host; "
                "install it and run 'gh auth login'",
            )
        effective_timeout = self.timeout if timeout is None else timeout
        try:
            proc = await asyncio.create_subprocess_exec(
                path,
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.subprocess_env(),
            )
        except FileNotFoundError:
            raise GitHubError(
                GitHubErrorCode.CLI_MISSING,
                "the GitHub CLI (gh) is not installed on the daemon host; "
                "install it and run 'gh auth login'",
            ) from None
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=effective_timeout)
        except TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            logger.warning("gh %s timed out after %.1fs", self._describe(args), effective_timeout)
            raise GitHubError(
                GitHubErrorCode.CLI_FAILED,
                f"gh {self._describe(args)} timed out after {effective_timeout:.0f}s "
                "(possible interactive prompt)",
            ) from None
        completed = _Completed(
            returncode=proc.returncode if proc.returncode is not None else -1,
            stdout=scrub_secrets(out.decode("utf-8", "replace")),
            stderr=scrub_secrets(err.decode("utf-8", "replace")),
        )
        if completed.returncode != 0:
            logger.warning(
                "gh %s exited %d: %s",
                self._describe(args),
                completed.returncode,
                completed.stderr.strip(),
            )
        return completed

    @staticmethod
    def _describe(args: list[str]) -> str:
        """The sub-command (first two words) for log lines — never the values."""
        return " ".join(args[:2])

    @staticmethod
    def _looks_unauthenticated(completed: _Completed) -> bool:
        if completed.returncode == _GH_EXIT_AUTH_REQUIRED:
            return True
        lowered = completed.stderr.lower()
        return any(hint in lowered for hint in _AUTH_HINTS)

    def _fail(self, args: list[str], completed: _Completed) -> GitHubError:
        """Classify a non-zero exit into the structured error for it."""
        if self._looks_unauthenticated(completed):
            return GitHubError(
                GitHubErrorCode.AUTH_REQUIRED,
                "the daemon host's GitHub CLI is not logged in; run 'gh auth login' there",
                details=completed.stderr.strip() or None,
            )
        return GitHubError(
            GitHubErrorCode.CLI_FAILED,
            f"gh {self._describe(args)} exited {completed.returncode}",
            details=completed.stderr.strip() or completed.stdout.strip() or None,
        )

    async def _graphql(self, query: str, variables: dict[str, str | int]) -> dict[str, Any]:
        """``gh api graphql`` with every variable passed as its own argument."""
        args = ["api", "graphql", "--hostname", self.hostname, "-f", f"query={query}"]
        for key, value in variables.items():
            # ``-F`` coerces numerals to integers but also reads ``@path`` as a
            # file and expands ``{owner}``; user-controlled strings must go
            # through ``-f`` (raw) so ``gh`` never interprets them.
            flag = "-F" if isinstance(value, int) else "-f"
            args.extend([flag, f"{key}={value}"])
        completed = await self._run(args)
        if completed.returncode != 0:
            raise self._fail(args, completed)
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            raise GitHubError(
                GitHubErrorCode.CLI_FAILED,
                "gh api graphql returned malformed JSON",
                details=completed.stdout.strip()[:500] or None,
            ) from None
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
            raise GitHubError(
                GitHubErrorCode.CLI_FAILED,
                "gh api graphql returned no data",
                details=json.dumps(payload.get("errors")) if isinstance(payload, dict) else None,
            )
        return payload["data"]

    # -- design §5.2 commands ---------------------------------------------

    async def auth_status(self) -> GitHubAuthStatus:
        """Installed? Authenticated? Which login? — never the token.

        Not-installed and not-logged-in are *states* here, not errors: the
        dashboard renders them as setup guidance.
        """
        if self.resolve_executable() is None:
            return GitHubAuthStatus(
                installed=False, authenticated=False, login=None, hostname=self.hostname
            )
        status = await self._run(["auth", "status", "--hostname", self.hostname])
        if status.returncode != 0:
            return GitHubAuthStatus(
                installed=True, authenticated=False, login=None, hostname=self.hostname
            )
        who = await self._run(["api", "user", "--hostname", self.hostname, "--jq", ".login"])
        if who.returncode != 0:
            if self._looks_unauthenticated(who):
                return GitHubAuthStatus(
                    installed=True, authenticated=False, login=None, hostname=self.hostname
                )
            raise self._fail(["api", "user"], who)
        login = who.stdout.strip().splitlines()[-1].strip() if who.stdout.strip() else None
        return GitHubAuthStatus(
            installed=True, authenticated=True, login=login or None, hostname=self.hostname
        )

    async def list_owners(self) -> list[GitHubOwner]:
        """The authenticated user first, then organisations that allow creation."""
        data = await self._graphql(_OWNERS_QUERY, {})
        viewer = data.get("viewer") or {}
        owners: list[GitHubOwner] = []
        login = viewer.get("login")
        if isinstance(login, str) and login:
            owners.append(GitHubOwner(login=login, kind="user"))
        for node in (viewer.get("organizations") or {}).get("nodes") or []:
            if not isinstance(node, dict) or not node.get("viewerCanCreateRepositories"):
                continue
            org = node.get("login")
            if isinstance(org, str) and _OWNER_RE.match(org):
                owners.append(GitHubOwner(login=org, kind="organization"))
        return owners

    async def search_repositories(
        self,
        query: str,
        cursor: str | None = None,
        limit: int = DEFAULT_SEARCH_LIMIT,
    ) -> RepositorySearchPage:
        """One page of repositories visible to the ``gh`` session.

        ``query`` is GitHub search syntax (bounded to
        :data:`MAX_QUERY_LENGTH`); ``cursor`` is an opaque value from a
        previous page's ``next_cursor``; ``limit`` is clamped to
        ``1..MAX_SEARCH_LIMIT``.  Rows whose identity does not validate are
        dropped, so every returned clone URL is one this module built.
        """
        if not isinstance(query, str) or not query.strip():
            raise GitHubError(GitHubErrorCode.INVALID_INPUT, "search query is empty")
        query = query.strip()
        if len(query) > MAX_QUERY_LENGTH:
            raise GitHubError(
                GitHubErrorCode.INVALID_INPUT,
                f"search query is longer than {MAX_QUERY_LENGTH} characters",
            )
        try:
            first = max(1, min(MAX_SEARCH_LIMIT, int(limit)))
        except (TypeError, ValueError):
            raise GitHubError(GitHubErrorCode.INVALID_INPUT, "limit must be an integer") from None
        variables: dict[str, str | int] = {"q": query, "first": first}
        if cursor:
            if not isinstance(cursor, str) or len(cursor) > 1024:
                raise GitHubError(GitHubErrorCode.INVALID_INPUT, "cursor is not valid")
            variables["after"] = cursor

        data = await self._graphql(_SEARCH_QUERY, variables)
        search = data.get("search") or {}
        page_info = search.get("pageInfo") or {}
        repositories: list[GitHubRepository] = []
        for node in search.get("nodes") or []:
            repo = self._repository_from_node(node)
            if repo is not None:
                repositories.append(repo)
        next_cursor = page_info.get("endCursor") if page_info.get("hasNextPage") else None
        return RepositorySearchPage(
            repositories=repositories,
            next_cursor=next_cursor if isinstance(next_cursor, str) and next_cursor else None,
        )

    @staticmethod
    def _repository_from_node(node: Any) -> GitHubRepository | None:
        if not isinstance(node, dict):
            return None
        owner = (node.get("owner") or {}).get("login")
        name = node.get("name")
        if not isinstance(owner, str) or not isinstance(name, str):
            return None
        try:
            identity = _build_repo(owner, name)
        except GitHubError:
            logger.debug("dropping search result with invalid identity")
            return None
        branch_ref = node.get("defaultBranchRef") or {}
        branch = branch_ref.get("name") if isinstance(branch_ref, dict) else None
        visibility = str(node.get("visibility") or "").lower() or "private"
        return GitHubRepository(
            owner=identity.owner,
            name=identity.name,
            visibility=visibility,
            default_branch=branch if isinstance(branch, str) and branch else None,
            html_url=identity.html_url,
            clone_https=identity.clone_https,
            clone_ssh=identity.clone_ssh,
        )

    async def create_repository(self, owner: str, name: str, visibility: str) -> GitHubRepo:
        """``gh repo create <owner>/<name> --private|--public``; returns the identity.

        ``owner`` / ``name`` are validated before ``gh`` runs, so the only
        thing ever passed on is a canonical ``owner/name`` argument.  A name
        that already exists is ``github_repository_conflict``.
        """
        if not isinstance(visibility, str) or visibility not in _VISIBILITIES:
            raise GitHubError(
                GitHubErrorCode.INVALID_INPUT, "visibility must be 'private' or 'public'"
            )
        if not isinstance(owner, str) or not isinstance(name, str):
            raise _inaccessible("owner and repository name must be strings")
        identity = _build_repo(owner.strip(), name.strip())
        args = ["repo", "create", identity.full_name, f"--{visibility}"]
        completed = await self._run(args, timeout=self.create_timeout)
        if completed.returncode != 0:
            lowered = completed.stderr.lower()
            if any(hint in lowered for hint in _CONFLICT_HINTS) and not (
                self._looks_unauthenticated(completed)
            ):
                raise GitHubError(
                    GitHubErrorCode.REPOSITORY_CONFLICT,
                    f"a repository named {identity.full_name} already exists",
                    details=completed.stderr.strip() or None,
                )
            raise self._fail(args, completed)
        logger.info("created GitHub repository %s (%s)", identity.full_name, visibility)
        return identity
