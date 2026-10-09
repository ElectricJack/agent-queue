from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from src.config import GitHubAppConfig
from src.git.github import GitHubAccess, GitHubClient, MAX_HEADER_BYTES
from src.git.github_app import AppTokenCandidate
from src.git.github_auth import GitHubAuth
from src.git.github_cli import GhRunner
from src.git.github_contracts import (
    GitHubAccessError,
    GitHubCredentialIdentity,
    GitHubRepositoryBinding,
)
from src.integration.ci import (
    AuthenticatedGitHubObserver,
    IntegrationCITrust,
    TrustedCIObservation,
)


REPOSITORY = GitHubRepositoryBinding(303, "acme/widgets")


def _rate_limit_access(tmp_path, identity, response, now):
    executable = tmp_path / "gh"
    executable.write_text(
        "#!/usr/bin/python3\nimport pathlib, sys\n"
        "path = pathlib.Path('calls')\n"
        "calls = int(path.read_text()) + 1 if path.exists() else 1\n"
        "path.write_text(str(calls))\n"
        f"sys.stdout.buffer.write({response.stdout!r} if calls == 1 else "
        "b'HTTP/2.0 200 OK\\r\\n\\r\\n{}')\n"
        f"sys.stderr.write({response.stderr!r} if calls == 1 else '')\n"
        f"sys.exit({response.returncode} if calls == 1 else 0)\n"
    )
    executable.chmod(0o700)
    app = identity.mode.value == "app"
    auth = GitHubAuth(
        GitHubAppConfig("Iv1.client", 101, 202, "/daemon/key.pem") if app else None,
        app_provider=FakeTokenProvider() if app else None,
        clock=lambda: 1_800_000_000.0,
    )
    runner = GhRunner(auth, executable=str(executable), env={}, cwd=tmp_path,
                      clock=lambda: now[0])
    return GitHubAccess(auth, runner)


@pytest.mark.parametrize("headers,body,delay", [
    ({"Retry-After": "7"}, {}, 7),
    ({"Retry-After": "90"}, {}, 90),
    ({"Retry-After": "Thu, 01 Jan 1970 00:18:20 GMT"}, {}, 100),
    ({"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1123"}, {}, 123),
    ({"Retry-After": "90", "X-RateLimit-Remaining": "0",
      "X-RateLimit-Reset": "1123"}, {}, 123),
    ({"Retry-After": "invalid", "X-RateLimit-Remaining": "0",
      "X-RateLimit-Reset": "1123"}, {}, 123),
    ({"Retry-After": "invalid", "X-RateLimit-Reset": "invalid"}, {}, 60),
    ({"X-RateLimit-Remaining": "4978", "X-RateLimit-Reset": "9000"},
     {"message": "You have exceeded a secondary rate limit"}, 60),
])
async def test_shared_backoff_honours_headers_and_blocks_all_request_paths(
    tmp_path, credential_identity, headers, body, delay,
):
    now = [1000.0]
    access = _rate_limit_access(
        tmp_path, credential_identity, _response(403, body, headers=headers, returncode=1), now,
    )
    client = GitHubClient(REPOSITORY, access=access, clock=lambda: now[0])
    other = GitHubRepositoryBinding(404, "acme/other")
    other_client = GitHubClient(other, access=access, clock=lambda: now[0])
    with pytest.raises(GitHubAccessError) as first:
        # The plain binding-read path must retain headers internally too.
        await access.run_read(["api", "repos/acme/widgets"], repository=REPOSITORY)
    assert first.value.retry_at == 1000 + delay
    assert first.value.category == "rate_limited"
    assert access.runner.activity()["api_calls_per_minute"] == {"GET repos/acme/widgets": 1}

    trust = IntegrationCITrust(
        canonical_repository_id="repo", repository_id=303, full_name="acme/widgets",
        producer_id="github-actions", required_checks={"version": "v1", "names": ["Tests"]},
    )
    paths = [
        # Train ref snapshot, CI observer, review poll and preflight reads.
        lambda: client.exact_head_ref("main"),
        lambda: AuthenticatedGitHubObserver(client).observe(trust, "a" * 40),
        lambda: client.paged_list("/repositories/303/pulls?state=open&per_page=100"),
        lambda: client.request_json("GET", "/repos/acme/widgets"),
        lambda: other_client.request_json("GET", "/repos/acme/other"),
        lambda: other_client.request_json("POST", "/repos/acme/other/issues",
                                          json_body={"title": "test"}),
        lambda: access.bind_repository("acme/other"),
        lambda: access.run_read(["pr", "view", "7", "--json", "headRefOid"],
                                repository=REPOSITORY),
        lambda: access.run_write(["pr", "close", "7"], repository=REPOSITORY),
        lambda: access.runner.run(["api", "user"], hostname="github.com"),
        lambda: GitHubClient(REPOSITORY, runner=access.runner).request_json(
            "GET", "/repos/acme/widgets"),
    ]
    now[0] += delay - 1
    for call in paths:
        with pytest.raises(GitHubAccessError) as blocked:
            await call()
        assert blocked.value.category == "rate_limited"
        assert blocked.value.retry_at == 1000 + delay
    assert (tmp_path / "calls").read_text() == "1"

    now[0] += 1
    assert await other_client.request_json("GET", "/repos/acme/other") == {}
    assert (tmp_path / "calls").read_text() == "2"
    activity = access.runner.activity()
    assert activity["retry_at"] is None
    expected_counts = {"GET repos/acme/other": 1}
    if delay < 60:
        expected_counts["GET repos/acme/widgets"] = 1
    assert activity["api_calls_per_minute"] == expected_counts


@pytest.mark.parametrize("category", ["permission", "rate_limited"])
async def test_permission_403_does_not_backoff_but_unframed_secondary_limit_does(
    tmp_path, credential_identity, category,
):
    now = [1000.0]
    diagnostic = "Forbidden (HTTP 403)" if category == "permission" else (
        "You have exceeded a secondary rate limit (HTTP 403)"
    )
    access = _rate_limit_access(tmp_path, credential_identity,
                              FakeResult(1, b"", diagnostic), now)
    with pytest.raises(GitHubAccessError) as caught:
        await access.run_read(["pr", "view", "7", "--json", "headRefOid"], repository=REPOSITORY)
    assert caught.value.category == category
    assert access.runner.activity()["retry_at"] == (1060 if category == "rate_limited" else None)
    if category == "rate_limited":
        with pytest.raises(GitHubAccessError, match="backing off"):
            await access.run_read(["api", "repos/acme/widgets"], repository=REPOSITORY)
        now[0] = 1060
    await access.run_read(["api", "repos/acme/widgets"], repository=REPOSITORY)
    assert (tmp_path / "calls").read_text() == "2"


