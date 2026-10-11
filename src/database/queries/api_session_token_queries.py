"""Queries for the ``api_session_tokens`` table (aq-surface Phase S2).

The table itself landed in Wave-0 substrate migration ``93a8a9e48fb8`` —
this mixin is the query layer.  Rows store sha256 hex of the plaintext
token; the plaintext is returned by :meth:`SessionTokenStore.mint` and
never persisted anywhere.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import and_, delete, insert, or_, select, update

from src.database.tables import api_session_tokens, sessions


class ApiSessionTokenQueriesMixin:
    async def insert_api_token(
        self,
        *,
        token_hash: str,
        session_id: str,
        session_instance_token: str | None = None,
        task_id: str | None,
        project_id: str | None,
        created_at: float,
        expires_at: float,
        elevated: bool = False,
    ) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(api_session_tokens).values(
                    token_hash=token_hash,
                    session_id=session_id,
                    session_instance_token=session_instance_token,
                    task_id=task_id,
                    project_id=project_id,
                    created_at=created_at,
                    expires_at=expires_at,
                    revoked_at=None,
                    elevated=bool(elevated),
                )
            )

    async def get_api_token(self, token_hash: str) -> dict[str, Any] | None:
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(api_session_tokens).where(
                            api_session_tokens.c.token_hash == token_hash
                        )
                    )
                )
                .mappings()
                .first()
            )
            return dict(row) if row else None

    async def revoke_api_tokens_for_session(self, session_id: str, *, now: float) -> int:
        async with self._engine.begin() as conn:
            result = await conn.execute(
                update(api_session_tokens)
                .where(
                    and_(
                        api_session_tokens.c.session_id == session_id,
                        api_session_tokens.c.revoked_at.is_(None),
                    )
                )
                .values(revoked_at=now)
            )
            return int(result.rowcount or 0)

    async def renew_supervisor_api_tokens(
        self, *, now: float, renew_before: float, expires_at: float,
    ) -> list[str]:
        """Extend still-valid credentials for the same live supervisor launch.

        Keep the identity checks and expiry/revocation predicate in the UPDATE:
        a stale cache or a concurrent revocation must never resurrect a token.
        """
        token = api_session_tokens.c
        session = sessions.c
        live_supervisor = select(session.id).where(
            session.id == token.session_id,
            session.instance_token == token.session_instance_token,
            session.instance_token != "",
            session.project_id.is_not_distinct_from(token.project_id),
            session.profile_id == "supervisor",
            session.lifecycle == "named",
            session.task_id.is_(None),
            session.state.in_(("starting", "running")),
            session.desired_state == "running",
            session.ended_at.is_(None),
        ).exists()
        async with self._engine.begin() as conn:
            result = await conn.execute(
                update(api_session_tokens).where(
                    token.elevated.is_(True),
                    token.task_id.is_(None),
                    token.revoked_at.is_(None),
                    token.expires_at > now,
                    token.expires_at <= renew_before,
                    live_supervisor,
                ).values(expires_at=expires_at).returning(token.token_hash)
            )
            return list(result.scalars())

    async def delete_expired_api_tokens(self, *, now: float) -> int:
        async with self._engine.begin() as conn:
            result = await conn.execute(
                delete(api_session_tokens).where(
                    or_(
                        api_session_tokens.c.expires_at < now,
                        and_(
                            api_session_tokens.c.revoked_at.isnot(None),
                            api_session_tokens.c.revoked_at < now - 60.0,
                        ),
                    )
                )
            )
            return int(result.rowcount or 0)
