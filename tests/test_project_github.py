"""Tests for ``src.projects.github`` — GitHub identity normalisation and the ``gh`` wrapper.

Project-onboarding design §4.5 (URL normalisation), §5.2 (discovery commands)
and §7 (security). The client tests run against ``tests/fixtures/fake_gh/gh``,
a shell script staged on ``PATH``; nothing here touches the network or a real
account.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
from pathlib import Path

import pytest

from src.projects.github import (
    GhClient,
    GitHubAuthStatus,
    GitHubError,
    GitHubErrorCode,
    GitHubOwner,
    GitHubRepo,
    GitHubRepository,
    RepositorySearchPage,
    parse_github_repository,
    scrub_secrets,
)

FIXTURE = Path(__file__).parent / "fixtures" / "fake_gh" / "gh"

# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def fake_gh_dir(tmp_path: Path) -> Path:
    """A directory holding an executable copy of the fake ``gh``."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    target = bin_dir / "gh"
    shutil.copy(FIXTURE, target)
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir


@pytest.fixture
def argv_log(tmp_path: Path) -> Path:
    return tmp_path / "argv.log"


@pytest.fixture
def fake_gh(fake_gh_dir: Path, argv_log: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put the fake ``gh`` first on PATH, in the authenticated state."""
    monkeypatch.setenv("PATH", str(fake_gh_dir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("FAKE_GH_ARGV_LOG", str(argv_log))
    monkeypatch.delenv("FAKE_GH_STATE", raising=False)
    monkeypatch.delenv("FAKE_GH_FAIL_STDERR", raising=False)
    monkeypatch.delenv("FAKE_GH_SLEEP", raising=False)
    return fake_gh_dir


def invocations(argv_log: Path) -> list[list[str]]:
    """Every argv the fake ``gh`` received, one list per invocation."""
    if not argv_log.exists():
        return []
    out: list[list[str]] = []
    for record in argv_log.read_bytes().split(b"\x1e"):
        if not record:
            continue
        parts = record.split(b"\0")
        assert parts[-1] == b"", "every argument is NUL-terminated"
        out.append([a.decode() for a in parts[:-1]])
    return out


# --------------------------------------------------------------------------
# parse_github_repository
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "https://github.com/octo-org/my.repo",
        "https://github.com/octo-org/my.repo.git",
        "https://github.com/octo-org/my.repo/",
        "https://github.com/octo-org/my.repo.git/",
        "HTTPS://GitHub.com/octo-org/my.repo",
        "https://www.github.com/octo-org/my.repo",
        "git@github.com:octo-org/my.repo",
        "git@github.com:octo-org/my.repo.git",
        "ssh://git@github.com/octo-org/my.repo",
        "ssh://git@github.com/octo-org/my.repo.git",
        "ssh://git@github.com:22/octo-org/my.repo.git",
        "octo-org/my.repo",
        "octo-org/my.repo.git",
        "github.com/octo-org/my.repo",
        "  octo-org/my.repo  ",
    ],
)
def test_parse_accepts_every_documented_form(text: str) -> None:
    repo = parse_github_repository(text)
    assert isinstance(repo, GitHubRepo)
    assert (repo.owner, repo.name) == ("octo-org", "my.repo")
    assert repo.full_name == "octo-org/my.repo"
    assert repo.clone_https == "https://github.com/octo-org/my.repo.git"
    assert repo.clone_ssh == "git@github.com:octo-org/my.repo.git"
    assert repo.html_url == "https://github.com/octo-org/my.repo"


def test_parse_preserves_case_of_owner_and_name() -> None:
    repo = parse_github_repository("https://github.com/OctoOrg/MyRepo")
    assert repo.full_name == "OctoOrg/MyRepo"


def test_parse_to_dict_is_flat_and_serialisable() -> None:
    d = parse_github_repository("o/r").to_dict()
    assert d == {
        "owner": "o",
        "name": "r",
        "full_name": "o/r",
        "html_url": "https://github.com/o/r",
        "clone_https": "https://github.com/o/r.git",
        "clone_ssh": "git@github.com:o/r.git",
    }


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "https://gitlab.com/octo-org/my.repo",
        "https://github.example.com/octo-org/my.repo",
        "https://notgithub.com/octo-org/my.repo",
        "https://github.com.evil.example/octo-org/my.repo",
        "git@gitlab.com:octo-org/my.repo.git",
        "ssh://git@bitbucket.org/octo-org/my.repo",
        "http://github.com/octo-org/my.repo",
        "ftp://github.com/octo-org/my.repo",
        "ssh://alice@github.com/octo-org/my.repo",
        "ssh://git@github.com:2222/octo-org/my.repo",
        "https://github.com/octo-org",
        "https://github.com/",
        "https://github.com/octo-org/my.repo/tree/main",
        "https://github.com/octo-org/my.repo?tab=readme",
        "https://github.com/octo-org/my.repo#readme",
        "octo-org",
        "octo-org/my.repo/extra",
        "/octo-org/my.repo",
        "octo-org//my.repo",
        "-octo/my.repo",
        "octo-/my.repo",
        "octo_org/my.repo",
        "octo org/my.repo",
        "octo-org/my repo",
        "octo-org/my;repo",
        "octo-org/.",
        "octo-org/..",
        "octo-org/.git",
        "octo-org/",
        "a" * 40 + "/repo",
        "octo-org/" + "r" * 101,
        "octo-org/my.repo\x00",
        "octo-org/my\nrepo",
        "$(id)/repo",
        "octo-org/repo`id`",
        "octo-org/my.repo;rm -rf /",
    ],
)
def test_parse_rejects_non_github_and_invalid_input(text: str) -> None:
    with pytest.raises(GitHubError) as excinfo:
        parse_github_repository(text)
    err = excinfo.value
    assert err.code is GitHubErrorCode.REPOSITORY_INACCESSIBLE
    assert err.to_dict()["code"] == "github_repository_inaccessible"
    assert err.message


@pytest.mark.parametrize(
    "text",
    [
        "https://alice:ghp_SECRETSECRETSECRET1234567890@github.com/octo-org/my.repo",
        "https://ghp_SECRETSECRETSECRET1234567890@github.com/octo-org/my.repo",
        "https://x-access-token:ghs_SECRETSECRETSECRET1234567890@github.com/o/r.git",
        "ssh://git:hunter2SECRET@github.com/octo-org/my.repo",
    ],
)
def test_parse_rejects_embedded_credentials_and_never_echoes_them(text: str) -> None:
    with pytest.raises(GitHubError) as excinfo:
        parse_github_repository(text)
    err = excinfo.value
    assert err.code is GitHubErrorCode.REPOSITORY_INACCESSIBLE
    for leak in ("SECRET", "hunter2"):
        assert leak not in str(err)
        assert leak not in err.message
        assert leak not in repr(err.to_dict())


def test_parse_rejects_non_string_input() -> None:
    with pytest.raises(GitHubError):
        parse_github_repository(None)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# scrub_secrets
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "leak"),
    [
        ("fatal: https://alice:hunter2@github.com/o/r.git denied", "hunter2"),
        ("https://ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ab@github.com/o/r", "ABCDEFGHIJ"),
        ("token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ab expired", "ABCDEFGHIJ"),
        ("gho_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ab", "ABCDEFGHIJ"),
        ("ghu_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ab", "ABCDEFGHIJ"),
        ("ghs_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ab", "ABCDEFGHIJ"),
        ("ghr_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ab", "ABCDEFGHIJ"),
        ("github_pat_11ABCDEFG0abcdefghijklmnopqrstuvwxyz_ABCDEFGHIJ", "abcdefghij"),
        ("Authorization: token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ab", "ABCDEFGHIJ"),
        ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.SECRETPAYLOAD.sig", "SECRETPAYLOAD"),
        ("Authorization: basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNz"),
        ("GH_TOKEN=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ab", "ABCDEFGHIJ"),
        ("GITHUB_TOKEN=plainoldsecretvalue", "plainoldsecretvalue"),
        ("  - Token: gho_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ab", "ABCDEFGHIJ"),
        ("  - Token: ****ab12", "ab12"),
        ("x-access-token:ghs_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ab@github.com", "ABCDEFGHIJ"),
    ],
)
def test_scrub_secrets_removes_credentials(text: str, leak: str) -> None:
    scrubbed = scrub_secrets(text)
    assert leak not in scrubbed
    assert "***" in scrubbed


def test_scrub_secrets_keeps_the_host_of_a_credentialed_url() -> None:
    out = scrub_secrets("clone https://alice:hunter2@github.com/o/r.git failed")
    assert out == "clone https://***@github.com/o/r.git failed"


def test_scrub_secrets_leaves_plain_text_alone() -> None:
    text = "fatal: repository 'https://github.com/o/r.git' not found"
    assert scrub_secrets(text) == text
    assert scrub_secrets("") == ""


def test_scrub_secrets_tolerates_non_string() -> None:
    assert scrub_secrets(None) == ""  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# GitHubError
# --------------------------------------------------------------------------


def test_github_error_scrubs_message_and_details() -> None:
    err = GitHubError(
        GitHubErrorCode.CLI_FAILED,
        "gh failed: https://alice:hunter2@github.com/o/r",
        details="stderr: GH_TOKEN=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ab",
    )
    assert "hunter2" not in str(err)
    assert "hunter2" not in err.message
    assert "ABCDEFGHIJ" not in (err.details or "")
    d = err.to_dict()
    assert d["code"] == "github_cli_failed"
    assert "hunter2" not in repr(d)
    assert isinstance(err, ValueError)


def test_github_error_codes_match_design_section_8() -> None:
    assert {c.value for c in GitHubErrorCode} >= {
        "github_cli_missing",
        "github_auth_required",
        "github_repository_inaccessible",
        "github_repository_conflict",
    }


# --------------------------------------------------------------------------
# GhClient.auth_status
# --------------------------------------------------------------------------


async def test_auth_status_installed_and_authenticated(fake_gh: Path, argv_log: Path) -> None:
    status = await GhClient().auth_status()
    assert status == GitHubAuthStatus(
        installed=True, authenticated=True, login="octocat", hostname="github.com"
    )
    d = status.to_dict()
    assert d == {
        "installed": True,
        "authenticated": True,
        "login": "octocat",
        "hostname": "github.com",
    }
    calls = invocations(argv_log)
    assert calls[0][:2] == ["auth", "status"]
    assert all(call[:2] != ["auth", "token"] for call in calls)


async def test_auth_status_never_carries_a_token(fake_gh: Path) -> None:
    # The fake ``gh auth status`` prints a ``Token:`` line like the real one;
    # nothing the client returns may include it.
    status = await GhClient().auth_status()
    assert "gho_" not in repr(status)
    assert "gho_" not in repr(status.to_dict())


async def test_auth_status_not_installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    status = await GhClient().auth_status()
    assert status == GitHubAuthStatus(
        installed=False, authenticated=False, login=None, hostname="github.com"
    )


async def test_auth_status_not_authenticated(
    fake_gh: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_GH_STATE", "unauthed")
    status = await GhClient().auth_status()
    assert status == GitHubAuthStatus(
        installed=True, authenticated=False, login=None, hostname="github.com"
    )


async def test_explicit_executable_path_bypasses_path_lookup(
    fake_gh_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    status = await GhClient(executable=str(fake_gh_dir / "gh")).auth_status()
    assert status.installed and status.authenticated


# --------------------------------------------------------------------------
# GhClient.list_owners
# --------------------------------------------------------------------------


async def test_list_owners_returns_user_then_orgs_that_allow_creation(
    fake_gh: Path, argv_log: Path
) -> None:
    owners = await GhClient().list_owners()
    assert owners == [
        GitHubOwner(login="octocat", kind="user"),
        GitHubOwner(login="acme-corp", kind="organization"),
        GitHubOwner(login="beta-labs", kind="organization"),
    ]
    assert owners[0].to_dict() == {"login": "octocat", "kind": "user"}
    calls = invocations(argv_log)
    assert len(calls) == 1
    assert calls[0][:2] == ["api", "graphql"]


async def test_list_owners_requires_auth(fake_gh: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_GH_STATE", "unauthed")
    with pytest.raises(GitHubError) as excinfo:
        await GhClient().list_owners()
    assert excinfo.value.code is GitHubErrorCode.AUTH_REQUIRED
    assert "gh auth login" in excinfo.value.message


async def test_list_owners_requires_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    with pytest.raises(GitHubError) as excinfo:
        await GhClient().list_owners()
    assert excinfo.value.code is GitHubErrorCode.CLI_MISSING


# --------------------------------------------------------------------------
# GhClient.search_repositories
# --------------------------------------------------------------------------


async def test_search_first_page_returns_identity_visibility_urls_branch_and_cursor(
    fake_gh: Path, argv_log: Path
) -> None:
    page = await GhClient().search_repositories("widgets", limit=2)
    assert isinstance(page, RepositorySearchPage)
    assert page.next_cursor == "CURSOR-PAGE-2"
    assert [r.full_name for r in page.repositories] == ["acme-corp/widgets", "octocat/dotfiles"]
    first = page.repositories[0]
    assert first == GitHubRepository(
        owner="acme-corp",
        name="widgets",
        visibility="private",
        default_branch="main",
        html_url="https://github.com/acme-corp/widgets",
        clone_https="https://github.com/acme-corp/widgets.git",
        clone_ssh="git@github.com:acme-corp/widgets.git",
    )
    assert first.full_name == "acme-corp/widgets"
    assert page.repositories[1].visibility == "public"
    assert page.repositories[1].default_branch == "trunk"
    d = page.to_dict()
    assert d["next_cursor"] == "CURSOR-PAGE-2"
    assert d["repositories"][0]["full_name"] == "acme-corp/widgets"

    # The query and limit travel as GraphQL variables, each one argument.
    (call,) = invocations(argv_log)
    assert call[:2] == ["api", "graphql"]
    assert "q=widgets" in call
    assert "first=2" in call
    assert not any(a.startswith("after=") for a in call)


async def test_search_second_page_follows_cursor_and_ends(fake_gh: Path, argv_log: Path) -> None:
    page = await GhClient().search_repositories("widgets", cursor="CURSOR-PAGE-2", limit=5)
    assert page.next_cursor is None
    # The malformed ``evil/../escape`` row is dropped rather than returned.
    assert [r.full_name for r in page.repositories] == ["beta-labs/empty-repo"]
    assert page.repositories[0].default_branch is None
    (call,) = invocations(argv_log)
    assert "after=CURSOR-PAGE-2" in call


async def test_search_passes_hostile_query_as_a_single_argument(
    fake_gh: Path, argv_log: Path
) -> None:
    hostile = "widgets; rm -rf / $(id) `id` | cat"
    await GhClient().search_repositories(hostile)
    (call,) = invocations(argv_log)
    assert "q=" + hostile in call


@pytest.mark.parametrize("query", ["", "   ", "x" * 257])
async def test_search_rejects_unbounded_or_empty_query(fake_gh: Path, query: str) -> None:
    with pytest.raises(GitHubError) as excinfo:
        await GhClient().search_repositories(query)
    assert excinfo.value.code is GitHubErrorCode.INVALID_INPUT


async def test_search_clamps_limit(fake_gh: Path, argv_log: Path) -> None:
    await GhClient().search_repositories("a", limit=999)
    await GhClient().search_repositories("a", limit=0)
    calls = invocations(argv_log)
    assert "first=50" in calls[0]
    assert "first=1" in calls[1]


async def test_search_requires_auth(fake_gh: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_GH_STATE", "unauthed")
    with pytest.raises(GitHubError) as excinfo:
        await GhClient().search_repositories("widgets")
    assert excinfo.value.code is GitHubErrorCode.AUTH_REQUIRED


# --------------------------------------------------------------------------
# GhClient.create_repository
# --------------------------------------------------------------------------


async def test_create_repository_private(fake_gh: Path, argv_log: Path) -> None:
    created = await GhClient().create_repository("acme-corp", "widgets", "private")
    assert created == GitHubRepo(
        owner="acme-corp",
        name="widgets",
        clone_https="https://github.com/acme-corp/widgets.git",
        clone_ssh="git@github.com:acme-corp/widgets.git",
    )
    assert created.html_url == "https://github.com/acme-corp/widgets"
    (call,) = invocations(argv_log)
    assert call == ["repo", "create", "acme-corp/widgets", "--private"]


async def test_create_repository_public(fake_gh: Path, argv_log: Path) -> None:
    await GhClient().create_repository("octocat", "site", "public")
    (call,) = invocations(argv_log)
    assert call == ["repo", "create", "octocat/site", "--public"]


async def test_create_repository_conflict(fake_gh: Path) -> None:
    with pytest.raises(GitHubError) as excinfo:
        await GhClient().create_repository("acme-corp", "taken", "private")
    assert excinfo.value.code is GitHubErrorCode.REPOSITORY_CONFLICT


async def test_create_repository_other_failure_is_cli_failed(fake_gh: Path) -> None:
    with pytest.raises(GitHubError) as excinfo:
        await GhClient().create_repository("acme-corp", "forbidden", "private")
    assert excinfo.value.code is GitHubErrorCode.CLI_FAILED
    assert "Resource not accessible" in (excinfo.value.details or "")


@pytest.mark.parametrize("visibility", ["internal", "PRIVATE", "", "public;--x"])
async def test_create_repository_rejects_unknown_visibility(
    fake_gh: Path, argv_log: Path, visibility: str
) -> None:
    with pytest.raises(GitHubError) as excinfo:
        await GhClient().create_repository("acme-corp", "widgets", visibility)
    assert excinfo.value.code is GitHubErrorCode.INVALID_INPUT
    assert invocations(argv_log) == []


@pytest.mark.parametrize(("owner", "name"), [("acme corp", "w"), ("acme", "w;x"), ("", "w")])
async def test_create_repository_rejects_invalid_identity_before_running_gh(
    fake_gh: Path, argv_log: Path, owner: str, name: str
) -> None:
    with pytest.raises(GitHubError) as excinfo:
        await GhClient().create_repository(owner, name, "private")
    assert excinfo.value.code is GitHubErrorCode.REPOSITORY_INACCESSIBLE
    assert invocations(argv_log) == []


async def test_create_repository_requires_auth(
    fake_gh: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_GH_STATE", "unauthed")
    with pytest.raises(GitHubError) as excinfo:
        await GhClient().create_repository("acme-corp", "widgets", "private")
    assert excinfo.value.code is GitHubErrorCode.AUTH_REQUIRED


# --------------------------------------------------------------------------
# secret scrubbing across the subprocess boundary
# --------------------------------------------------------------------------

CREDENTIALED_STDERR = (
    "fatal: unable to access "
    "'https://alice:ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ab@github.com/acme/widgets.git': "
    "GH_TOKEN=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ab rejected"
)


async def test_credentialed_stderr_never_reaches_error_or_log(
    fake_gh: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("FAKE_GH_FAIL_STDERR", CREDENTIALED_STDERR)
    with (
        caplog.at_level(logging.DEBUG, logger="src.projects.github"),
        pytest.raises(GitHubError) as excinfo,
    ):
        await GhClient().list_owners()
    err = excinfo.value
    assert err.code is GitHubErrorCode.CLI_FAILED
    for surface in (str(err), err.message, err.details or "", repr(err.to_dict()), repr(err.args)):
        assert "ABCDEFGHIJ" not in surface
        assert "alice:" not in surface
    assert caplog.records, "the failure is logged"
    for record in caplog.records:
        assert "ABCDEFGHIJ" not in record.getMessage()
        assert "alice:" not in record.getMessage()
    # The scrubbed host is still there so the operator can tell what failed.
    assert "github.com/acme/widgets.git" in (err.details or "")


async def test_timeout_kills_gh_and_reports_cli_failed(
    fake_gh: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_GH_SLEEP", "5")
    with pytest.raises(GitHubError) as excinfo:
        await GhClient(timeout=0.2).list_owners()
    assert excinfo.value.code is GitHubErrorCode.CLI_FAILED
    assert "timed out" in excinfo.value.message


# --------------------------------------------------------------------------
# subprocess hygiene
# --------------------------------------------------------------------------


def test_module_never_uses_a_shell() -> None:
    source = (Path(__file__).parent.parent / "src" / "projects" / "github.py").read_text()
    assert "shell=True" not in source
    assert "create_subprocess_shell" not in source
    assert "os.system" not in source
    assert "subprocess.run" not in source
    assert "create_subprocess_exec" in source


async def test_subprocess_env_disables_prompts(
    fake_gh: Path, argv_log: Path, tmp_path: Path
) -> None:
    # The fake records nothing about env, so check the client's own view.
    env = GhClient().subprocess_env()
    assert env["GH_PROMPT_DISABLED"] == "1"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GH_NO_UPDATE_NOTIFIER"] == "1"
    assert env["NO_COLOR"] == "1"


async def test_search_query_travels_as_a_raw_field_so_gh_never_reads_a_file(
    fake_gh: Path, argv_log: Path
) -> None:
    # ``gh api -F`` treats ``@path`` as "read this file" and expands
    # ``{owner}``; only ``-f`` (raw) is safe for user-controlled values.
    query = "@/etc/passwd {owner}"
    await GhClient().search_repositories(query, cursor="@/etc/shadow")
    (call,) = invocations(argv_log)
    assert call[call.index("q=" + query) - 1] == "-f"
    assert call[call.index("after=@/etc/shadow") - 1] == "-f"
    assert call[call.index("first=20") - 1] == "-F"