async def test_framed_permission_403_with_primary_quota_headers_does_not_backoff(
    tmp_path, credential_identity,
):
    now = [1000.0]
    access = _rate_limit_access(tmp_path, credential_identity,
                              _response(403, {"message": "Resource not accessible by integration"},
                                        headers={"X-RateLimit-Remaining": "4978",
                                                 "X-RateLimit-Reset": "9000"}, returncode=1), now)
    client = GitHubClient(REPOSITORY, access=access)
    with pytest.raises(GitHubAccessError) as caught:
        await client.request_json("GET", "/repos/acme/widgets")
    assert caught.value.category == "permission"
    assert access.runner.activity()["retry_at"] is None
    assert await client.request_json("GET", "/repos/acme/widgets") == {}
    assert access.runner.activity()["api_calls_per_minute"] == {"GET repos/acme/widgets": 2}


async def test_endpoint_counter_overflow_keeps_api_and_cli_counts_separate(tmp_path, monkeypatch):
    monkeypatch.setattr("src.git.github_cli.MAX_CALL_ENDPOINTS", 4)
    access = _rate_limit_access(tmp_path, GitHubCredentialIdentity.existing_login(),
                              _response(200, {}), [1000.0])
    runner = access.runner
    for endpoint in ("repos/acme/widgets", "user", "repos/acme/widgets/issues",
                     "repos/acme/widgets/pulls"):
        await runner.run(["api", endpoint], repository=REPOSITORY)
    await runner.run(["pr", "view", "7"], repository=REPOSITORY)
    activity = runner.activity()
    assert activity["api_calls_per_minute"] == {
        "GET repos/acme/widgets": 1, "GET user": 1, "other endpoints": 2,
    }
    assert activity["gh_commands_per_minute"] == {"gh other commands": 1}


@pytest.mark.parametrize("control_only", [True, False])
async def test_integration_status_exposes_rolling_github_endpoint_counts(tmp_path, control_only):
    from src.commands.integration_commands import IntegrationCommandsMixin
    from src.git.github_cli import ExistingLoginCredentials

    now = [1000.0]
    executable = tmp_path / "gh"
    executable.write_text("#!/usr/bin/python3\nprint('HTTP/2.0 200 OK\\r\\n\\r\\n{}')\n")
    executable.chmod(0o700)
    runner = GhRunner(ExistingLoginCredentials(), executable=str(executable), env={},
                      cwd=tmp_path, clock=lambda: now[0])
    for endpoint in ("repos/acme/widgets", "repos/acme/widgets?per_page=100", "user"):
        await runner.run(["api", endpoint], repository=REPOSITORY)
    handler = IntegrationCommandsMixin()
    handler.db = SimpleNamespace()
    handler.config = SimpleNamespace(integration=SimpleNamespace(git_first="active"))
    handler.orchestrator = SimpleNamespace(github_runner=runner)
    method = "control_status" if control_only else "status"
    from unittest.mock import patch

    with patch(f"src.integration.status.IntegrationStatusService.{method}",
               new=AsyncMock(return_value={"project_id": "p"})):
        result = await handler._cmd_integration_status({"project_id": "p", "control_only": control_only})
    assert result["github"] == {
        "scope": "daemon", "window_seconds": 60, "retry_at": None,
        "api_calls_per_minute": {"GET repos/acme/widgets": 2, "GET user": 1},
        "gh_commands_per_minute": {},
    }
    now[0] = 1060
    assert runner.activity()["api_calls_per_minute"] == {}


@dataclass(frozen=True)
class FakeResult:
    returncode: int
    stdout: bytes
    stderr: str = ""


@dataclass
class FakeRunner:
    credential_identity: GitHubCredentialIdentity
    results: list[FakeResult]
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def run(self, args, **kwargs):
        self.calls.append({"args": list(args), **kwargs})
        result = self.results.pop(0)
        if result.returncode and kwargs.get("check", True):
            category = "credentials" if result.returncode == 4 else "transient"
            raise GitHubAccessError(category, "GitHub CLI request failed")
        return result


class FakeTokenProvider:
    credential_identity = GitHubCredentialIdentity.app(101, 202)

    def __init__(self) -> None:
        self.calls = 0

    async def mint(self, repository):
        self.calls += 1
        return AppTokenCandidate(
            identity=self.credential_identity,
            repository=repository,
            token=f"installation-secret-{self.calls}",
            expires_at=1_800_003_600.0,
        )

    async def mint_for_repository(self, full_name):  # pragma: no cover - not used here
        raise AssertionError(full_name)


def _response(
    status: int,
    body: bytes | str | object = b"",
    *,
    headers: dict[str, str] | None = None,
    returncode: int = 0,
    stderr: str = "",
) -> FakeResult:
    if not isinstance(body, (bytes, str)):
        body = json.dumps(body, separators=(",", ":"))
    if isinstance(body, str):
        body = body.encode()
    reason = {200: "OK", 201: "Created", 204: "No Content"}.get(status, "Failure")
    header_lines = [f"HTTP/2.0 {status} {reason}"]
    header_lines.extend(f"{name}: {value}" for name, value in (headers or {}).items())
    framed = "\r\n".join(header_lines).encode() + b"\r\n\r\n" + body
    return FakeResult(returncode, framed, stderr)


def _audit_pull(
    *,
    key: str,
    head_sha: str = "a" * 40,
    head_branch: str = "aq/integration/batch",
    base_branch: str = "main",
    body: str | None = None,
    state: str = "open",
) -> dict[str, Any]:
    return {
        "html_url": "https://github.com/acme/widgets/pull/7",
        "number": 7,
        "state": state,
        "body": body or f"<!-- aq-integration-audit:{key} -->",
        "head": {
            "sha": head_sha,
            "ref": head_branch,
            "repo": {"id": 303, "full_name": "acme/widgets"},
        },
        "base": {
            "ref": base_branch,
            "repo": {"id": 303, "full_name": "acme/widgets"},
        },
    }


@pytest.fixture(params=["app", "existing_login"])
def credential_identity(request) -> GitHubCredentialIdentity:
    if request.param == "app":
        return GitHubCredentialIdentity.app(101, 202)
    return GitHubCredentialIdentity.existing_login()


@pytest.mark.asyncio
async def test_shared_request_suite_uses_identical_gh_api_contract(credential_identity):
    runner = FakeRunner(credential_identity, [_response(201, {"id": 901})])
    client = GitHubClient(REPOSITORY, runner=runner)

    result = await client.request_json(
        "POST",
        "/repos/acme/widgets/check-runs",
        json_body={"name": "required", "head_sha": "a" * 40},
        expected_statuses={201},
    )

    assert result == {"id": 901}
    assert client.credential_identity == credential_identity
    call = runner.calls[0]
    assert call["repository"] == REPOSITORY
    assert call["check"] is False
    assert call["args"] == [
        "api",
        "--include",
        "--method",
        "POST",
        "--header",
        "Accept: application/vnd.github+json",
        "--header",
        "X-GitHub-Api-Version: 2022-11-28",
        "repos/acme/widgets/check-runs",
        "--input",
        "-",
    ]
    assert json.loads(call["stdin"]) == {
        "name": "required",
        "head_sha": "a" * 40,
    }


