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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

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
    task_id: str | None = None
    project_id: str | None = None
    #: Trusted-scope flag. When True (currently only per-project
    #: supervisor sessions), :func:`check_command_scope` allows any
    #: command instead of restricting to :data:`AGENT_COMMAND_SET`.
    #: ``project_id`` is still enforced when set.
    elevated: bool = False


LOCAL_SCOPE = RequestScope(kind="local")


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

    async def mint(
        self,
        *,
        session_id: str,
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
        if refresh:
            self._cache.pop(h, None)
        cached = self._cache.get(h)
        if cached is not None:
            scope, expires_at = cached
            if expires_at > now:
                return scope
            self._cache.pop(h, None)
        row = await self._db.get_api_token(h)
        if row is None:
            return None
        if row.get("revoked_at") is not None:
            return None
        expires_at = float(row["expires_at"])
        if expires_at <= now:
            return None
        scope = RequestScope(
            kind="session",
            session_id=row["session_id"],
            task_id=row.get("task_id"),
            project_id=row.get("project_id"),
            elevated=bool(row.get("elevated") or False),
        )
        self._cache[h] = (scope, expires_at)
        return scope

    async def revoke_session(self, session_id: str) -> int:
        now = time.time()
        n = await self._db.revoke_api_tokens_for_session(session_id, now=now)
        # Invalidate cache entries for this session.
        for h in [k for k, (s, _) in self._cache.items() if s.session_id == session_id]:
            self._cache.pop(h, None)
        if n:
            logger.debug("revoked %d api token(s) for session %s", n, session_id)
        return n

    async def revoke_expired(self) -> int:
        now = time.time()
        n = await self._db.delete_expired_api_tokens(now=now)
        # Drop cache entries whose expiry has passed.
        for h in [k for k, (_, exp) in self._cache.items() if exp <= now]:
            self._cache.pop(h, None)
        return n
