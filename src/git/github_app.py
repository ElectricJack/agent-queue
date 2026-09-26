"""GitHub App bootstrap: fixed-identity App tokens for the shared ``gh`` runner.

``AppTokenProvider`` owns the only direct GitHub HTTP calls: fixed App identity
and installation-token bootstrap requests.  Composed repository operations live
in ``src.git.github`` and are driven through ``GhRunner`` with the tokens minted
by this provider.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import time
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

import aiohttp
import jwt

from src.config import GitHubAppConfig
from src.git.github import MAX_RESPONSE_BYTES
from src.git.github_contracts import (
    GitHubAccessError,
    GitHubCredentialIdentity,
    GitHubRepositoryBinding,
)

ACCEPT = "application/vnd.github+json"
API_VERSION = "2022-11-28"
API_BASE = "https://api.github.com"
_PERMISSIONS = {
    "checks": "write",
    "actions": "read",
    "contents": "write",
    "administration": "read",
    "pull_requests": "write",
    "issues": "write",
    "actions_variables": "read",
    "workflows": "write",
}


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes


class PrivateKeyProvider(Protocol):
    def read_private_key(self, path: str) -> bytes: ...


class OwnerFilePrivateKeyProvider:
    """Read a regular private-key file owned by this daemon user only."""

    def read_private_key(self, path: str) -> bytes:
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError as exc:
            raise GitHubAppError("credentials", "private key is unreadable") from exc
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                raise GitHubAppError("credentials", "private key ownership is invalid")
            if not info.st_mode & stat.S_IRUSR or info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
                raise GitHubAppError("credentials", "private key permissions are too broad")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                return stream.read(MAX_RESPONSE_BYTES + 1)
        except OSError as exc:
            raise GitHubAppError("credentials", "private key is unreadable") from exc
        finally:
            os.close(descriptor)


class HttpTransport(Protocol):
    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json_body: dict[str, Any] | None = None,
        max_bytes: int,
    ) -> HttpResponse: ...


class AiohttpTransport:
    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json_body: dict[str, Any] | None = None,
        max_bytes: int,
    ) -> HttpResponse:
        timeout = aiohttp.ClientTimeout(total=30)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.request(
                    method,
                    url,
                    headers=headers,
                    json=json_body,
                    allow_redirects=False,
                ) as response:
                    chunks: list[bytes] = []
                    size = 0
                    while True:
                        chunk = await response.content.read(max_bytes + 1 - size)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > max_bytes:
                            raise GitHubAppError("transient", "GitHub response exceeded size limit")
                        chunks.append(chunk)
                    return HttpResponse(response.status, dict(response.headers), b"".join(chunks))
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise GitHubAppError("transient", "GitHub request failed") from exc


# Compatibility name retained until the transport-specific client is removed.
GitHubAppError = GitHubAccessError


@dataclass(frozen=True, slots=True)
class AppTokenCandidate:
    """A not-yet-exposed repository credential minted by the configured App.

    The candidate is handed to the shared ``gh`` binding path for repository
    identity verification before ``GitHubAuth`` accepts it into its ready
    cache.  The secret is deliberately omitted from representations.
    """

    identity: GitHubCredentialIdentity
    repository: GitHubRepositoryBinding
    token: str = field(repr=False)
    expires_at: float


class AppTokenProvider:
    """Sign App JWTs and mint fixed-permission, single-repository tokens."""

    def __init__(
        self,
        config: GitHubAppConfig,
        *,
        key_provider: PrivateKeyProvider,
        transport: HttpTransport | None = None,
        clock=time.time,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ) -> None:
        if config.validate():
            raise ValueError("invalid GitHub App configuration")
        self.config = config
        self.key_provider = key_provider
        self.transport = transport or AiohttpTransport()
        self.clock = clock
        self.max_response_bytes = max_response_bytes

    @property
    def credential_identity(self) -> GitHubCredentialIdentity:
        return GitHubCredentialIdentity.app(
            self.config.app_id,
            self.config.installation_id,
        )

    async def mint(self, repository: GitHubRepositoryBinding) -> AppTokenCandidate:
        """Mint a fresh token for an already validated repository binding."""
        payload = await self._mint_payload(repository_ids=[repository.repository_id])
        selected = _single_repository(payload)
        if _strict_positive_int(selected.get("id")) != repository.repository_id:
            raise GitHubAppError("permission", "installation repository selection did not match")
        if selected.get("full_name") != repository.full_name:
            raise GitHubAppError("credentials", "authenticated repository identity did not match")
        return self._candidate(payload, repository)

    async def mint_for_repository(self, full_name: str) -> AppTokenCandidate:
        """Mint a candidate token while resolving a canonical repository name.

        This is bootstrap only: the returned numeric/name binding still needs
        verification through the shared ``gh`` runner before it becomes ready.
        """
        provisional = GitHubRepositoryBinding(1, full_name)
        repository_name = provisional.full_name.split("/", 1)[1]
        payload = await self._mint_payload(repository_names=[repository_name])
        selected = _single_repository(payload)
        repository_id = _strict_positive_int(selected.get("id"))
        if (
            repository_id is None
            or selected.get("name") != repository_name
            or selected.get("full_name") != provisional.full_name
        ):
            raise GitHubAppError("credentials", "authenticated repository identity did not match")
        return self._candidate(
            payload,
            GitHubRepositoryBinding(repository_id, provisional.full_name),
        )

    async def _mint_payload(
        self,
        *,
        repository_ids: list[int] | None = None,
        repository_names: list[str] | None = None,
    ) -> dict[str, Any]:
        if (repository_ids is None) == (repository_names is None):
            raise ValueError("exactly one repository selector is required")
        app_jwt = self._app_jwt()
        app = await self._bootstrap_json("GET", "/app", credential=app_jwt)
        if _strict_positive_int(app.get("id")) != self.config.app_id:
            raise GitHubAppError("credentials", "authenticated App identity did not match")
        selector: dict[str, Any]
        if repository_ids is not None:
            selector = {"repository_ids": repository_ids}
        else:
            selector = {"repositories": repository_names}
        return await self._bootstrap_json(
            "POST",
            f"/app/installations/{self.config.installation_id}/access_tokens",
            credential=app_jwt,
            json_body=selector | {"permissions": dict(_PERMISSIONS)},
            expected_statuses={201},
        )

    def _candidate(
        self,
        payload: dict[str, Any],
        repository: GitHubRepositoryBinding,
    ) -> AppTokenCandidate:
        token = payload.get("token")
        if not isinstance(token, str) or not token:
            raise GitHubAppError("credentials", "installation token response was malformed")
        _validate_permissions(payload.get("permissions"))
        expires_at = _parse_timestamp(payload.get("expires_at"))
        if expires_at - self.clock() <= 300:
            raise GitHubAppError("credentials", "installation token expiry was invalid")
        return AppTokenCandidate(
            identity=self.credential_identity,
            repository=repository,
            token=token,
            expires_at=expires_at,
        )

    def _app_jwt(self) -> str:
        now = int(self.clock())
        private_key = self.key_provider.read_private_key(self.config.private_key_path)
        if len(private_key) > MAX_RESPONSE_BYTES:
            raise GitHubAppError("credentials", "private key exceeded size limit")
        try:
            return jwt.encode(
                {"iat": now - 60, "exp": now + 540, "iss": self.config.client_id},
                private_key,
                algorithm="RS256",
            )
        except Exception as exc:
            raise GitHubAppError("credentials", "private key could not sign App JWT") from exc

    async def _bootstrap_json(
        self,
        method: str,
        path: str,
        *,
        credential: str,
        json_body: dict[str, Any] | None = None,
        expected_statuses: set[int] | None = None,
    ) -> dict[str, Any]:
        token_path = f"/app/installations/{self.config.installation_id}/access_tokens"
        if (method, path) not in {("GET", "/app"), ("POST", token_path)}:
            raise ValueError("GitHub App bootstrap endpoint is not allowed")
        response = await self.transport.request(
            method,
            API_BASE + path,
            headers={
                "Accept": ACCEPT,
                "X-GitHub-Api-Version": API_VERSION,
                "Authorization": f"Bearer {credential}",
            },
            json_body=json_body,
            max_bytes=self.max_response_bytes,
        )
        if len(response.body) > self.max_response_bytes:
            raise GitHubAppError("transient", "GitHub response exceeded size limit")
        if 300 <= response.status < 400:
            raise GitHubAppError(
                "conflict_or_invalid", "GitHub App bootstrap redirect was rejected"
            )
        allowed = expected_statuses or {200}
        if response.status not in allowed:
            raise _http_error(response.status, response.headers, self.clock())
        return _decode_object(response.body)


def _single_repository(payload: dict[str, Any]) -> dict[str, Any]:
    repositories = payload.get("repositories")
    if (
        not isinstance(repositories, list)
        or len(repositories) != 1
        or not isinstance(repositories[0], dict)
    ):
        raise GitHubAppError("permission", "installation repository selection did not match")
    return repositories[0]


def _validate_permissions(value: Any) -> None:
    if not isinstance(value, dict) or any(
        value.get(name) != level for name, level in _PERMISSIONS.items()
    ):
        raise GitHubAppError("permission", "installation permissions did not match")
    extras = set(value) - set(_PERMISSIONS)
    if extras - {"metadata"} or ("metadata" in value and value["metadata"] != "read"):
        raise GitHubAppError("permission", "installation permissions did not match")


def _strict_positive_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _decode_object(body: bytes) -> dict[str, Any]:
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GitHubAppError("conflict_or_invalid", "GitHub response was not valid JSON") from exc
    if not isinstance(value, dict):
        raise GitHubAppError("conflict_or_invalid", "GitHub response was not an object")
    return value


def _parse_timestamp(value: Any) -> float:
    if not isinstance(value, str):
        raise GitHubAppError("credentials", "installation token expiry was malformed")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timezone is required")
        return parsed.timestamp()
    except (OSError, OverflowError, ValueError) as exc:
        raise GitHubAppError("credentials", "installation token expiry was malformed") from exc


def _http_error(status: int, headers: dict[str, str], now: float) -> GitHubAppError:
    retry_after = headers.get("Retry-After") or headers.get("retry-after")
    remaining = headers.get("X-RateLimit-Remaining") or headers.get("x-ratelimit-remaining")
    if status in {403, 429} and (retry_after is not None or remaining == "0"):
        retry_at = now
        if retry_after:
            try:
                retry_at = now + max(0, int(retry_after))
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(retry_after).timestamp()
                except (TypeError, ValueError):
                    pass
        return GitHubAppError("rate_limited", "GitHub request was rate limited", retry_at=retry_at)
    category = {
        401: "credentials",
        403: "permission",
        404: "not_found_or_hidden",
        409: "conflict_or_invalid",
        422: "conflict_or_invalid",
    }.get(status, "transient" if status >= 500 else "conflict_or_invalid")
    return GitHubAppError(category, f"GitHub request failed ({category})")