@pytest.mark.asyncio
async def test_expected_empty_response_preserves_status_and_decodes_as_empty_object(
    credential_identity,
):
    runner = FakeRunner(credential_identity, [_response(204)])
    client = GitHubClient(REPOSITORY, runner=runner)

    response = await client.request(
        "DELETE",
        "/repositories/303/actions/caches/7",
        expected_statuses={204},
    )

    assert response.status == 204
    assert response.body == b""

    runner.results.append(_response(204))
    assert (
        await client.request_json(
            "DELETE",
            "/repositories/303/actions/caches/7",
            expected_statuses={204},
        )
        == {}
    )


@pytest.mark.asyncio
async def test_rate_limit_headers_produce_bounded_structured_retry_time(credential_identity):
    runner = FakeRunner(
        credential_identity,
        [
            _response(
                403,
                {"message": "secret diagnostic must not escape"},
                headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1700000123"},
                returncode=1,
            )
        ],
    )
    client = GitHubClient(REPOSITORY, runner=runner, clock=lambda: 1_700_000_000.0)

    with pytest.raises(GitHubAccessError) as caught:
        await client.request_json("GET", "/repositories/303")

    assert caught.value.category == "rate_limited"
    assert caught.value.retry_at == 1_700_000_123.0
    assert caught.value.http_status == 403
    assert str(caught.value) == "GitHub request was rate limited (rate_limited, HTTP 403)"
    assert "secret diagnostic" not in str(caught.value)


@pytest.mark.parametrize(
    ("status", "category"),
    [
        (401, "credentials"), (403, "permission"), (404, "not_found_or_hidden"),
        (409, "conflict_or_invalid"), (422, "conflict_or_invalid"),
        (429, "rate_limited"), (503, "transient"),
    ],
)
async def test_framed_http_failure_retains_safe_category_and_status(
    credential_identity, status, category
):
    runner = FakeRunner(
        credential_identity,
        [_response(status, {"message": "raw-private-diagnostic ghp_private"}, returncode=1)],
    )
    client = GitHubClient(REPOSITORY, runner=runner)

    with pytest.raises(GitHubAccessError) as caught:
        await client.request_json("POST", "/repositories/303/issues", json_body={"title": "test"})

    assert caught.value.category == category
    assert caught.value.http_status == status
    assert f"({category}, HTTP {status})" in str(caught.value)
    assert "raw-private-diagnostic" not in repr(caught.value)
    assert "ghp_private" not in repr(caught.value)


@pytest.mark.parametrize("composed_access", [True, False])
async def test_api_warning_uses_validated_category_and_ignores_expected_status(
    credential_identity, composed_access, tmp_path, caplog
):
    executable = tmp_path / "gh"
    executable.write_text(
        "#!/usr/bin/python3\nimport os, sys\n"
        "status = 404 if sys.argv[-1].endswith('/absent') else 403\n"
        "sys.stdout.write(f'HTTP/2.0 {status} Failure\\r\\n'"
        " + 'X-RateLimit-Remaining: 0\\r\\n\\r\\n'"
        " + '{\"message\":\"raw-private-diagnostic\"}')\n"
        "sys.stderr.write('Authorization: Bearer opaque-secret\\n'"
        " + os.environ.get('GH_TOKEN', '') + '\\nhttps://user:password@github.com')\n"
        "sys.exit(1)\n"
    )
    executable.chmod(0o700)
    app = credential_identity.mode.value == "app"
    auth = GitHubAuth(
        GitHubAppConfig("Iv1.client", 101, 202, "/daemon/key.pem") if app else None,
        app_provider=FakeTokenProvider() if app else None,
        clock=lambda: 1_800_000_000.0,
    )
    runner = GhRunner(auth, executable=str(executable), env={}, cwd=tmp_path)
    client = (
        GitHubClient(REPOSITORY, access=GitHubAccess(auth, runner)) if composed_access
        else GitHubClient(REPOSITORY, runner=runner)
    )
    caplog.set_level(logging.WARNING, logger="src.git.github_cli")

    response = await client.request(
        "GET", "/repositories/303/git/ref/heads/absent", expected_statuses={404}
    )
    assert response.status == 404
    assert caplog.records == []

    for _ in range(2):
        with pytest.raises(GitHubAccessError) as caught:
            await client.request_json("GET", "/repositories/303")
        assert caught.value.category == "rate_limited"
        assert caught.value.http_status == 403
    assert len(caplog.records) == 1
    assert "rate_limited, HTTP 403" in caplog.text
    assert f"credential_mode={credential_identity.mode.value} repository=acme/widgets" in caplog.text
    for private_text in (
        "raw-private-diagnostic", "opaque-secret", "installation-secret", "password", "https://",
    ):
        assert private_text not in caplog.text


@pytest.mark.asyncio
async def test_text_logs_are_bounded_and_do_not_require_json(credential_identity):
    runner = FakeRunner(credential_identity, [_response(200, "line one\nline two\n")])
    client = GitHubClient(REPOSITORY, runner=runner, max_response_bytes=64)

    logs = await client.request_text(
        "GET",
        "/repos/acme/widgets/actions/jobs/17/logs",
        max_response_bytes=32,
    )

    assert logs == "line one\nline two\n"
    assert runner.calls[0]["max_stdout_bytes"] == MAX_HEADER_BYTES + 32


@pytest.mark.asyncio
async def test_pagination_follows_validated_links_one_bounded_page_at_a_time(
    credential_identity,
):
    first = [{"id": 1, "value": "x" * 38}]
    second = [{"id": 2}]
    first_body = json.dumps(first, separators=(",", ":")).encode()
    runner = FakeRunner(
        credential_identity,
        [
            _response(
                200,
                first_body,
                headers={
                    "Link": (
                        "<https://api.github.com/repositories/303/issues?per_page=1&page=2>; "
                        'rel="next"'
                    )
                },
            ),
            _response(200, second),
        ],
    )
    client = GitHubClient(
        REPOSITORY,
        runner=runner,
        max_response_bytes=100,
        max_pagination_bytes=120,
    )

    assert await client.paged_list("/repositories/303/issues?per_page=1") == first + second

    assert len(runner.calls) == 2
    assert runner.calls[0]["args"][-1] == "repositories/303/issues?per_page=1"
    assert runner.calls[1]["args"][-1] == "repositories/303/issues?per_page=1&page=2"
    assert runner.calls[0]["max_stdout_bytes"] == MAX_HEADER_BYTES + 100
    assert runner.calls[1]["max_stdout_bytes"] == (
        MAX_HEADER_BYTES + min(100, 120 - len(first_body))
    )
    for call in runner.calls:
        assert "--paginate" not in call["args"]
        assert "--slurp" not in call["args"]


