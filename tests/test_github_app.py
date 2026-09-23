from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from src.config import GitHubAppConfig
from src.git.github_app import (
    AiohttpTransport,
    AppTokenProvider,
    GitHubAppError,
    GitHubRepositoryBinding,
    HttpResponse,
    OwnerFilePrivateKeyProvider,
)


@pytest.mark.parametrize(
    "full_name",
    ["acme/widgets?redirect=https://attacker.example", "acme/widgets/extra", "-acme/widgets"],
)
def test_repository_binding_rejects_nonliteral_github_full_name(full_name):
    with pytest.raises(ValueError, match="full_name"):
        GitHubRepositoryBinding(303, full_name)


class StaticKeyProvider:
    def __init__(self, key: bytes):
        self.key = key

    def read_private_key(self, path: str) -> bytes:
        assert path == "/daemon/key.pem"
        return self.key


class ScriptedTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def request(self, method, url, *, headers, json_body=None, max_bytes):
        self.requests.append((method, url, dict(headers), json_body, max_bytes))
        return self.responses.pop(0)


class PartialReadContent:
    def __init__(self, chunks: list[bytes]):
        self.chunks = list(chunks)
        self.read_sizes: list[int] = []

    async def read(self, size: int) -> bytes:
        self.read_sizes.append(size)
        if not self.chunks:
            return b""
        chunk = self.chunks.pop(0)
        assert len(chunk) <= size
        return chunk


class FakeAiohttpResponse:
    def __init__(self, chunks: list[bytes], *, status: int = 201):
        self.status = status
        self.headers = {"Content-Type": "application/json"}
        self.content = PartialReadContent(chunks)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None


class FakeAiohttpSession:
    def __init__(self, response: FakeAiohttpResponse):
        self.response = response
        self.request_kwargs = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    def request(self, *_args, **kwargs):
        self.request_kwargs = kwargs
        return self.response


@pytest.mark.asyncio
async def test_aiohttp_transport_reads_entire_partial_success_response(monkeypatch):
    payload = json.dumps({"padding": "x" * 6700}).encode()
    response = FakeAiohttpResponse([payload[:239], payload[239:400], payload[400:]])
    session = FakeAiohttpSession(response)
    monkeypatch.setattr("src.git.github_app.aiohttp.ClientSession", lambda **_kwargs: session)

    result = await AiohttpTransport().request(
        "POST",
        "https://api.github.com/app/installations/202/access_tokens",
        headers={},
        max_bytes=8192,
    )

    assert result.status == 201
    assert json.loads(result.body) == {"padding": "x" * 6700}
    assert response.content.read_sizes == [8193, 8193 - 239, 8193 - 400, 8193 - len(payload)]
    assert session.request_kwargs["allow_redirects"] is False


@pytest.mark.asyncio
async def test_aiohttp_transport_rejects_oversized_partial_response(monkeypatch):
    response = FakeAiohttpResponse([b"abcd", b"efgh", b"i"])
    session = FakeAiohttpSession(response)
    monkeypatch.setattr("src.git.github_app.aiohttp.ClientSession", lambda **_kwargs: session)

    with pytest.raises(GitHubAppError, match="size limit") as caught:
        await AiohttpTransport().request(
            "GET", "https://api.github.com/app", headers={}, max_bytes=8
        )

    assert caught.value.category == "transient"
    assert response.content.read_sizes == [9, 5, 1]


def _private_key() -> tuple[bytes, bytes]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private, public


def _installation_response(token: str, *, expires: str = "2030-01-01T00:00:00Z") -> HttpResponse:
    permissions = {
        "checks": "write",
        "actions": "read",
        "contents": "write",
        "administration": "read",
        "pull_requests": "write",
        "issues": "write",
        "variables": "read",
    }
    return HttpResponse(
        201,
        {},
        (
            '{"token":"%s","expires_at":"%s",'
            '"repositories":[{"id":303,"full_name":"acme/widgets"}],"permissions":%s}'
            % (token, expires, json.dumps(permissions))
        ).encode(),
    )


