"""Bounded conditional GET storage shared by the startup-owned GitHub runner."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Sequence
from hashlib import sha256

from src.git.github import GitHubApiResponse
from src.git.github_contracts import GitHubCredentialIdentity, GitHubRepositoryBinding

MAX_CACHE_ENTRIES = 256
MAX_CACHE_BYTES = 16 * 1024 * 1024

CacheKey = tuple[GitHubCredentialIdentity, str, GitHubRepositoryBinding | None, tuple[str, ...]]


def conditional_key(
    command: Sequence[str],
    identity: GitHubCredentialIdentity,
    repository: GitHubRepositoryBinding | None,
    token: str | None,
) -> CacheKey | None:
    """Only untransformed REST GETs without caller-supplied conditions qualify.

    Keep full arguments in the key so representation headers and page/query
    parameters cannot collide. Unknown flags bypass caching rather than guessing
    their effect on the request or output. Secrets are never stored in the key.
    """
    if command[0] != "api":
        return None
    method, endpoint = "GET", None
    arguments = iter(command[1:])
    for argument in arguments:
        if argument in {"--include", "-i"}:
            continue
        if argument in {"--method", "-X", "--hostname", "--header", "-H"}:
            value = next(arguments, None)
            if value is None:
                return None
            if argument in {"--method", "-X"}:
                method = value.upper()
            elif argument in {"--header", "-H"}:
                name = value.split(":", 1)[0].strip().lower()
                if name.startswith("if-") or name in {"authorization", "range"}:
                    return None
        elif argument.startswith("-") or endpoint is not None:
            return None
        else:
            endpoint = argument
    if method != "GET" or endpoint is None or endpoint.lstrip("/") == "graphql":
        return None
    fingerprint = sha256(token.encode()).hexdigest() if token is not None else "stored-login"
    return identity, fingerprint, repository, tuple(command)


def frame_response(response: GitHubApiResponse) -> bytes:
    headers = [f"HTTP/2.0 {response.status}"]
    headers.extend(f"{name}: {value}" for name, value in response.headers.items())
    return "\r\n".join(headers).encode("latin-1") + b"\r\n\r\n" + response.body


class ConditionalResponseCache:
    def __init__(self) -> None:
        self._entries: OrderedDict[CacheKey, tuple[GitHubApiResponse, int]] = OrderedDict()
        self._bytes = 0

    def get(self, key: CacheKey | None, *, output_limit: int) -> GitHubApiResponse | None:
        if key is None or key not in self._entries:
            return None
        response, _ = self._entries[key]
        if len(frame_response(response)) > output_limit:
            return None
        self._entries.move_to_end(key)
        return response

    def discard(self, key: CacheKey) -> None:
        old = self._entries.pop(key, None)
        if old is not None:
            self._bytes -= old[1]

    def store(self, key: CacheKey, response: GitHubApiResponse) -> None:
        self.discard(key)
        if (
            response.status != 200
            or not (response.headers.get("etag") or response.headers.get("last-modified"))
            or "no-store" in response.headers.get("cache-control", "").lower()
            or response.headers.get("vary", "").strip() == "*"
        ):
            return
        size = len(frame_response(response)) + sum(len(value.encode()) for value in key[3])
        if size > MAX_CACHE_BYTES:
            return
        self._entries[key] = (response, size)
        self._bytes += size
        while len(self._entries) > MAX_CACHE_ENTRIES or self._bytes > MAX_CACHE_BYTES:
            _, (_, evicted_size) = self._entries.popitem(last=False)
            self._bytes -= evicted_size