@pytest.mark.asyncio
async def test_aggregate_limit_is_applied_before_a_later_page_can_be_consumed(
    credential_identity,
):
    first_body = json.dumps([{"value": "x" * 70}], separators=(",", ":")).encode()
    oversized_second = json.dumps([{"value": "y" * 70}], separators=(",", ":")).encode()
    link = '<https://api.github.com/repositories/303/issues?page=2>; rel="next"'
    runner = FakeRunner(
        credential_identity,
        [
            _response(200, first_body, headers={"Link": link}),
            _response(200, oversized_second),
        ],
    )
    client = GitHubClient(
        REPOSITORY,
        runner=runner,
        max_response_bytes=100,
        max_pagination_bytes=110,
    )

    with pytest.raises(GitHubAccessError, match="size limit") as caught:
        await client.paged_list("/repositories/303/issues")

    assert caught.value.category == "transient"
    remaining = 110 - len(first_body)
    assert runner.calls[1]["max_stdout_bytes"] == MAX_HEADER_BYTES + remaining


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "https://api.github.com/repos/acme/widgets",
        "//api.github.com/repos/acme/widgets",
        "/repos/other/repository/pulls",
        "/repositories/999/pulls",
        "/repos/acme/widgets/../other",
        "/repos/acme/widgets/%2e%2e/other",
        "/repos/acme/widgets/%252e%252e/other",
        "/repos/acme/widgets%2f..%2fother/pulls",
        "/repos/acme/widgets/%5c..%5cother",
        "/repos/acme/widgets/issues/%00hidden",
        "/repos/acme/widgets#foreign",
        "--hostname=attacker.example",
    ],
)
async def test_repository_endpoint_containment_rejects_escapes_before_launch(
    credential_identity,
    path,
):
    runner = FakeRunner(credential_identity, [])
    client = GitHubClient(REPOSITORY, runner=runner)

    with pytest.raises(ValueError, match="repository-bound"):
        await client.request_json("GET", path)

    assert runner.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "next_link",
    [
        "https://attacker.example/repositories/303/issues?page=2",
        "https://api.github.com@attacker.example/repositories/303/issues?page=2",
        "https://api.github.com/repos/other/repository/issues?page=2",
        "https://api.github.com/repositories/303/%2e%2e/404/issues",
        "http://api.github.com/repositories/303/issues?page=2",
    ],
)
async def test_unsafe_next_link_is_rejected_before_a_second_request(
    credential_identity,
    next_link,
):
    runner = FakeRunner(
        credential_identity,
        [_response(200, [], headers={"Link": f'<{next_link}>; rel="next"'})],
    )
    client = GitHubClient(REPOSITORY, runner=runner)

    with pytest.raises(GitHubAccessError, match="pagination") as caught:
        await client.paged_list("/repositories/303/issues")

    assert caught.value.category == "conflict_or_invalid"
    assert len(runner.calls) == 1


@pytest.mark.asyncio
async def test_client_never_manually_follows_redirect_or_exposes_raw_diagnostic(
    credential_identity,
):
    secret = "github_pat_supplied_secret_value"
    runner = FakeRunner(
        credential_identity,
        [
            _response(
                302,
                headers={"Location": "https://objects.example/actions/log.txt"},
                returncode=1,
                stderr=f"Authorization: Bearer {secret}",
            )
        ],
    )
    client = GitHubClient(REPOSITORY, runner=runner)

    with pytest.raises(GitHubAccessError) as caught:
        await client.request_text("GET", "/repositories/303/actions/jobs/17/logs")

    assert caught.value.category == "conflict_or_invalid"
    assert secret not in str(caught.value)
    assert len(runner.calls) == 1
    assert not any(secret in argument for argument in runner.calls[0]["args"])
    assert not any("objects.example" in argument for argument in runner.calls[0]["args"])


@pytest.mark.asyncio
async def test_malformed_non_http_cli_failure_returns_only_safe_category(credential_identity):
    secret = "ghp_abcdefghijklmnopqrstuvwxyz123456"
    runner = FakeRunner(
        credential_identity,
        [FakeResult(1, b"malformed authenticated output", f"failure token={secret}")],
    )
    client = GitHubClient(REPOSITORY, runner=runner)

    with pytest.raises(GitHubAccessError) as caught:
        await client.request_json("GET", "/repositories/303")

    assert caught.value.category == "transient"
    assert secret not in str(caught.value)


@pytest.mark.asyncio
async def test_numeric_ref_helper_allows_encoded_branch_suffix_without_namespace_escape(
    credential_identity,
):
    oid = "a" * 40
    runner = FakeRunner(
        credential_identity,
        [_response(200, {"ref": "refs/heads/feature/x", "object": {"sha": oid}})],
    )
    client = GitHubClient(REPOSITORY, runner=runner)

    assert await client.exact_head_ref("feature/x") == oid
    assert runner.calls[0]["args"][-1] == "repositories/303/git/ref/heads/feature%2Fx"


@pytest.mark.asyncio
async def test_composed_app_read_retries_one_rejected_generation_through_shared_client():
    provider = FakeTokenProvider()
    auth = GitHubAuth(
        GitHubAppConfig("Iv1.client", 101, 202, "/daemon/key.pem"),
        app_provider=provider,
        clock=lambda: 1_800_000_000.0,
    )
    runner = FakeRunner(
        auth.credential_identity,
        [_response(401, returncode=1), _response(200, {"id": 303})],
    )
    client = GitHubClient(REPOSITORY, access=GitHubAccess(auth, runner))

    assert await client.request_json("GET", "/repositories/303") == {"id": 303}

    assert provider.calls == 2
    assert [call["credential"].generation for call in runner.calls] == [1, 2]
    assert all(call["check"] is False for call in runner.calls)


@pytest.mark.asyncio
async def test_shared_client_git_token_uses_the_same_startup_auth_service():
    provider = FakeTokenProvider()
    auth = GitHubAuth(
        GitHubAppConfig("Iv1.client", 101, 202, "/daemon/key.pem"),
        app_provider=provider,
        clock=lambda: 1_800_000_000.0,
    )
    runner = FakeRunner(auth.credential_identity, [])
    access = GitHubAccess(auth, runner)
    client = GitHubClient(REPOSITORY, access=access)

    assert await client.installation_token() == "installation-secret-1"
    assert provider.calls == 1
    assert runner.calls == []

    existing_auth = GitHubAuth()
    existing_runner = FakeRunner(existing_auth.credential_identity, [])
    existing = GitHubClient(
        REPOSITORY,
        access=GitHubAccess(existing_auth, existing_runner),
    )
    assert await existing.installation_token() is None
    assert existing_runner.calls == []


@pytest.mark.asyncio
async def test_composed_app_write_invalidates_rejection_without_replaying_mutation():
    provider = FakeTokenProvider()
    auth = GitHubAuth(
        GitHubAppConfig("Iv1.client", 101, 202, "/daemon/key.pem"),
        app_provider=provider,
        clock=lambda: 1_800_000_000.0,
    )
    runner = FakeRunner(
        auth.credential_identity,
        [_response(401, returncode=1), _response(201, {"id": 17})],
    )
    client = GitHubClient(REPOSITORY, access=GitHubAccess(auth, runner))

    with pytest.raises(GitHubAccessError) as caught:
        await client.request_json(
            "POST",
            "/repositories/303/issues",
            json_body={"title": "one attempt"},
            expected_statuses={201},
        )

    assert caught.value.category == "credentials"
    assert provider.calls == 1
    assert len(runner.calls) == 1
    assert len(runner.results) == 1
    assert await auth.invalidate(REPOSITORY, generation=1) is False