@pytest.mark.asyncio
async def test_mints_narrow_installation_token_after_app_and_repository_binding():
    private, public = _private_key()
    now = 1_800_000_000.0
    expires = (datetime.fromtimestamp(now, UTC) + timedelta(hours=1)).isoformat()
    permissions = {
        "checks": "write",
        "actions": "read",
        "contents": "write",
        "administration": "read",
        "pull_requests": "write",
        "issues": "write",
        "variables": "read",
    }
    token_response = HttpResponse(
        201,
        {},
        (
            '{"token":"installation-secret","expires_at":"%s",'
            '"repositories":[{"id":303,"full_name":"acme/widgets"}],"permissions":'
            '{"checks":"write","actions":"read","contents":"write",'
            '"administration":"read","pull_requests":"write",'
            '"issues":"write","variables":"read","metadata":"read"}}' % expires
        ).encode(),
    )
    transport = ScriptedTransport([HttpResponse(200, {}, b'{"id":101}'), token_response])
    provider = AppTokenProvider(
        GitHubAppConfig("Iv1.client", 101, 202, "/daemon/key.pem"),
        key_provider=StaticKeyProvider(private),
        transport=transport,
        clock=lambda: now,
    )

    candidate = await provider.mint(GitHubRepositoryBinding(303, "acme/widgets"))

    assert candidate.token == "installation-secret"
    assert candidate.repository == GitHubRepositoryBinding(303, "acme/widgets")
    app_jwt = transport.requests[0][2]["Authorization"].removeprefix("Bearer ")
    claims = jwt.decode(
        app_jwt,
        public,
        algorithms=["RS256"],
        options={"verify_exp": False, "verify_iat": False},
    )
    assert claims == {"iat": 1_799_999_940, "exp": 1_800_000_540, "iss": "Iv1.client"}
    assert transport.requests[1][3] == {"repository_ids": [303], "permissions": permissions}
    for request in transport.requests:
        assert request[2]["Accept"] == "application/vnd.github+json"
        assert request[2]["X-GitHub-Api-Version"] == "2022-11-28"


@pytest.mark.asyncio
async def test_app_provider_rejects_bootstrap_redirect_without_following_location():
    private, _ = _private_key()
    transport = ScriptedTransport(
        [HttpResponse(302, {"Location": "https://attacker.example/app"}, b"")]
    )
    provider = AppTokenProvider(
        GitHubAppConfig("Iv1.client", 101, 202, "/daemon/key.pem"),
        key_provider=StaticKeyProvider(private),
        transport=transport,
        clock=lambda: 1_800_000_000.0,
    )

    with pytest.raises(GitHubAppError, match="redirect") as caught:
        await provider.mint(GitHubRepositoryBinding(303, "acme/widgets"))

    assert caught.value.category == "conflict_or_invalid"
    assert [request[1] for request in transport.requests] == ["https://api.github.com/app"]


@pytest.mark.asyncio
async def test_app_provider_rejects_expired_or_widened_token_response():
    private, _ = _private_key()
    responses = [
        HttpResponse(200, {}, b'{"id":101}'),
        HttpResponse(
            201,
            {},
            b'{"token":"sensitive","expires_at":"2030-01-01T00:00:00Z",'
            b'"repositories":[{"id":303,"full_name":"acme/widgets"}],'
            b'"permissions":{"checks":"write",'
            b'"actions":"read","contents":"write","administration":"write",'
            b'"pull_requests":"write","issues":"write","variables":"read"}}',
        ),
    ]
    provider = AppTokenProvider(
        GitHubAppConfig("Iv1.client", 101, 202, "/daemon/key.pem"),
        key_provider=StaticKeyProvider(private),
        transport=ScriptedTransport(responses),
        clock=lambda: 1_800_000_000.0,
    )

    with pytest.raises(GitHubAppError, match="permissions") as widened:
        await provider.mint(GitHubRepositoryBinding(303, "acme/widgets"))
    assert widened.value.category == "permission"

    expired_transport = ScriptedTransport(
        [
            HttpResponse(200, {}, b'{"id":101}'),
            HttpResponse(
                201,
                {},
                b'{"token":"sensitive","expires_at":"2030-01-01T00:00:00Z",'
                b'"repositories":[{"id":303,"full_name":"acme/widgets"}],'
                b'"permissions":{"checks":"write",'
                b'"actions":"read","contents":"write","administration":"read",'
                b'"pull_requests":"write","issues":"write","variables":"read"}}',
            ),
        ]
    )
    expired = AppTokenProvider(
        GitHubAppConfig("Iv1.client", 101, 202, "/daemon/key.pem"),
        key_provider=StaticKeyProvider(private),
        transport=expired_transport,
        clock=lambda: 1_900_000_000.0,
    )

    with pytest.raises(GitHubAppError, match="expiry") as expiry:
        await expired.mint(GitHubRepositoryBinding(303, "acme/widgets"))
    assert expiry.value.category == "credentials"


