"""Tests for aq-surface Phase S2 session-scoped API auth."""

from __future__ import annotations

import hashlib
import time

import pytest

from src.api.auth import LOCAL_SCOPE, RequestScope, SessionTokenStore, TOKEN_PREFIX
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


class TestSessionTokenStore:
    async def _store(self, tmp_path, *, ttl_hours=72):
        db = Database(str(tmp_path / "store.db"))
        await db.initialize()
        return db, SessionTokenStore(db, ttl_hours=ttl_hours)

    async def test_mint_returns_prefixed_plaintext_once(self, tmp_path):
        db, store = await self._store(tmp_path)
        tok = await store.mint(session_id="s1", task_id="t1", project_id="p1")
        assert tok.startswith(TOKEN_PREFIX)
        assert len(tok) >= len(TOKEN_PREFIX) + 32
        h = hashlib.sha256(tok.encode()).hexdigest()
        row = await db.get_api_token(h)
        assert row is not None and row["session_id"] == "s1"
        await db.close()

    async def test_validate_happy_path(self, tmp_path):
        db, store = await self._store(tmp_path)
        tok = await store.mint(session_id="s1", task_id="t1", project_id="p1")
        scope = await store.validate(tok)
        assert scope == RequestScope(
            kind="session", session_id="s1", task_id="t1", project_id="p1"
        )
        await db.close()

    async def test_validate_missing_prefix_returns_none(self, tmp_path):
        db, store = await self._store(tmp_path)
        assert await store.validate("bearer-without-prefix") is None
        await db.close()

    async def test_validate_unknown_token_returns_none(self, tmp_path):
        db, store = await self._store(tmp_path)
        assert await store.validate(TOKEN_PREFIX + "z" * 43) is None
        await db.close()

    async def test_validate_revoked_returns_none(self, tmp_path):
        db, store = await self._store(tmp_path)
        tok = await store.mint(session_id="s1", task_id=None, project_id=None)
        assert await store.revoke_session("s1") == 1
        assert await store.validate(tok) is None
        await db.close()

    async def test_validate_expired_returns_none(self, tmp_path):
        db, store = await self._store(tmp_path)
        # Insert directly with an already-expired row keyed by the hash of a
        # plaintext we control.
        pt = TOKEN_PREFIX + "x" * 43
        real_h = hashlib.sha256(pt.encode()).hexdigest()
        await db.insert_api_token(
            token_hash=real_h, session_id="s2", task_id=None, project_id=None,
            created_at=time.time() - 7200, expires_at=time.time() - 3600,
        )
        assert await store.validate(pt) is None
        await db.close()

    async def test_cache_short_circuits_second_validate(self, tmp_path, monkeypatch):
        db, store = await self._store(tmp_path)
        tok = await store.mint(session_id="s1", task_id=None, project_id=None)
        assert await store.validate(tok) is not None

        async def _boom(*_a, **_k):
            raise AssertionError("cache miss")

        monkeypatch.setattr(db, "get_api_token", _boom)
        assert await store.validate(tok) is not None
        await db.close()

    async def test_revoke_session_invalidates_cache(self, tmp_path):
        db, store = await self._store(tmp_path)
        tok = await store.mint(session_id="s1", task_id=None, project_id=None)
        assert await store.validate(tok) is not None  # populates cache
        await store.revoke_session("s1")
        assert await store.validate(tok) is None
        await db.close()

    async def test_revoke_expired_drops_and_reports(self, tmp_path):
        db, store = await self._store(tmp_path)
        await db.insert_api_token(
            token_hash="dead" + "0" * 60,
            session_id="sX", task_id=None, project_id=None,
            created_at=time.time() - 7200, expires_at=time.time() - 3600,
        )
        n = await store.revoke_expired()
        assert n == 1
        await db.close()

    def test_local_scope_singleton(self):
        assert LOCAL_SCOPE.kind == "local"
        assert LOCAL_SCOPE.session_id is None