@pytest.mark.asyncio
async def test_exact_pull_request_validates_canonical_base_repository(credential_identity):
    payload = {
        "html_url": "https://github.com/acme/widgets/pull/7",
        "number": 7,
        "state": "open",
        "head": {
            "sha": "a" * 40,
            "repo": {"id": 303, "full_name": "acme/widgets"},
        },
        "base": {"repo": {"id": 303, "full_name": "acme/widgets"}},
    }
    runner = FakeRunner(credential_identity, [_response(200, payload)])
    client = GitHubClient(REPOSITORY, runner=runner)

    assert await client.exact_pull_request(number=7) == {
        "repository_numeric_id": 303,
        "repository_full_name": "acme/widgets",
        "head_sha": "a" * 40,
        "state": "open",
    }

    payload["base"] = {"repo": {"id": 404, "full_name": "other/widgets"}}
    runner.results.append(_response(200, payload))
    with pytest.raises(GitHubAccessError, match="PR identity") as caught:
        await client.exact_pull_request(number=7)
    assert caught.value.category == "conflict_or_invalid"


@pytest.mark.asyncio
async def test_audit_lookup_rechecks_requested_branch_identity(credential_identity):
    key = "b" * 64
    payload = _audit_pull(key=key, head_branch="aq/integration/other")
    runner = FakeRunner(credential_identity, [_response(200, [payload])])
    client = GitHubClient(REPOSITORY, runner=runner)

    with pytest.raises(GitHubAccessError, match="branch identity") as caught:
        await client.lookup_audit_pr(
            idempotency_key=key,
            branch="aq/integration/batch",
        )

    assert caught.value.category == "conflict_or_invalid"
    assert runner.calls[0]["args"][-1].endswith("&head=acme%3Aaq%2Fintegration%2Fbatch")


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["head", "base", "repository", "base_repository"])
async def test_audit_create_confirms_head_base_and_repository_identity(
    credential_identity, mismatch
):
    key = "b" * 64
    payload = _audit_pull(key=key)
    if mismatch == "head":
        payload["head"]["sha"] = "c" * 40
    elif mismatch == "base":
        payload["base"]["ref"] = "other"
    elif mismatch == "repository":
        payload["head"]["repo"] = {"id": 404, "full_name": "other/widgets"}
    else:
        payload["base"]["repo"] = {"id": 404, "full_name": "other/widgets"}
    runner = FakeRunner(
        credential_identity,
        [_response(200, []), _response(201, payload)],
    )
    client = GitHubClient(REPOSITORY, runner=runner)

    with pytest.raises(GitHubAccessError) as caught:
        await client.create_audit_pr(
            repository_id="repo",
            branch="aq/integration/batch",
            head_sha="a" * 40,
            base_branch="main",
            batch_id="batch",
            idempotency_key=key,
            repository_numeric_id=303,
            repository_full_name="acme/widgets",
        )

    assert caught.value.category == "conflict_or_invalid"
    assert [call["args"][call["args"].index("--method") + 1] for call in runner.calls] == [
        "GET",
        "POST",
    ]


@pytest.mark.asyncio
async def test_uncertain_audit_revision_write_reconciles_without_replay(credential_identity):
    old_key = "a" * 64
    new_key = "b" * 64
    old_body = f"<!-- aq-integration-audit:{old_key} -->\nRoot integration batch `batch`."
    new_body = old_body + f"\n<!-- aq-integration-audit:{new_key} -->"
    existing = _audit_pull(key=old_key, body=old_body)
    reconciled = _audit_pull(key=new_key, body=new_body)
    runner = FakeRunner(
        credential_identity,
        [
            _response(200, [existing]),
            _response(500, {"message": "ambiguous"}, returncode=1),
            _response(200, [reconciled]),
        ],
    )
    client = GitHubClient(REPOSITORY, runner=runner)
    arguments = {
        "repository_id": "repo",
        "branch": "aq/integration/batch",
        "head_sha": "a" * 40,
        "base_branch": "main",
        "batch_id": "batch",
        "idempotency_key": new_key,
        "repository_numeric_id": 303,
        "repository_full_name": "acme/widgets",
    }

    with pytest.raises(GitHubAccessError) as caught:
        await client.create_audit_pr(**arguments)
    assert caught.value.category == "transient"

    result = await client.create_audit_pr(**arguments)

    assert result.idempotency_key == new_key
    assert result.head_sha == "a" * 40
    methods = [call["args"][call["args"].index("--method") + 1] for call in runner.calls]
    assert methods == ["GET", "PATCH", "GET"]


@pytest.mark.asyncio
async def test_audit_create_supersedes_closed_pr_of_earlier_revision(credential_identity):
    old_key = "a" * 64
    new_key = "b" * 64
    body = f"<!-- aq-integration-audit:{old_key} -->\nRoot integration batch `batch`."
    stale = _audit_pull(key=old_key, body=body, state="closed", head_sha="c" * 40)
    created = _audit_pull(key=new_key, head_sha="a" * 40)
    created["number"] = 8
    created["html_url"] = "https://github.com/acme/widgets/pull/8"
    runner = FakeRunner(
        credential_identity, [_response(200, [stale]), _response(201, created)]
    )
    client = GitHubClient(REPOSITORY, runner=runner)

    result = await client.create_audit_pr(
        repository_id="repo",
        branch="aq/integration/batch",
        head_sha="a" * 40,
        base_branch="main",
        batch_id="batch",
        idempotency_key=new_key,
        repository_numeric_id=303,
        repository_full_name="acme/widgets",
    )

    assert result.number == 8
    assert result.head_sha == "a" * 40
    methods = [call["args"][call["args"].index("--method") + 1] for call in runner.calls]
    assert methods == ["GET", "POST"]


@pytest.mark.asyncio
async def test_audit_create_refuses_closed_pr_of_another_batch(credential_identity):
    body = f"<!-- aq-integration-audit:{'a' * 64} -->\nRoot integration batch `other`."
    stale = _audit_pull(key="a" * 64, body=body, state="closed", head_sha="c" * 40)
    runner = FakeRunner(credential_identity, [_response(200, [stale])])
    client = GitHubClient(REPOSITORY, runner=runner)

    with pytest.raises(GitHubAccessError) as caught:
        await client.create_audit_pr(
            repository_id="repo",
            branch="aq/integration/batch",
            head_sha="a" * 40,
            base_branch="main",
            batch_id="batch",
            idempotency_key="b" * 64,
            repository_numeric_id=303,
            repository_full_name="acme/widgets",
        )
    assert caught.value.category == "conflict_or_invalid"


