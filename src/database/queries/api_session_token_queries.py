"""Queries for the ``api_session_tokens`` table (aq-surface Phase S2).

The table itself landed in Wave-0 substrate migration ``93a8a9e48fb8`` —
this mixin is the query layer.  Rows store sha256 hex of the plaintext
token; the plaintext is returned by :meth:`SessionTokenStore.mint` and
never persisted anywhere.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import and_, delete, insert, or_, select, update

from src.database.tables import api_session_tokens


class ApiSessionTokenQueriesMixin:
    async def insert_api_token(
        self,
        *,
        token_hash: str,
        session_id: str,
        task_id: str | None,
        project_id: str | None,
        created_at: float,
        expires_at: float,
    ) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(api_session_tokens).values(
                    token_hash=token_hash,
                    session_id=session_id,
                    task_id=task_id,
                    project_id=project_id,
                    created_at=created_at,
                    expires_at=expires_at,
                    revoked_at=None,
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
