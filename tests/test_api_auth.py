"""Tests for aq-surface Phase S2 session-scoped API auth."""

from __future__ import annotations

import hashlib
import time

import pytest

from src.database import Database


class TestApiSessionTokenQueries:
    async def _db(self, tmp_path):
        db = Database(str(tmp_path / "auth.db"))
        await db.initialize()
        return db

    async def test_insert_and_get_roundtrip(self, tmp_path):
        db = await self._db(tmp_path)
        now = time.time()
        await db.insert_api_token(
            token_hash="h" * 64,
            session_id="s1",
            task_id="t1",
            project_id="p1",
            created_at=now,
            expires_at=now + 3600,
        )
        row = await db.get_api_token("h" * 64)
        assert row is not None
        assert row["session_id"] == "s1"
        assert row["task_id"] == "t1"
        assert row["project_id"] == "p1"
        assert row["revoked_at"] is None
        assert row["expires_at"] == pytest.approx(now + 3600)
        await db.close()

    async def test_get_unknown_returns_none(self, tmp_path):
        db = await self._db(tmp_path)
        assert await db.get_api_token("nope") is None
        await db.close()

    async def test_revoke_marks_all_for_session(self, tmp_path):
        db = await self._db(tmp_path)
        now = time.time()
        for i in range(3):
            await db.insert_api_token(
                token_hash=f"h{i:0>63}",
                session_id="s1",
                task_id=None,
                project_id=None,
                created_at=now,
                expires_at=now + 3600,
            )
        await db.insert_api_token(
            token_hash="other" + "0" * 59,
            session_id="s2",
            task_id=None,
            project_id=None,
            created_at=now,
            expires_at=now + 3600,
        )
        n = await db.revoke_api_tokens_for_session("s1", now=now)
        assert n == 3
        for i in range(3):
            row = await db.get_api_token(f"h{i:0>63}")
            assert row["revoked_at"] == pytest.approx(now)
        assert (await db.get_api_token("other" + "0" * 59))["revoked_at"] is None
        await db.close()

    async def test_revoke_is_idempotent(self, tmp_path):
        db = await self._db(tmp_path)
        now = time.time()
        await db.insert_api_token(
            token_hash="h" * 64,
            session_id="s1",
            task_id=None,
            project_id=None,
            created_at=now,
            expires_at=now + 3600,
        )
        assert await db.revoke_api_tokens_for_session("s1", now=now) == 1
        assert await db.revoke_api_tokens_for_session("s1", now=now + 1) == 0
        await db.close()

    async def test_delete_expired_removes_only_past(self, tmp_path):
        db = await self._db(tmp_path)
        now = time.time()
        await db.insert_api_token(
            token_hash="live" + "0" * 60,
            session_id="s1",
            task_id=None,
            project_id=None,
            created_at=now,
            expires_at=now + 3600,
        )
        await db.insert_api_token(
            token_hash="dead" + "0" * 60,
            session_id="s2",
            task_id=None,
            project_id=None,
            created_at=now - 7200,
            expires_at=now - 3600,
        )
        n = await db.delete_expired_api_tokens(now=now)
        assert n == 1
        assert await db.get_api_token("live" + "0" * 60) is not None
        assert await db.get_api_token("dead" + "0" * 60) is None
        await db.close()

    async def test_delete_expired_reaps_revoked_after_grace(self, tmp_path):
        db = await self._db(tmp_path)
        now = time.time()
        await db.insert_api_token(
            token_hash="r" * 64,
            session_id="s1",
            task_id=None,
            project_id=None,
            created_at=now - 7200,
            expires_at=now + 3600,
        )
        await db.revoke_api_tokens_for_session("s1", now=now - 3600)
        n = await db.delete_expired_api_tokens(now=now)
        assert n == 1
        await db.close()