@pytest.mark.asyncio
async def test_uncertain_comment_write_is_reconciled_by_marker(credential_identity):
    marker = "<!-- aq-delivery:receipt:head -->"
    body = f"{marker}\nDelivered once."
    runner = FakeRunner(
        credential_identity,
        [
            _response(500, {"message": "ambiguous"}, returncode=1),
            _response(200, [{"id": 91, "body": body}]),
        ],
    )
    client = GitHubClient(REPOSITORY, runner=runner)

    with pytest.raises(GitHubAccessError):
        await client.comment_pull_request(number=7, marker=marker, body=body)

    assert await client.has_comment_marker(number=7, marker=marker) is True
    methods = [call["args"][call["args"].index("--method") + 1] for call in runner.calls]
    assert methods == ["POST", "GET"]


@pytest.mark.asyncio
async def test_ci_observation_uses_shared_repository_client(credential_identity):
    head = "a" * 40
    check = {
        "id": 11,
        "name": "Tests",
        "app": {"id": 404, "slug": "github-actions"},
        "head_sha": head,
        "status": "completed",
        "conclusion": "success",
        "check_suite": {"id": 21},
    }
    workflow = {
        "id": 31,
        "workflow_id": 301,
        "run_attempt": 2,
        "check_suite_id": 21,
        "head_sha": head,
        "status": "completed",
        "conclusion": "success",
        "event": "push",
        "repository": {"id": 303, "full_name": "acme/widgets"},
        "head_repository": {"id": 303, "full_name": "acme/widgets"},
    }
    job = {
        "id": 101,
        "name": "Tests",
        "run_id": 31,
        "run_attempt": 2,
        "head_sha": head,
        "status": "completed",
        "conclusion": "success",
        "check_run_url": "https://api.github.com/repos/acme/widgets/check-runs/11",
    }
    runner = FakeRunner(
        credential_identity,
        [
            # Workflow runs are read first: they decide which check suites
            # belong to the required push event before any check is selected.
            _response(200, {"workflow_runs": [workflow]}),
            _response(200, {"check_runs": [check]}),
            _response(200, {"jobs": [job]}),
        ],
    )
    client = GitHubClient(REPOSITORY, runner=runner)
    trust = IntegrationCITrust(
        canonical_repository_id="repo",
        repository_id=303,
        full_name="acme/widgets",
        producer_id="github-actions",
        required_checks={"version": "checks-v1", "names": ["Tests"]},
    )

    observation = await AuthenticatedGitHubObserver(client, expected_event="push").observe(
        trust, head
    )

    assert isinstance(observation, TrustedCIObservation)
    assert observation.payload.head_sha == head
    assert observation.payload.checks[0].check_run_id == 11
    assert all(call["repository"] == REPOSITORY for call in runner.calls)
    assert all(call["args"][0] == "api" for call in runner.calls)


@pytest.mark.asyncio
async def test_ordinary_pr_create_requires_published_head_and_uses_shared_credential_source(
    credential_identity,
):
    head = "a" * 40
    runner = FakeRunner(
        credential_identity,
        [
            _response(200, {"ref": "refs/heads/feature", "object": {"sha": head}}),
            _response(200, []),
            FakeResult(0, b"https://github.com/acme/widgets/pull/7\n"),
        ],
    )
    client = GitHubClient(REPOSITORY, runner=runner)

    url = await client.create_pull_request(
        title="Fix widget", body="Detailed body\n", base="main", head="feature"
    )

    assert url == "https://github.com/acme/widgets/pull/7"
    assert len(runner.calls) == 3
    create = runner.calls[2]
    assert create["repository"] == REPOSITORY
    assert create["stdin"] == "Detailed body\n"
    assert create["args"] == [
        "pr", "create", "--title", "Fix widget", "--body-file", "-",
        "--base", "main", "--head", "feature",
    ]
    assert all(word not in create["args"] for word in ("push", "fork"))


@pytest.mark.asyncio
async def test_ordinary_pr_create_refuses_unpublished_head_without_write(credential_identity):
    runner = FakeRunner(credential_identity, [
        _response(404, {"message": "Not Found"}),
        _response(200, {"ref": "refs/heads/main", "object": {"sha": "b" * 40}}),
    ])
    client = GitHubClient(REPOSITORY, runner=runner)
    with pytest.raises(GitHubAccessError):
        await client.create_pull_request(title="Fix", body="Body", base="main", head="missing")
    assert len(runner.calls) == 2
    assert all(call["args"][0] == "api" for call in runner.calls)


@pytest.mark.asyncio
async def test_ordinary_create_does_not_call_hidden_repo_an_unpublished_head(credential_identity):
    runner = FakeRunner(credential_identity, [
        _response(404, {"message": "Not Found"}),
        _response(404, {"message": "Not Found"}),
    ])
    client = GitHubClient(REPOSITORY, runner=runner)
    with pytest.raises(GitHubAccessError) as caught:
        await client.create_pull_request(title="Fix", body="Body", base="main", head="feature")
    assert caught.value.category == "not_found_or_hidden"
    assert all(call["args"][0] == "api" for call in runner.calls)


@pytest.mark.asyncio
async def test_ordinary_merge_pins_head_and_rejects_foreign_pr(credential_identity):
    sha = "b" * 40
    runner = FakeRunner(
        credential_identity,
        [FakeResult(0, f"Merged pull request #7 ({sha}).\n".encode())],
    )
    client = GitHubClient(REPOSITORY, runner=runner)
    with pytest.raises(GitHubAccessError):
        await client.merge_pull_request(
            "https://github.com/other/repo/pull/7", method="merge",
            expected_head_oid="a" * 40,
        )
    assert not runner.calls

    result = await client.merge_pull_request(
        "https://github.com/acme/widgets/pull/7", method="merge",
        expected_head_oid="a" * 40,
    )
    assert result == sha
    assert runner.calls[0]["repository"] == REPOSITORY
    assert runner.calls[0]["args"] == [
        "pr", "merge", "7", "--merge", "--match-head-commit", "a" * 40,
        "--delete-branch",
    ]


def _ordinary_pull(*, head_sha="a" * 40, base="main", merged=False):
    return {
        "number": 7,
        "html_url": "https://github.com/acme/widgets/pull/7",
        "state": "closed" if merged else "open",
        "merged_at": "2026-09-22T00:00:00Z" if merged else None,
        "merge_commit_sha": "b" * 40 if merged else None,
        "head": {
            "ref": "feature", "sha": head_sha,
            "repo": {"id": 303, "full_name": "acme/widgets"},
        },
        "base": {"ref": base, "repo": {"id": 303, "full_name": "acme/widgets"}},
    }


