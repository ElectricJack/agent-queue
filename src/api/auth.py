"""Session-scoped bearer-token auth for the local HTTP API.

See ``docs/specs/implementation/aq-surface.md`` §4.  Mint is called at
session start by ``src/orchestrator/execution.py`` (task sessions) and
``src/messages/session_lens.py`` (named/supervisor sessions).  Validate is
called by :class:`~src.api.middleware.TokenAuthMiddleware`.  Revoke is
called on session close and periodically by
:meth:`~src.orchestrator.core.Orchestrator._revoke_expired_tokens`.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import time
from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterator, Literal

if TYPE_CHECKING:
    from src.database import Database

logger = logging.getLogger(__name__)

TOKEN_PREFIX = "aqs_"


@dataclass(frozen=True)
class RequestScope:
    """Identity attached to a request by :class:`TokenAuthMiddleware`.

    ``kind="local"`` means "no bearer token supplied" — the trusted
    loopback path, allowed to run every command.  ``kind="session"`` means
    the request carried a valid session token; the daemon then enforces
    that the tool is in :data:`AGENT_COMMAND_SET` and that any ``task_id``
    / ``project_id`` args match this scope.
    """

    kind: Literal["local", "session"]
    session_id: str | None = None
    # Immutable daemon-minted launch instance. Unlike profile identity this
    # must not follow a replacement ``sessions`` row: managed writes compare
    # the caller's original token claim with the currently locked instance.
    session_instance_token: str | None = None
    task_id: str | None = None
    project_id: str | None = None
    #: Trusted-scope flag. When True (global and per-project
    #: supervisor sessions), :func:`check_command_scope` allows any
    #: command instead of restricting to :data:`AGENT_COMMAND_SET`.
    #: ``project_id`` is still enforced when set.
    #:
    #: Elevation is **not** a capability bypass: it answers "which project
    #: may this token touch", while the capability policy answers "which
    #: commands may this profile run".
    elevated: bool = False
    #: Server-derived identity (Playbook V2 Package 0 §3.7).  Populated by
    #: :class:`~src.api.middleware.TokenAuthMiddleware` *after*
    #: ``store.validate()`` returns, from the live ``sessions`` row — never
    #: minted into the token and never client-supplied.
    #:
    #: Deriving rather than persisting is deliberate: a token minted at
    #: launch would pin a ``profile_id`` that goes stale the moment an
    #: operator edits or re-syncs the profile, and stale-*wide* is the
    #: dangerous direction.  It also means Package 0 needs no
    #: ``api_session_tokens`` migration, which is what keeps its rollback
    #: boundary free of a database downgrade.
    profile_id: str | None = None
    policy_fingerprint: str | None = None


LOCAL_SCOPE = RequestScope(kind="local")

# The dashboard proxy can see the real peer while the daemon sees loopback.
# Keep its operator-viewer verdict in request context so the generic command
# route cannot bypass the Reviews route's approval gate.
_operator_viewer_allowed: ContextVar[bool] = ContextVar(
    "operator_viewer_allowed", default=True,
)
_request_surface: ContextVar[str] = ContextVar("request_surface", default="cli")


def request_surface() -> str:
    """Transport provenance for operator audit, derived by HTTP middleware."""
    return _request_surface.get()


def operator_viewer_allowed() -> bool:
    return _operator_viewer_allowed.get()


def request_operator_viewer(request) -> bool:
    """Trust only a local peer and a first-party proxy verdict or loopback Host."""
    client_host = request.client.host if request.client else None
    if client_host not in {"127.0.0.1", "::1", "localhost"}:
        return False
    viewer = request.headers.get("x-aq-dashboard-viewer")
    if viewer == "other":
        return False
    if viewer == "operator":
        return True
    # Direct daemon/CLI access has no proxy assertion.  An unrecognised Host
    # may be a browser pointed at loopback through DNS rebinding.
    return request.url.hostname in {"localhost", "127.0.0.1", "::1"}


def request_remote_dashboard_viewer(request) -> bool:
    """A browser the dashboard server proxied from another machine.

    The edge reaches the daemon from loopback and replaces any client-supplied
    ``x-aq-dashboard-viewer`` with its own real-peer verdict, which is
    ``other`` for every peer it does not take for the operator.
    """
    client_host = request.client.host if request.client else None
    if client_host not in {"127.0.0.1", "::1", "localhost"}:
        return False
    return request.headers.get("x-aq-dashboard-viewer") == "other"


@contextmanager
def operator_viewer_context(allowed: bool, *, surface: str = "cli") -> Iterator[None]:
    token = _operator_viewer_allowed.set(allowed)
    surface_token = _request_surface.set(surface)
    try:
        yield
    finally:
        _operator_viewer_allowed.reset(token)
        _request_surface.reset(surface_token)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class SessionTokenStore:
    def __init__(self, db: "Database", *, ttl_hours: int = 72) -> None:
        self._db = db
        self._ttl_seconds = max(1, ttl_hours) * 3600
        # {token_hash: (scope, expires_at)} — a lookup by exact sha256 hash
        # cannot leak timing info about the plaintext, so no ``compare_digest``
        # is used or needed here.
        self._cache: dict[str, tuple[RequestScope, float]] = {}
        # Diagnostic evidence only; never store the bearer or its hash here.
        self.auth_failures: dict[str, dict] = {}
        # Bounded tombstones identify rejected calls after credential pruning;
        # they are never accepted as authentication or exposed in diagnostics.
        self._rejected_tokens: dict[str, tuple[RequestScope, float, str]] = {}
        #: Called with the session id of every validated session token; the
        #: orchestrator wires it to provider availability, where a session's
        #: first authenticated call is ``launch_success`` evidence
        #: (provider-failover D2).  Must be cheap and must not raise.
        self.on_session_seen: Callable[[str], None] | None = None

    async def mint(
        self,
        *,
        session_id: str,
        session_instance_token: str | None = None,
        task_id: str | None,
        project_id: str | None,
        elevated: bool = False,
    ) -> str:
        plaintext = TOKEN_PREFIX + secrets.token_urlsafe(32)
        now = time.time()
        expires_at = now + self._ttl_seconds
        h = _hash(plaintext)
        await self._db.insert_api_token(
            token_hash=h,
            session_id=session_id,
            session_instance_token=session_instance_token,
            task_id=task_id,
            project_id=project_id,
            created_at=now,
            expires_at=expires_at,
            elevated=elevated,
        )
        self._cache[h] = (
            RequestScope(
                kind="session",
                session_id=session_id,
                session_instance_token=session_instance_token,
                task_id=task_id,
                project_id=project_id,
                elevated=elevated,
            ),
            expires_at,
        )
        return plaintext

    async def validate(self, token: str, *, refresh: bool = False) -> RequestScope | None:
        if not token or not token.startswith(TOKEN_PREFIX):
            return None
        h = _hash(token)
        now = time.time()
        # Long-lived terminal streams must observe revocation by another store/process.
        cached = self._cache.get(h)
        if refresh:
            self._cache.pop(h, None)
        if cached is not None and not refresh:
            scope, expires_at = cached
            if expires_at > now:
                self._seen(scope)
                return scope
            self._cache.pop(h, None)
        row = await self._db.get_api_token(h)
        if row is None:
            if cached is not None:
                self._failed(cached[0], "expired or removed", now)
            elif (rejected := self._rejected_tokens.get(h)) and rejected[1] > now:
                self._failed(rejected[0], rejected[2], now)
            return None
        scope = RequestScope(
            kind="session", session_id=row["session_id"],
            session_instance_token=row.get("session_instance_token"),
            task_id=row.get("task_id"), project_id=row.get("project_id"),
            elevated=bool(row.get("elevated") or False),
        )
        if row.get("revoked_at") is not None:
            self._failed(scope, "revoked", now)
            return None
        expires_at = float(row["expires_at"])
        if expires_at <= now:
            self._failed(scope, "expired", now)
            return None
        self._cache[h] = (scope, expires_at)
        self._seen(scope)
        return scope

    def _seen(self, scope: RequestScope) -> None:
        failure = self.auth_failures.get(scope.session_id)
        if failure and failure["instance_token"] == scope.session_instance_token:
            self.auth_failures.pop(scope.session_id, None)
        hook = self.on_session_seen
        if hook is None or not scope.session_id:
            return
        try:
            hook(scope.session_id)
        except Exception:  # evidence must never break authentication
            logger.debug("on_session_seen hook failed", exc_info=True)

    def _failed(self, scope: RequestScope, reason: str, now: float) -> None:
        if not scope.session_id:
            return
        if scope.session_id not in self.auth_failures and len(self.auth_failures) >= 256:
            self.auth_failures.pop(next(iter(self.auth_failures)))
        self.auth_failures[scope.session_id] = {
            "instance_token": scope.session_instance_token, "at": now, "reason": reason,
        }
        logger.warning("session authentication failed session_id=%s reason=%s",
                       scope.session_id, reason)

    def _remember_rejected(self, token_hash: str, scope: RequestScope, reason: str, now: float):
        if token_hash not in self._rejected_tokens and len(self._rejected_tokens) >= 256:
            self._rejected_tokens.pop(next(iter(self._rejected_tokens)))
        self._rejected_tokens[token_hash] = (scope, now + 900, reason)

    async def revoke_session(self, session_id: str) -> int:
        now = time.time()
        n = await self._db.revoke_api_tokens_for_session(session_id, now=now)
        # Invalidate cache entries for this session.
        for h in [k for k, (s, _) in self._cache.items() if s.session_id == session_id]:
            self._remember_rejected(h, self._cache[h][0], "revoked", now)
            self._cache.pop(h, None)
        if n:
            logger.debug("revoked %d api token(s) for session %s", n, session_id)
        return n

    async def revoke_expired(self) -> int:
        now = time.time()
        # A supervisor can outlive the launch TTL without making any requests.
        # Renew from daemon-owned live-session evidence before pruning; never
        # accept an expired bearer as authority to mint or renew credentials.
        renewed = await self._db.renew_supervisor_api_tokens(
            now=now,
            renew_before=now + min(3600, self._ttl_seconds / 2),
            expires_at=now + self._ttl_seconds,
        )
        for h in renewed:
            self._cache.pop(h, None)
        n = await self._db.delete_expired_api_tokens(now=now)
        # Drop cache entries whose expiry has passed.
        for h in [k for k, (_, exp) in self._cache.items() if exp <= now]:
            self._remember_rejected(h, self._cache[h][0], "expired", now)
            self._cache.pop(h, None)
        for h in [k for k, (_, until, _) in self._rejected_tokens.items() if until <= now]:
            self._rejected_tokens.pop(h, None)
        return n
