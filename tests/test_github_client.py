from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from src.config import GitHubAppConfig
from src.git.github import GitHubAccess, GitHubClient, MAX_HEADER_BYTES
from src.git.github_app import AppTokenCandidate
from src.git.github_auth import GitHubAuth
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
        return self.results.pop(0)


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
    assert "secret diagnostic" not in str(caught.value)


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
            _response(200, {"check_runs": [check]}),
            _response(200, {"workflow_runs": [workflow]}),
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

    observation = await AuthenticatedGitHubObserver(client).observe(trust, head)

    assert isinstance(observation, TrustedCIObservation)
    assert observation.payload.head_sha == head
    assert observation.payload.checks[0].check_run_id == 11
    assert all(call["repository"] == REPOSITORY for call in runner.calls)
    assert all(call["args"][0] == "api" for call in runner.calls)