@pytest.mark.asyncio
async def test_ordinary_create_reuses_exact_existing_pr_before_write(credential_identity):
    runner = FakeRunner(credential_identity, [
        _response(200, {"ref": "refs/heads/feature", "object": {"sha": "a" * 40}}),
        _response(200, [_ordinary_pull()]),
    ])
    client = GitHubClient(REPOSITORY, runner=runner)

    creation = await client.create_pull_request_result(
        title="Fix", body="Body", base="main", head="feature"
    )
    assert creation.url == "https://github.com/acme/widgets/pull/7"
    assert creation.created is False
    assert len(runner.calls) == 2
    assert all(call["args"][0] == "api" for call in runner.calls)


@pytest.mark.asyncio
async def test_ordinary_create_reconciles_uncertain_cli_failure_without_replay(credential_identity):
    runner = FakeRunner(credential_identity, [
        _response(200, {"ref": "refs/heads/feature", "object": {"sha": "a" * 40}}),
        _response(200, []),
        FakeResult(1, b"", "request timed out"),
        _response(200, [_ordinary_pull()]),
    ])
    client = GitHubClient(REPOSITORY, runner=runner)

    creation = await client.create_pull_request_result(
        title="Fix", body="Body", base="main", head="feature"
    )
    assert creation.url == "https://github.com/acme/widgets/pull/7"
    assert creation.created is False
    assert sum(call["args"][:2] == ["pr", "create"] for call in runner.calls) == 1


@pytest.mark.asyncio
async def test_app_create_reconciles_rejected_write_with_refreshed_read():
    provider = FakeTokenProvider()
    auth = GitHubAuth(
        GitHubAppConfig("Iv1.client", 101, 202, "/daemon/key.pem"),
        app_provider=provider, clock=lambda: 1_800_000_000.0,
    )
    runner = FakeRunner(auth.credential_identity, [
        _response(200, {"ref": "refs/heads/feature", "object": {"sha": "a" * 40}}),
        _response(200, []),
        FakeResult(4, b"", "HTTP 401 Unauthorized"),
        _response(200, [_ordinary_pull()]),
    ])
    client = GitHubClient(REPOSITORY, access=GitHubAccess(auth, runner))

    assert await client.create_pull_request(
        title="Fix", body="Body", base="main", head="feature"
    ) == "https://github.com/acme/widgets/pull/7"
    assert provider.calls == 2
    assert [call["credential"].generation for call in runner.calls] == [1, 1, 1, 2]
    assert sum(call["args"][:2] == ["pr", "create"] for call in runner.calls) == 1


@pytest.mark.asyncio
async def test_ordinary_create_rejects_conflicting_existing_pr(credential_identity):
    runner = FakeRunner(credential_identity, [
        _response(200, {"ref": "refs/heads/feature", "object": {"sha": "a" * 40}}),
        _response(200, [_ordinary_pull(base="release")]),
    ])
    client = GitHubClient(REPOSITORY, runner=runner)
    with pytest.raises(GitHubAccessError, match="existing PR head did not match"):
        await client.create_pull_request(title="Fix", body="Body", base="main", head="feature")
    assert len(runner.calls) == 2


@pytest.mark.asyncio
async def test_ordinary_merge_reports_confirmed_cleanup_failure(credential_identity):
    from src.git.github import GitHubMergeReconciled

    runner = FakeRunner(credential_identity, [
        FakeResult(1, b"", "remote branch cleanup failed"),
        _response(200, _ordinary_pull(merged=True)),
        _response(200, {"ref": "refs/heads/feature", "object": {"sha": "a" * 40}}),
    ])
    client = GitHubClient(REPOSITORY, runner=runner)
    with pytest.raises(GitHubMergeReconciled) as caught:
        await client.merge_pull_request(
            "https://github.com/acme/widgets/pull/7", method="merge",
            expected_head_oid="a" * 40, expected_base_ref="main",
        )
    assert caught.value.sha == "b" * 40
    assert caught.value.outcome == "merged_cleanup_failed"
    assert sum(call["args"][:2] == ["pr", "merge"] for call in runner.calls) == 1


@pytest.mark.asyncio
async def test_ordinary_merge_reconciles_when_cleanup_already_finished(credential_identity):
    from src.git.github import GitHubMergeReconciled

    runner = FakeRunner(credential_identity, [
        FakeResult(1, b"", "CLI exited after merging"),
        _response(200, _ordinary_pull(merged=True)),
        _response(404, {"message": "Not Found"}),
        _response(200, {"ref": "refs/heads/main", "object": {"sha": "c" * 40}}),
    ])
    client = GitHubClient(REPOSITORY, runner=runner)
    with pytest.raises(GitHubMergeReconciled) as caught:
        await client.merge_pull_request(
            "https://github.com/acme/widgets/pull/7", method="merge",
            expected_head_oid="a" * 40, expected_base_ref="main",
        )
    assert caught.value.outcome == "merged_reconciled"


@pytest.mark.asyncio
async def test_ordinary_merge_does_not_assume_hidden_head_was_deleted(credential_identity):
    from src.git.github import GitHubMergeReconciled

    runner = FakeRunner(credential_identity, [
        FakeResult(1, b"", "CLI exited after merging"),
        _response(200, _ordinary_pull(merged=True)),
        _response(404, {"message": "Not Found"}),
        _response(404, {"message": "Not Found"}),
    ])
    client = GitHubClient(REPOSITORY, runner=runner)
    with pytest.raises(GitHubMergeReconciled) as caught:
        await client.merge_pull_request(
            "https://github.com/acme/widgets/pull/7", method="merge",
            expected_head_oid="a" * 40, expected_base_ref="main",
        )
    assert caught.value.outcome == "merged_cleanup_unknown"


@pytest.mark.asyncio
async def test_ordinary_merge_does_not_replay_confirmed_rejection(credential_identity):
    runner = FakeRunner(credential_identity, [
        FakeResult(4, b"", "HTTP 401 Unauthorized"),
        _response(200, _ordinary_pull()),
    ])
    client = GitHubClient(REPOSITORY, runner=runner)
    with pytest.raises(GitHubAccessError) as caught:
        await client.merge_pull_request(
            "https://github.com/acme/widgets/pull/7", method="merge",
            expected_head_oid="a" * 40, expected_base_ref="main",
        )
    assert caught.value.category == "credentials"
    assert sum(call["args"][:2] == ["pr", "merge"] for call in runner.calls) == 1


@pytest.mark.asyncio
async def test_ordinary_pr_and_ci_reads_are_repository_bound(credential_identity):
    pull = {
        "number": 7,
        "html_url": "https://github.com/acme/widgets/pull/7",
        "base": {"repo": {"id": 303, "full_name": "acme/widgets"}, "ref": "main"},
        "head": {"sha": "a" * 40},
    }
    runner = FakeRunner(
        credential_identity,
        [
            _response(200, pull),
            FakeResult(0, b'{"statusCheckRollup": []}'),
            _response(200, {"sha": "a" * 40}),
            _response(200, {"check_runs": [{"name": "Tests", "conclusion": "success"}]}),
            _response(200, {"behind_by": 0}),
            _response(200, b"FAILED tests/test_widget.py::test_fix\n"),
        ],
    )
    client = GitHubClient(REPOSITORY, runner=runner)
    url = "https://github.com/acme/widgets/pull/7"
    assert (await client.pull_request(url))["number"] == 7
    assert await client.check_rollup(url) == []
    assert await client.commit_head("main") == "a" * 40
    assert await client.commit_check_runs("a" * 40) == [
        {"name": "Tests", "conclusion": "success"}
    ]
    assert (await client.compare("main", "a" * 40))["behind_by"] == 0
    assert "FAILED tests/test_widget.py" in await client.job_log(17)
    assert all(call["repository"] == REPOSITORY for call in runner.calls)
    assert runner.calls[-1]["args"][-1] == "repositories/303/actions/jobs/17/logs"