@pytest.mark.asyncio
async def test_rejects_installation_token_without_variables_read_permission():
    private, _ = _private_key()
    transport = ScriptedTransport(
        [
            HttpResponse(200, {}, b'{"id":101}'),
            HttpResponse(
                201,
                {},
                b'{"token":"installation-secret",'
                b'"expires_at":"2030-01-01T00:00:00Z",'
                b'"repositories":[{"id":303,"full_name":"acme/widgets"}],'
                b'"permissions":{"checks":"write",'
                b'"actions":"read","contents":"write","administration":"read",'
                b'"pull_requests":"write","issues":"write"}}',
            ),
        ]
    )
    provider = AppTokenProvider(
        GitHubAppConfig("Iv1.client", 101, 202, "/daemon/key.pem"),
        key_provider=StaticKeyProvider(private),
        transport=transport,
        clock=lambda: 1_800_000_000.0,
    )

    with pytest.raises(GitHubAppError) as caught:
        await provider.mint(GitHubRepositoryBinding(303, "acme/widgets"))

    assert caught.value.category == "permission"
    assert len(transport.requests) == 2


@pytest.mark.asyncio
async def test_binds_repository_by_name_with_one_narrow_installation_token():
    private, _ = _private_key()
    permissions = {
        "checks": "write",
        "actions": "read",
        "contents": "write",
        "administration": "read",
        "pull_requests": "write",
        "issues": "write",
        "variables": "read",
    }
    transport = ScriptedTransport(
        [
            HttpResponse(200, {}, b'{"id":101}'),
            HttpResponse(
                201,
                {},
                (
                    '{"token":"installation-secret","expires_at":"2030-01-01T00:00:00Z",'
                    '"repositories":[{"id":303,"name":"widgets",'
                    '"full_name":"acme/widgets"}],"permissions":%s}'
                    % json.dumps(permissions)
                ).encode(),
            ),
        ]
    )
    provider = AppTokenProvider(
        GitHubAppConfig("Iv1.client", 101, 202, "/daemon/key.pem"),
        key_provider=StaticKeyProvider(private),
        transport=transport,
        clock=lambda: 1_800_000_000.0,
    )

    candidate = await provider.mint_for_repository("acme/widgets")

    assert candidate.repository == GitHubRepositoryBinding(303, "acme/widgets")
    assert candidate.token == "installation-secret"
    assert transport.requests[1][3] == {"repositories": ["widgets"], "permissions": permissions}


@pytest.mark.asyncio
async def test_repository_identity_mismatch_fails_closed_without_response_body():
    private, _ = _private_key()
    transport = ScriptedTransport(
        [
            HttpResponse(200, {}, b'{"id":101}'),
            HttpResponse(
                201,
                {},
                b'{"token":"sensitive","expires_at":"2030-01-01T00:00:00Z",'
                b'"repositories":[{"id":303,"full_name":"attacker/redirected"}],'
                b'"permissions":{"checks":"write",'
                b'"actions":"read","contents":"write","administration":"read",'
                b'"pull_requests":"write","issues":"write","variables":"read"}}',
            ),
        ]
    )
    provider = AppTokenProvider(
        GitHubAppConfig("Iv1.client", 101, 202, "/daemon/key.pem"),
        key_provider=StaticKeyProvider(private),
        transport=transport,
        clock=lambda: 1_800_000_000.0,
    )

    with pytest.raises(GitHubAppError) as caught:
        await provider.mint(GitHubRepositoryBinding(303, "acme/widgets"))
    assert caught.value.category == "credentials"
    assert "sensitive" not in str(caught.value)
    assert "redirected" not in str(caught.value)


def test_file_key_provider_rejects_group_readable_and_symlink_paths(tmp_path):
    key = tmp_path / "key.pem"
    key.write_bytes(b"private-sentinel")
    key.chmod(0o640)
    provider = OwnerFilePrivateKeyProvider()
    with pytest.raises(GitHubAppError, match="permissions") as broad:
        provider.read_private_key(str(key))
    assert "private-sentinel" not in str(broad.value)

    link = tmp_path / "link.pem"
    link.symlink_to(key)
    with pytest.raises(GitHubAppError, match="unreadable"):
        provider.read_private_key(str(link))