@pytest.mark.asyncio
async def test_rerequesting_a_check_suite_is_a_repository_bound_write():
    """A re-run re-checks one named check suite and can reach nothing else.

    GitHub's documented endpoint, which is the only way to retry CI for a head
    that has not moved:
    https://docs.github.com/en/rest/checks/suites#rerequest-a-check-suite —
    ``POST /repos/{owner}/{repo}/check-suites/{check_suite_id}/rerequest``,
    201 Created.  There is no write at ``commits/{ref}/check-suites``: that path
    only lists suites.
    """
    runner = FakeRunner(GitHubCredentialIdentity.app(101, 202), [_response(201, b"")])
    client = GitHubClient(REPOSITORY, runner=runner)
    await client.rerequest_check_suite(4242)
    call = runner.calls[-1]
    assert call["args"][:4] == ["api", "--include", "--method", "POST"]
    assert call["args"][-1] == "repos/acme/widgets/check-suites/4242/rerequest"
    # Addressed by a validated suite id, so no caller-supplied endpoint can
    # name another repository, commit or head.
    for suite_id in (0, -1, True, "4242", "4242/rerequest/../../other"):
        with pytest.raises(ValueError, match="invalid check suite ID"):
            await client.rerequest_check_suite(suite_id)
    assert len(runner.calls) == 1


@pytest.mark.asyncio
async def test_check_suite_rerequest_refuses_a_credential_that_cannot_write_checks():
    """GitHub documents writes to checks as GitHub-App-only.

    https://docs.github.com/en/rest/guides/using-the-rest-api-to-interact-with-checks
    — OAuth apps and authenticated users can read check runs and suites but not
    write them.  An existing-login credential therefore issues no request at all,
    rather than asking for one that cannot succeed: widening a credential is a
    human decision, and the caller falls back to its named infrastructure
    blocker.
    """
    runner = FakeRunner(GitHubCredentialIdentity.existing_login(), [_response(201, b"")])
    client = GitHubClient(REPOSITORY, runner=runner)
    with pytest.raises(GitHubAccessError, match="GitHub-App-only"):
        await client.rerequest_check_suite(4242)
    assert runner.calls == []


@pytest.mark.asyncio
async def test_ordinary_pr_payload_cannot_rebind_a_project(credential_identity):
    runner = FakeRunner(credential_identity, [
        _response(200, {
            "number": 7,
            "html_url": "https://github.com/acme/widgets/pull/7",
            "base": {"repo": {"id": 404, "full_name": "other/repo"}},
        }),
    ])
    client = GitHubClient(REPOSITORY, runner=runner)
    with pytest.raises(GitHubAccessError, match="repository did not match"):
        await client.pull_request("https://github.com/acme/widgets/pull/7")


@pytest.mark.asyncio
async def test_app_credential_failure_prevents_ordinary_pr_launch():
    class FailingProvider:
        credential_identity = GitHubCredentialIdentity.app(101, 202)

        async def mint(self, repository):
            raise GitHubAccessError("credentials", "App token unavailable")

    auth = GitHubAuth(
        GitHubAppConfig("Iv1.client", 101, 202, "/daemon/key.pem"),
        app_provider=FailingProvider(),
    )
    runner = FakeRunner(auth.credential_identity, [])
    client = GitHubClient(REPOSITORY, access=GitHubAccess(auth, runner))
    with pytest.raises(GitHubAccessError, match="App token unavailable"):
        await client.create_pull_request(
            title="Fix", body="Body", base="main", head="feature"
        )
    assert not runner.calls


@pytest.mark.asyncio
async def test_app_credential_failure_before_merge_is_not_an_uncertain_write():
    class FailingProvider:
        credential_identity = GitHubCredentialIdentity.app(101, 202)

        async def mint(self, repository):
            raise GitHubAccessError("credentials", "App token unavailable")

    auth = GitHubAuth(
        GitHubAppConfig("Iv1.client", 101, 202, "/daemon/key.pem"),
        app_provider=FailingProvider(),
    )
    runner = FakeRunner(auth.credential_identity, [])
    client = GitHubClient(REPOSITORY, access=GitHubAccess(auth, runner))

    with pytest.raises(GitHubAccessError, match="App token unavailable") as caught:
        await client.merge_pull_request(
            "https://github.com/acme/widgets/pull/7", method="merge",
            expected_head_oid="a" * 40, expected_base_ref="main",
        )
    assert caught.value.category == "credentials"
    assert not runner.calls


@pytest.mark.asyncio
async def test_rate_limited_cli_failure_logs_scrubbed_stderr(tmp_path, caplog):
    executable = tmp_path / "gh"
    executable.write_text(
        "#!/usr/bin/python3\nimport sys\n"
        "sys.stderr.write('Authorization: Bearer opaque-secret\\n'"
        " + 'gh: You have exceeded a secondary rate limit (HTTP 403)\\n')\n"
        "sys.exit(1)\n"
    )
    executable.chmod(0o700)
    runner = GhRunner(GitHubAuth(), executable=str(executable), env={}, cwd=tmp_path)
    caplog.set_level(logging.WARNING, logger="src.git.github_cli")

    result = await runner.run(["api", "repos/acme/widgets"], repository=REPOSITORY, check=False)

    assert result.returncode == 1
    assert "gh rate_limited; repository=acme/widgets" in caplog.text
    assert "secondary rate limit" in caplog.text
    assert "opaque-secret" not in caplog.text


async def test_authenticated_user_requires_human_credentials_and_fixed_endpoint():
    runner = FakeRunner(GitHubCredentialIdentity.existing_login(), [
        _response(200, {"login": "operator", "type": "User"}),
    ])
    client = GitHubClient(REPOSITORY, runner=runner)
    assert (await client.authenticated_user())["login"] == "operator"
    assert runner.calls[0]["args"][-1] == "user"
    assert runner.calls[0]["repository"] == REPOSITORY
    with pytest.raises(ValueError, match="repository-bound"):
        await client.request_json("GET", "/user")
    app_runner = FakeRunner(GitHubCredentialIdentity.app(101, 202), [])
    with pytest.raises(ValueError, match="existing-login"):
        await GitHubClient(REPOSITORY, runner=app_runner).authenticated_user()
    assert not app_runner.calls
